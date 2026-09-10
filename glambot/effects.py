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
from pathlib import Path

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
    exposure: float = 0.0      # stops, -5..5
    contrast: float = 1.0      # 0.5..2
    saturation: float = 1.0    # 0..2, 1 = untouched
    white_balance: int = 0     # -100 (warm) .. 100 (cool)

    def is_neutral(self) -> bool:
        return (abs(self.exposure) < 1e-3 and abs(self.contrast - 1.0) < 1e-3
                and abs(self.saturation - 1.0) < 1e-3 and self.white_balance == 0)


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

    exposure = max(-5.0, min(5.0, grade.exposure))
    contrast = max(0.5, min(2.0, grade.contrast))
    saturation = max(0.0, min(2.0, grade.saturation))
    if (abs(exposure) > 1e-3 or abs(contrast - 1.0) > 1e-3
            or abs(saturation - 1.0) > 1e-3):
        gamma = 2 ** (-exposure / 2)
        # The cap has to track the slider's range, not sit at a fixed 0.3.
        # `eq` combines brightness and gamma such that a *frozen* brightness
        # against a still-falling gamma sends the picture back the other way:
        # measured on a real frame, +3/+4/+5 stops came out at mean luma
        # 78 / 63 / 52, i.e. the slider reversed past 3 stops. Keeping the
        # brightness proportional across the whole range gives 78 / 84 / 92.
        brightness = max(-0.5, min(0.5, exposure * 0.10))
        parts.append(f"eq=gamma={gamma:.4f}:brightness={brightness:.4f}"
                     f":contrast={contrast:.3f}:saturation={saturation:.3f}")

    wb = max(-100, min(100, int(grade.white_balance)))
    if wb != 0:
        if has_filter("colortemperature", ffmpeg_bin):
            temp = max(3000, min(12000, int(6500 - wb * 30)))
            parts.append(f"colortemperature=temperature={temp}:mix=1")
        else:
            r = wb * 0.003
            parts.append(f"colorbalance=rs={-r:.4f}:bs={r:.4f}")

    return ",".join(parts)


# Base looks for raw Phantom footage, chosen per project. `camera` reproduces
# what the camera recorded (its own tone curve, from the sidecar) and is the
# default; the others replace that tone stage with a fixed curve.
#
# FALLBACK ONLY. These are Glambot's own curves, tuned by eye - they are NOT
# Vision Research's Log1/Log2 and must never be described to a client as
# matching PCC. They are used only when a profile has no fitted LUT in looks/,
# and measured ~10% mean error against what the SDK actually renders.
#
# The real Log1/Log2 come from looks/*.cube instead (see look_lut_path and
# camera_bridge/fit_look.py), fitted from the SDK's own renderer to ~0.5%.
# `GCI_LOGMODE` is a *cine header* field, so the SDK renders log from an
# ordinary raw clip even though this body cannot record it - the camera-side
# gsSupportsLogMode reads 0 while the file cine's GCI_SUPPORTSLOGMODE reads 1.
#
# NB on direction: ffmpeg's `eq` applies output = input^(1/gamma), so a *higher*
# gamma lifts shadows. A flat/log look wants lifted blacks and reduced contrast,
# hence gamma above 2.2 on the log variants, not below - the reverse crushes the
# shadows it is supposed to protect.
CAMERA_PROFILE = "camera"
_CINE_PROFILES = {
    "rec709": "eq=gamma=2.2",
    "log1": "eq=gamma=2.6:contrast=0.85:brightness=0.03",
    "log2": "eq=gamma=3.0:contrast=0.70:brightness=0.06",
}

# The camera's colour description, written beside each clip at import time.
LOOK_SUFFIX = ".look.json"


