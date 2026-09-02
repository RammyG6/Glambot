"""Speed-ramp and colour-grade filtergraph construction for ffmpeg.

Kept separate from processor.py so the curve maths is unit-testable on its own
and the (long, fiddly) filtergraph strings live in one place. processor.py
imports this; this module imports nothing from processor.py (no cycle).
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

MIN_SPEED = 0.1

# The speed-ramp editor works on a log speed axis running from RAMP_VIEW_LO at
# the bottom to the ramp's own `max_speed` at the top. Curve handles are stored
# as (dt, dv) offsets in that normalised (t in [0,1], v in [0,1]) space, so the
# max_speed a curve was drawn with must travel with it and be used identically
# on save and render - the JS editor and this module have to agree exactly.
RAMP_VIEW_LO = 0.25
DEFAULT_MAX_SPEED = 40.0     # the historical fixed ceiling; default for old curves
HARD_MAX_SPEED = 1000.0      # absolute cap on a custom max_speed


def _v_params(view_hi: float) -> tuple[float, float]:
    off = math.log(RAMP_VIEW_LO, 2)
    return off, math.log(max(RAMP_VIEW_LO * 2, view_hi), 2) - off


def speed_to_v(s: float, view_hi: float = DEFAULT_MAX_SPEED) -> float:
    off, span = _v_params(view_hi)
    s = min(view_hi, max(RAMP_VIEW_LO, s))
    return (math.log(s, 2) - off) / span


def v_to_speed(v: float, view_hi: float = DEFAULT_MAX_SPEED) -> float:
    off, span = _v_params(view_hi)
    v = min(1.0, max(0.0, v))
    return min(view_hi, max(MIN_SPEED, 2 ** (v * span + off)))


def _auto_handle(pt: dict, other: dict, sign: float) -> tuple[float, float]:
    """Default 'flat weighted' tangent: a horizontal handle 1/3 of the way to
    the neighbouring point (reproduces the old ease-in/ease-out smoothstep)."""
    dt = sign * abs(other["t"] - pt["t"]) / 3.0
    return dt, 0.0


def _handle(pt: dict, key: str, other: dict, sign: float) -> tuple[float, float]:
    h = pt.get(key)
    if isinstance(h, (list, tuple)) and len(h) == 2:
        try:
            return float(h[0]), float(h[1])
        except (TypeError, ValueError):
            pass
    return _auto_handle(pt, other, sign)


def _bezier1(p0: float, p1: float, p2: float, p3: float, u: float) -> float:
    mt = 1.0 - u
    return (mt * mt * mt * p0 + 3 * mt * mt * u * p1
            + 3 * mt * u * u * p2 + u * u * u * p3)


@dataclass
class Grade:
    exposure: float = 0.0      # stops, -2..2
    contrast: float = 1.0      # 0.5..2
    white_balance: int = 0     # -100 (warm) .. 100 (cool)

    def is_neutral(self) -> bool:
        return abs(self.exposure) < 1e-3 and abs(self.contrast - 1.0) < 1e-3 and self.white_balance == 0


@dataclass
class SpeedRamp:
    points: list[dict]                 # [{"t": 0..1, "speed": 0.1..max_speed}], sorted, ends at 0 and 1
    interpolation: str = "smooth"      # "smooth" | "linear"
    smooth_frames: bool = False
    max_speed: float = DEFAULT_MAX_SPEED   # top of the editor's log speed axis


def sample_speed(points: list[dict], interpolation: str, u: float,
                 view_hi: float = DEFAULT_MAX_SPEED) -> float:
    """Speed multiplier at normalised time `u` in [0, 1].

    `linear` = straight segments on the (log-speed) editor axis. `smooth` =
    per-segment cubic bezier using each point's tangent handles (`hl`/`hr`,
    stored as (dt, dv) offsets; auto-computed flat handles when absent).
    `view_hi` is the curve's max speed - the top of the log axis the handle
    offsets were drawn against."""
    u = min(1.0, max(0.0, u))
    if not points:
        return 1.0
    if u <= points[0]["t"]:
        return points[0]["speed"]
    if u >= points[-1]["t"]:
        return points[-1]["speed"]

    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        if u > b["t"]:
            continue
        ta, tb = a["t"], b["t"]
        va, vb = speed_to_v(a["speed"], view_hi), speed_to_v(b["speed"], view_hi)
        span = tb - ta
        if span <= 0:
            return b["speed"]

        if interpolation == "linear":
            frac = (u - ta) / span
            return v_to_speed(va + (vb - va) * frac, view_hi)

        # Cubic bezier in (t, v). Control points:
        hr_dt, hr_dv = _handle(a, "hr", b, +1.0)
        hl_dt, hl_dv = _handle(b, "hl", a, -1.0)
        c1t = min(tb, max(ta, ta + hr_dt))
        c2t = min(tb, max(ta, tb + hl_dt))
        if c2t < c1t:
            c1t = c2t = (c1t + c2t) / 2.0
        c1v = va + hr_dv
        c2v = vb + hl_dv

        # bezier-x(p) is monotone (ta <= c1t <= c2t <= tb) -> bisect for p.
        lo, hi = 0.0, 1.0
        for _ in range(28):
            mid = (lo + hi) / 2.0
            if _bezier1(ta, c1t, c2t, tb, mid) < u:
                lo = mid
            else:
                hi = mid
        p = (lo + hi) / 2.0
        return v_to_speed(_bezier1(va, c1v, c2v, vb, p), view_hi)

    return points[-1]["speed"]


# The whole retime map becomes ONE setpts expression - a flat sum of one
# `min(max(T-x,0),w)*r` term per knot. ffmpeg's expression parser falls over
# on a large tree (tested: ~60 such terms parse, ~100 do not), so keep the
# knot count well under that. 32 piecewise-linear segments is visually smooth
# for a Bezier ramp curve.
_RAMP_MAX_KNOTS = 32


def _ramp_knots(ramp: "SpeedRamp", duration: float) -> tuple[list[float], list[float]]:
    """Piecewise-linear model of the retime map: knot times `xs` (0..duration,
    K+1 of them) and the mean speed `speeds` over each of the K intervals.

    Knots are placed non-uniformly - dense where the curve's log-speed changes
    fast (the ramp transitions), sparse where speed is steady - so a long clip
    follows the curve accurately without a huge filtergraph."""
    view_hi = getattr(ramp, "max_speed", DEFAULT_MAX_SPEED)

    fine = 2000
    ts = [i / fine for i in range(fine + 1)]
    sp = [min(view_hi, max(MIN_SPEED, sample_speed(ramp.points, ramp.interpolation, t, view_hi)))
          for t in ts]
    # Cumulative "cost" = uniform floor + how much log2(speed) moves; invert it
    # at equal increments to get the knot positions.
    cost = [0.0]
    for i in range(1, fine + 1):
        d_log = abs(math.log2(sp[i]) - math.log2(sp[i - 1]))
        cost.append(cost[-1] + (1.0 / fine) + 2.5 * d_log)
    total_cost = cost[-1] or 1.0

    k = max(8, min(_RAMP_MAX_KNOTS, int(duration / 8) + 1))
    xs = [0.0]
    j = 0
    for m in range(1, k):
        target = total_cost * m / k
        while j < fine and cost[j] < target:
            j += 1
        xs.append(round(ts[j] * duration, 4))
    xs.append(round(duration, 4))
    # de-dup / enforce strictly increasing
    dedup = [xs[0]]
    for x in xs[1:]:
        if x > dedup[-1] + 1e-4:
            dedup.append(x)
    if dedup[-1] < duration:
        dedup[-1] = round(duration, 4)
    xs = dedup

    speeds = []
    for a, b in zip(xs[:-1], xs[1:]):
        mid = [(a + (b - a) * f / 4) / duration for f in range(5)]
        vals = [min(view_hi, max(MIN_SPEED, sample_speed(ramp.points, ramp.interpolation, u, view_hi)))
                for u in mid]
        speeds.append(sum(vals) / len(vals))
    return xs, speeds


def build_speed_ramp_filtergraph(ramp: SpeedRamp, in_label: str, duration: float | None,
                                 seg_len: float = 0.2):
    """Return (fragment, out_label, expected_out_duration).

    Retimes the whole stream with a single `setpts` whose new timestamp is a
    monotonic piecewise-linear function of input time - a flat sum of clipped
    ramps, `T_out(T) = sum_k clip(T - x_k, 0, w_k) / s_k`. One filter, no
    split/concat fan-out, so it scales to multi-minute clips. The output's
    constant frame rate is handled by the caller's output `-r`.
    """
    if not duration or duration <= 0 or not ramp.points:
        return "", in_label, None

    xs, speeds = _ramp_knots(ramp, duration)
    terms = []
    expected = 0.0
    for a, b, s in zip(xs[:-1], xs[1:], speeds):
        w = b - a
        r = 1.0 / s
        expected += w * r
        terms.append(f"min(max(T-{a:.4f},0),{w:.4f})*{r:.6f}")
    expr = "+".join(terms)
    # setpts=PTS-STARTPTS first normalises the (possibly trimmed) input to t=0,
    # so the T in the map below is measured from the clip's own start.
    chain = f"setpts=PTS-STARTPTS,setpts='({expr})/TB'"
    if ramp.smooth_frames:
        chain += ",minterpolate=mi_mode=mci"
    out_label = "[srout]"
    fragment = f"{in_label}{chain}{out_label}"
    return fragment, out_label, expected


def expected_ramp_duration(ramp: SpeedRamp, duration: float | None, seg_len: float = 0.2) -> float | None:
    """How long the output of a speed ramp will be, for progress-bar sizing -
    the same piecewise-linear map as build_speed_ramp_filtergraph."""
    if not duration or duration <= 0 or not ramp.points:
        return duration
    xs, speeds = _ramp_knots(ramp, duration)
    return sum((b - a) / s for a, b, s in zip(xs[:-1], xs[1:], speeds))


_filter_cache: set[str] | None = None


def has_filter(name: str, ffmpeg_bin: str) -> bool:
    global _filter_cache
    if _filter_cache is None:
        _filter_cache = set()
        try:
            out = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW_FLAGS,
            ).stdout
            for line in out.splitlines():
                bits = line.split()
                if len(bits) >= 2:
                    _filter_cache.add(bits[1])
        except (OSError, subprocess.SubprocessError):
            logger.warning("Could not probe ffmpeg filters", exc_info=True)
    return name in _filter_cache


def build_grade_filter(grade: Grade | None, ffmpeg_bin: str) -> str:
    """A comma-chained fragment (no pad labels) for exposure / contrast / white
    balance, or "" when the grade is neutral."""
    if grade is None or grade.is_neutral():
        return ""
    parts: list[str] = []

    exposure = max(-2.0, min(2.0, grade.exposure))
    contrast = max(0.5, min(2.0, grade.contrast))
    if abs(exposure) > 1e-3 or abs(contrast - 1.0) > 1e-3:
        gamma = 2 ** (-exposure / 2)
        brightness = max(-0.3, min(0.3, exposure * 0.10))
        parts.append(f"eq=gamma={gamma:.4f}:brightness={brightness:.4f}:contrast={contrast:.3f}")

    wb = max(-100, min(100, int(grade.white_balance)))
    if wb != 0:
        if has_filter("colortemperature", ffmpeg_bin):
            temp = max(3000, min(12000, int(6500 - wb * 30)))
            parts.append(f"colortemperature=temperature={temp}:mix=1")
        else:
            r = wb * 0.003
            parts.append(f"colorbalance=rs={-r:.4f}:bs={r:.4f}")

    return ",".join(parts)


def build_cine_source_filter(input_path, ffprobe_bin: str) -> str:
    """Neutralise raw Phantom `.cine` colour: ffmpeg debayers the Bayer sensor
    data but ignores the camera's embedded white-balance gains and gamma, so
    the frame comes out green and flat. Read those tags and apply them.

    Returns a comma-chained fragment (no pad labels) to prepend to `[0:v]`,
    or "" if the file has no `wbgain` tags (i.e. it isn't raw Phantom footage).
    The 2.2 gamma is a sensible fixed default for near-linear raw; the operator
    fine-tunes on top with the exposure / contrast / white-balance grade."""
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_tags", "-of", "json", str(input_path)],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW_FLAGS,
        ).stdout
        tags = (json.loads(out).get("streams") or [{}])[0].get("tags", {})
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return ""

    try:
        r_gain = float(tags["wbgain[0].r"])
        b_gain = float(tags["wbgain[0].b"])
    except (KeyError, TypeError, ValueError):
        return ""

    r_gain = min(4.0, max(0.2, r_gain))
    b_gain = min(4.0, max(0.2, b_gain))
    return (
        f"format=gbrp16le,colorchannelmixer=rr={r_gain:.4f}:gg=1:bb={b_gain:.4f},"
        f"eq=gamma=2.2"
    )