def _normalise_profile(profile: str | None) -> str:
    """A profile name we recognise. Anything else becomes `camera`.

    load_config already validates this, so an unknown name here means a config
    edited by hand or a newer name on older code. Falling back to the camera's
    own look keeps a shoot running, and doing it in one place means the LUT
    path and the reconstructed path cannot disagree about what to fall back to.
    """
    name = str(profile or CAMERA_PROFILE).lower()
    if name == CAMERA_PROFILE or name in _CINE_PROFILES:
        return name
    logger.warning("unknown colour profile %r - using the camera's own look", profile)
    return CAMERA_PROFILE


def _profile_tone(profile: str | None) -> str:
    """The fixed tone stage for a non-camera profile, or "" for `camera`."""
    name = _normalise_profile(profile)
    return "" if name == CAMERA_PROFILE else _CINE_PROFILES[name]


def load_cine_look(input_path) -> dict | None:
    """The camera's colour description, written beside the clip at import time
    by the Phantom importer (`<clip>.look.json`). None when there isn't one."""
    try:
        path = Path(str(input_path)).with_suffix("")
        sidecar = Path(str(path) + LOOK_SUFFIX)
        if not sidecar.exists():
            sidecar = Path(str(input_path) + LOOK_SUFFIX)
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data.get("look") if isinstance(data, dict) else None
    except (OSError, ValueError):
        logger.warning("could not read the colour sidecar for %s", input_path)
        return None


def _levels_filter(look: dict | None) -> str:
    """Subtract the black reference so the picture actually reaches black.

    ffmpeg's cine decode hands back the sensor's codes untouched, pedestal and
    all: measured across six clips shot on two different days, the darkest pixel
    in a full 4096x2160 frame sits at 7283-7316 of 65535 - an 11.2% floor with a
    0.05% spread, i.e. a fixed offset, not scene content. PCC subtracts this
    before it does anything else; without it the tone curve's first segment
    (slope 4.17) multiplies the floor up to 28% grey and the picture reads flat.

    The floor comes from the sidecar, measured from the footage at import time
    (or from the .cine header when it reports a real sub-range) - never guessed
    here. No sidecar value means no filter and today's behaviour.

    Only the black point is touched. The sidecar also records `white_ceiling`,
    but that is just the brightest pixel this clip happens to contain: stretching
    to it adds contrast that varies shot to shot, so two takes of the same setup
    land differently. The floor is the opposite - a fixed offset with a 0.05%
    spread across six clips - so it is the half worth acting on.
    """
    lo = _as_float((look or {}).get("black_floor"), -1.0)
    if not 0.0 < lo < 0.9:
        return ""
    # colorlevels defaults the maxima to 1.0, so the highlights stay put.
    return f"colorlevels=rimin={lo:.6f}:gimin={lo:.6f}:bimin={lo:.6f}"


def _matrix_filter(look: dict | None) -> str:
    """The camera's colour correction matrix as an ffmpeg `colorchannelmixer`.

    `calib_matrix` is the factory characterisation of this sensor and
    `user_matrix` whatever the operator set on top; the camera applies both when
    `enable_matrices` is set. Skipping them is why rendered clips came out
    desaturated *and* green. Applied in linear light, before the tone curve,
    which is the order the camera uses.
    """
    look = look or {}
    if not look.get("enable_matrices"):
        return ""
    calib = _matrix3(look.get("calib_matrix"))
    user = _matrix3(look.get("user_matrix"))
    if calib is None and user is None:
        return ""
    m = _mat_mul(user, calib) if (calib and user) else (calib or user)
    # Identity contributes nothing but a filter stage and a rounding pass.
    ident = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    if all(abs(a - b) < 1e-4 for a, b in zip(m, ident)):
        return ""

    # Applied whole, white balance included. The matrix's rows do not sum to 1 -
    # on a tungsten-lit clip they sum to 1.26 / 1.15 / 1.76 - because Phantom
    # bakes the white balance into it. That lift is not an artefact to normalise
    # away: without it the render keeps the green cast of an unbalanced Bayer
    # decode, which is visible on any frame. (The separate `wb_red`/`wb_blue`
    # gains stay unapplied - see build_cine_source_filter - as the matrix has
    # already done that job.)
    #
    # ffmpeg caps colorchannelmixer coefficients at +-2 and the blue row exceeds
    # it, so the matrix is split into m/s followed by a uniform gain of s. The
    # two are exactly equivalent: m/s can only ever produce 1/s of the final
    # value, so the intermediate stage clips nothing the single stage wouldn't.
    scale = max(1.0, max(abs(v) for v in m) / 2.0)
    if scale > 2.0:
        # Would need a third stage, and by then the intermediate really would
        # clip. Not a matrix we understand; skip it rather than render wrong.
        logger.warning("colour matrix coefficients out of range - skipping it")
        return ""
    keys = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
    stages = ["colorchannelmixer=" + ":".join(
        f"{k}={v / scale:.6f}" for k, v in zip(keys, m))]
    if scale > 1.0 + 1e-9:
        stages.append(f"colorchannelmixer=rr={scale:.6f}:gg={scale:.6f}:bb={scale:.6f}")
    return ",".join(stages)


def _matrix3(values) -> list[float] | None:
    """Nine finite floats, row-major, or None. A short or malformed matrix is
    dropped rather than padded - a wrong matrix is worse than none."""
    if not isinstance(values, (list, tuple)) or len(values) < 9:
        return None
    try:
        m = [float(v) for v in values[:9]]
    except (TypeError, ValueError):
        return None
    if any(v != v or abs(v) > 16.0 for v in m):   # NaN or absurd
        return None
    return m


def _mat_mul(a: list[float], b: list[float]) -> list[float]:
    """Row-major 3x3 product a*b, i.e. b applied first."""
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3))
            for r in range(3) for c in range(3)]


def _tone_curve_filter(tone: dict | None) -> str:
    """The camera's tone curve as an ffmpeg `curves` filter.

    This is the LUT the camera records (TONEDESC: control points in 0..1). It
    *replaces* the gamma rather than stacking with it - applying both blows the
    picture out, which is verifiable on any clip.
    """
    pts = (tone or {}).get("points") or []
    clean: list[tuple[float, float]] = []
    for pair in pts:
        try:
            x, y = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < x < 1.0:
            clean.append((x, y))
    if not clean:
        return ""
    clean.sort()
    # curves needs the endpoints; duplicates on x make it reject the whole set.
    full = [(0.0, 0.0)] + clean + [(1.0, 1.0)]
    seen, uniq = set(), []
    for x, y in full:
        key = round(x, 6)
        if key not in seen:
            seen.add(key)
            uniq.append((x, y))
    return "curves=all='" + " ".join(f"{x:g}/{y:g}" for x, y in uniq) + "'"


# LUTs fitted against the Phantom SDK's own renderer by camera_bridge/fit_look.py.
# One per profile; a profile with no .cube falls back to the hand-built chain.
#
# Resolved the same way app.py resolves templates/static/logo: a PyInstaller
# build extracts to a temp dir rather than preserving the package layout, so
# __file__ points somewhere useless there and the executable's own folder is
# what tracks the install. Getting this wrong would not raise - the LUT would
# just never be found and every render would quietly drop to the fallback
# chain, which is ~10% off the SDK.
LOOKS_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent) / "looks"


def look_lut_path(profile: str | None) -> Path | None:
    """The fitted `.cube` for a profile, or None when there isn't one.

    These beat the hand-built chain by a wide margin - measured against the SDK
    on real footage, the reconstruction runs ~10% mean error while the LUT runs
    ~0.5% - because the LUT is fitted from what Phantom's own code produces
    rather than assembled from the header and hope.
    """
    # _normalise_profile only ever returns a name we ship, so a config value
    # cannot steer this at the filesystem.
    path = LOOKS_DIR / f"{_normalise_profile(profile)}.cube"
    return path if path.is_file() else None


def _escape_filter_path(path) -> str:
    r"""A Windows path an ffmpeg filter argument will accept, quotes included.

    Inside a filtergraph `:` separates options, so a drive letter splits the
    argument and ffmpeg reports "No option name near ...". Escaping the colon
    is not enough on its own and neither is quoting on its own - of the forms
    tested against this ffmpeg, only quoted *and* escaped parses:

        file='D\:/Glambot/looks/log1.cube'      works
        file=D\:/Glambot/looks/log1.cube        fails
        file=D\\:/Glambot/looks/log1.cube       fails
        file='D:/Glambot/looks/log1.cube'       fails

    Backslashes become forward slashes, which Windows accepts throughout.
    """
    p = str(path).replace("\\", "/")
    if "'" in p:
        # Nothing sane escapes a quote inside a quoted filter argument; a look
        # rendered from the wrong path would be worse than no look at all.
        raise ValueError(f"LUT path contains a quote, which ffmpeg cannot take: {p}")
    return "'" + p.replace(":", r"\:") + "'"


def _lut_filter(profile: str | None) -> str:
    path = look_lut_path(profile)
    if path is None:
        return ""
    # Linear interpolation, not the default: these LUTs are fitted samples of a
    # smooth response, and a spline through them can overshoot into banding.
    return f"lut1d=file={_escape_filter_path(path)}:interp=linear"


def build_cine_source_filter(input_path, ffprobe_bin: str, look: dict | None = None,
                             profile: str = CAMERA_PROFILE) -> str:
    """Reproduce the camera's own colour for a raw Phantom `.cine`.

    ffmpeg debayers the Bayer data but knows nothing about the camera's
    processing description, most of which isn't even exposed to ffprobe. When a
    `.look.json` sidecar is present we apply what the camera actually recorded,
    in the camera's own order: black reference, colour matrix, tone curve, then
    any non-neutral gain/offset/saturation.

    `profile` selects the look. When a fitted LUT exists for it (looks/*.cube,
    produced by camera_bridge/fit_look.py) the chain is just the cine's colour
    matrix and that LUT - the LUT already contains everything else Phantom does,
    measured from the SDK's own output rather than rebuilt from the header.

    Without a LUT it falls back to the reconstruction below: black reference,
    colour matrix, tone curve, then non-neutral gain/offset/saturation, with
    rec709/log1/log2 swapping the tone stage for a fixed curve of Glambot's own
    (see _CINE_PROFILES). That path is known to sit ~10% mean error from what
    the SDK produces, so it is a fallback and not the intent. The operator's
    grade composes after either.

    Two things this deliberately does NOT do, both established by measurement:

    * It does not apply the `wb_red`/`wb_blue` gains on top of the matrix. The
      decode is emphatically not neutral - it comes out green, as an unbalanced
      Bayer decode does - but the white balance for it is already carried in the
      colour matrix, whose rows sum to 1.26 / 1.15 / 1.76 rather than to 1.
      Applying the gains as well is a second white balance, which measured as a
      purple cast (B/G 1.37).
    * It does not apply the header's `gamma` on top of the tone curve. The
      curve already is a linear->display transform built around 2.2 - its
      `0.134 -> 0.400` point is exactly 0.134**(1/2.2) - so a second gamma
      double-encodes: measured, that takes the black floor to 0.808 and the
      highlights to 0.906, i.e. no picture left.

    Without a sidecar it falls back to the previous wbgain+gamma behaviour, so
    un-backfilled clips and non-Phantom footage are unchanged.

    Returns a comma-chained fragment (no pad labels) to prepend to `[0:v]`, or
    "" when the file carries no Phantom colour information at all. The
    operator's exposure / contrast / white-balance grade composes after this."""
    look = look if look is not None else load_cine_look(input_path)

    # Preferred path: the cine's own colour matrix, then a LUT fitted against
    # the SDK's renderer. The LUT absorbs everything else the camera does -
    # black reference, gamma, tone curve, gain/offset/saturation - so none of
    # it is reconstructed here, which is exactly why it lands within ~0.5% of
    # the SDK instead of ~10%.
    lut = _lut_filter(profile)
    matrix = _matrix_filter(look)
    if lut and matrix:
        return ",".join(["format=gbrp16le", matrix, lut])
    if lut:
        logger.info("%s has no colour matrix in its sidecar - falling back to the "
                    "reconstructed chain; run the Phantom colour backfill",
                    getattr(input_path, "name", input_path))

    fixed_tone = _profile_tone(profile)
    curve = fixed_tone or _tone_curve_filter((look or {}).get("tone"))
    # A fixed profile still needs the sidecar for the black reference and the
    # matrix, so it only takes this branch when there is a sidecar to read.
    if fixed_tone and not (look or {}).get("tone"):
        curve = ""
    if curve:
        # Order matters and mirrors the camera: subtract black, correct colour
        # in linear light, then encode with the tone curve. Each stage drops out
        # of the chain entirely when the sidecar has nothing to say about it.
        parts = ["format=gbrp16le"]
        for stage in (_levels_filter(look), _matrix_filter(look)):
            if stage:
                parts.append(stage)
        parts.append(curve)
        # Only what the camera actually set - these are all neutral on a stock
        # setup, so they usually contribute nothing. Skipped under a fixed
        # profile: those numbers belong to the camera's own look, and stacking
        # them onto a curve of ours would be applying half of each.
        eq = []
        if fixed_tone:
            look = {}
        gain = _as_float((look or {}).get("gain"), 1.0)
        offset = _as_float((look or {}).get("offset"), 0.0)
        sat = _as_float((look or {}).get("saturation"), 1.0)
        if abs(gain - 1.0) > 1e-3:
            eq.append(f"contrast={min(3.0, max(0.1, gain)):.4f}")
        if abs(offset) > 1e-3:
            eq.append(f"brightness={min(1.0, max(-1.0, offset)):.4f}")
        if abs(sat - 1.0) > 1e-3:
            eq.append(f"saturation={min(3.0, max(0.0, sat)):.4f}")
        if eq:
            parts.append("eq=" + ":".join(eq))
        return ",".join(parts)

    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_tags", "-of", "json", str(input_path)],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW_FLAGS,
        ).stdout
        tags = (json.loads(out).get("streams") or [{}])[0].get("tags", {})
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        # Silence here means the render still succeeds but comes out green and
        # flat, with no colour tagging - worth a line in the log.
        logger.warning("cine colour fix skipped: could not probe %s (%s)", input_path, exc)
        return ""

    try:
        r_gain = float(tags["wbgain[0].r"])
        b_gain = float(tags["wbgain[0].b"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "cine colour fix skipped: %s has no wbgain tags. Expected for non-Phantom "
            "footage; on a .cine it usually means the file isn't raw (check the Phantom "
            "import File type is SVV_RAWCINE) or ffmpeg lacks the Phantom SDK.",
            input_path)
        return ""

    # Legacy path: no sidecar, so we don't know the camera's curve. Kept
    # byte-identical to the previous behaviour rather than guessing, so an
    # un-backfilled clip renders as it always did. Run the backfill to get the
    # camera's real colour instead.
    logger.info("no colour sidecar for %s - using the legacy wbgain+gamma look; "
                "run the Phantom colour backfill to pick up the camera's own curve",
                getattr(input_path, "name", input_path))
    r_gain = min(4.0, max(0.2, r_gain))
    b_gain = min(4.0, max(0.2, b_gain))
    # `camera` has no meaning without a sidecar, so it falls back to rec709 -
    # which is byte-identical to the behaviour before profiles existed.
    tone = fixed_tone or _CINE_PROFILES["rec709"]
    return (
        f"format=gbrp16le,colorchannelmixer=rr={r_gain:.4f}:gg=1:bb={b_gain:.4f},"
        f"{tone}"
    )


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
