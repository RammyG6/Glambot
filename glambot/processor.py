"""Turn raw footage into a trimmed, overlaid, compressed clip via ffmpeg."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import (
    FOOTAGE_SUBDIR,
    THUMBNAIL_SUBDIR,
    ProjectConfig,
    managed_subdir_in,
)
from .effects import (
    build_cine_source_filter,
    build_grade_filter,
    build_speed_ramp_filtergraph,
    expected_ramp_duration,
)
from .db import Job, JobStore
from .drive import DriveError, upload_and_share
from .emailer import EmailError

logger = logging.getLogger(__name__)

# Suppresses the console window Windows otherwise pops up for every
# ffmpeg/ffprobe subprocess a windowed (no-console) process spawns - e.g.
# the packaged standalone app (windows_app/). `creationflags` is a valid
# Popen kwarg on every platform; CREATE_NO_WINDOW only exists as a
# subprocess attribute on Windows, so this is a no-op (0) elsewhere.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class OverlaySpec:
    """One overlay to composite onto a render. `path` of None means no overlay
    (the clip is processed without a logo)."""
    path: str | None
    position: str = "full"
    scale: int | None = None
    x: float | None = None
    y: float | None = None


def resolve_output_base(project_dir: Path, config: ProjectConfig) -> Path:
    """Base directory that holds the Footage/ and Thumbnail/ subfolders.
    Defaults to project_dir (today's behavior). When config.output_dir is
    set, relocates to <output_dir>/<project_dir.name>_Output, so the whole
    output lifecycle for that project lives under one relocated parent."""
    if config.output_dir:
        return (config.output_dir / f"{project_dir.name}_Output").resolve()
    return project_dir


def _overlay_spec_for(config: ProjectConfig, width: int, height: int) -> OverlaySpec:
    """Pick the overlay whose orientation matches this output: taller-than-wide
    -> the vertical overlay, otherwise the horizontal one. Both fall back to a
    legacy single overlay in load_config, so this is safe for old configs."""
    if height > width:
        return OverlaySpec(config.vertical_overlay, config.vertical_overlay_position,
                           config.vertical_overlay_scale, config.vertical_overlay_x,
                           config.vertical_overlay_y)
    return OverlaySpec(config.horizontal_overlay, config.horizontal_overlay_position,
                       config.horizontal_overlay_scale, config.horizontal_overlay_x,
                       config.horizontal_overlay_y)


def _primary_overlay_spec(config: ProjectConfig) -> OverlaySpec:
    return _overlay_spec_for(config, config.width, config.height)


def _second_overlay_spec(config: ProjectConfig) -> OverlaySpec:
    return _overlay_spec_for(config, config.second_width or config.width,
                             config.second_height or config.height)

# .mxf (Sony), .cine (Phantom high-speed), .braw (Blackmagic RAW) are accepted
# here, but stock ffmpeg can only demux .mxf out of the box - .cine/.braw
# decoding requires a specially-built ffmpeg/SDK plugin the user must supply;
# without one, those jobs fail cleanly into `error` status via process_job().
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mxf", ".cine", ".braw"}

_OVERLAY_MARGIN = 20
THUMBNAIL_SUFFIX = ".jpg"

_ROTATION_FILTERS = {
    0: "",
    90: "transpose=1,",
    -90: "transpose=2,",
    180: "hflip,vflip,",
}


def is_footage_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


# How much of each end of a file feeds the content hash. Video files differ
# in their first megabyte (container header, moov atom, first frames) far
# more reliably than they collide in it, so sampling the ends plus the exact
# byte length identifies a clip without reading gigabytes off disk.
_HASH_SAMPLE_BYTES = 1024 * 1024


def content_hash(path: Path) -> str | None:
    """Identify footage by its bytes rather than its path, so the same clip
    arriving somewhere new isn't mistaken for a different one.

    Hashes the file's length plus its first and last megabyte. Full-file
    hashing would mean reading every byte of multi-GB footage before any
    render could start; this is fast enough to sit inline in the watcher.
    Returns None if the file can't be read, in which case callers fall back
    to path-based dedup rather than blocking the clip."""
    try:
        size = path.stat().st_size
        digest = hashlib.sha256(str(size).encode())
        with open(path, "rb") as handle:
            digest.update(handle.read(_HASH_SAMPLE_BYTES))
            if size > _HASH_SAMPLE_BYTES * 2:
                handle.seek(-_HASH_SAMPLE_BYTES, os.SEEK_END)
                digest.update(handle.read(_HASH_SAMPLE_BYTES))
        return digest.hexdigest()
    except OSError:
        logger.warning("Could not hash %s for duplicate detection", path, exc_info=True)
        return None


# ffprobe (used for the progress bar's total duration and audio-stream
# detection) is NOT bundled by imageio-ffmpeg — resolve a system one if
# present, else (for a packaged .exe) a copy vendored alongside the frozen
# build, else fall back to the bare name (the callers degrade gracefully
# when none of those are available).
def _resolve_ffprobe() -> str:
    found = shutil.which("ffprobe")
    if found:
        return found
    if getattr(sys, "frozen", False):
        vendored = Path(sys.executable).resolve().parent / "vendor" / "ffprobe.exe"
        if vendored.exists():
            return str(vendored)
    return "ffprobe"


_FFPROBE = _resolve_ffprobe()

_ffmpeg_path: str | None = None
_ffmpeg_resolved = False

# Lets a Flask request thread reach and kill a render running on the
# watcher's worker thread (or a rerender/bulk-retry background thread) -
# there's otherwise no handle any other part of the app has on that specific
# ffmpeg subprocess.
_active_lock = threading.Lock()
_active_processes: dict[int, subprocess.Popen] = {}
_cancelled_jobs: set[int] = set()


def cancel_job(job_id: int) -> bool:
    """Ask a currently-running render to stop. Returns True if a live
    process was found and signalled (process_job will mark the job stopped
    once ffmpeg actually exits), False if nothing is running for this job id
    right now (already finished, or hasn't started its ffmpeg pass yet)."""
    with _active_lock:
        proc = _active_processes.get(job_id)
        if proc is None:
            return False
        _cancelled_jobs.add(job_id)
    proc.terminate()
    return True


def stop_all_active() -> None:
    """Terminate every ffmpeg render currently in flight. For a clean process
    exit (e.g. the packaged Windows app's tray Quit / window-close) - Windows
    doesn't kill child processes when a parent exits, so skipping this would
    leave an orphaned ffmpeg.exe still encoding in the background."""
    with _active_lock:
        procs = list(_active_processes.values())
    for proc in procs:
        proc.terminate()


def _pop_cancelled(job_id: int) -> bool:
    with _active_lock:
        if job_id in _cancelled_jobs:
            _cancelled_jobs.discard(job_id)
            return True
        return False


def _cleanup_partial(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _cleanup_filtergraph(output_path: Path | None) -> None:
    """Remove the sidecar filtergraph file build_ffmpeg_cmd may have written
    for a very long speed-ramp expression."""
    if output_path is None:
        return
    try:
        output_path.with_suffix(output_path.suffix + ".filtergraph").unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_ffmpeg() -> str | None:
    """Prefer a system ffmpeg on PATH; otherwise fall back to the static
    binary bundled by the imageio-ffmpeg package — works identically on
    macOS/Windows/Linux, no PATH or symlink setup required."""
    global _ffmpeg_path, _ffmpeg_resolved
    if _ffmpeg_resolved:
        return _ffmpeg_path
    _ffmpeg_resolved = True
    _ffmpeg_path = shutil.which("ffmpeg")
    if _ffmpeg_path:
        return _ffmpeg_path
    try:
        import imageio_ffmpeg
        _ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _ffmpeg_path = None
    return _ffmpeg_path


def _has_audio_stream(path: Path) -> bool:
    """Best-effort probe for an audio stream. Defaults to True (assume audio
    present) when ffprobe is unavailable/fails — referencing a nonexistent
    stream makes ffmpeg fail loudly, which is safer than silently dropping
    real audio by guessing the opposite way."""
    try:
        result = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW_FLAGS,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        return True


def _overlay_filter(w: int, h: int, rotation: int, position_x: int, position_y: int,
                     spec: OverlaySpec, overlay_index: int | None,
                     src_label: str = "[0:v]", grade_filter: str = "") -> str:
    """Return the filter_complex video fragment: rotate/pan the source, scale/
    crop to WxH, apply the optional colour grade, then (if `spec.path`)
    composite the overlay from input `overlay_index`. Ends in output pad [v].

    `src_label` is the input pad to read from - `[0:v]` normally, or the output
    of the speed-ramp fragment when one is active.
    """
    rotate = _ROTATION_FILTERS[rotation]
    grade_suffix = f",{grade_filter}" if grade_filter else ""
    # Pan the center-crop window by position_x/position_y (pixels) instead of
    # always cropping dead-center; min(max(...)) clamps the offset so it can
    # never push the crop window outside the actual (per-source, unknown
    # until runtime) scaled frame — ffmpeg evaluates these expressions itself.
    # Single-quoted: the expressions contain commas, which ffmpeg's
    # filtergraph parser would otherwise treat as filter-chain separators.
    x_expr = f"'min(max(0,(in_w-out_w)/2+({position_x})),in_w-out_w)'"
    y_expr = f"'min(max(0,(in_h-out_h)/2+({position_y})),in_h-out_h)'"
    bg_chain = (
        f"{src_label}{rotate}scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:{x_expr}:{y_expr},setsar=1{grade_suffix}"
    )

    if not spec.path or overlay_index is None:
        # No overlay — the background chain IS the output.
        return f"{bg_chain}[v]"

    bg = f"{bg_chain}[bg]"
    ov_in = f"[{overlay_index}:v]"

    # overlay_scale (% of frame width) overrides the per-position default
    # sizing below; without it, `full` fills the frame and corner/custom
    # positions keep the overlay at its native pixel size (legacy behavior).
    if spec.scale is not None:
        target_w = max(1, round(w * spec.scale / 100))
        ov = f"{ov_in}scale={target_w}:-1[ov]"
    elif spec.position == "full":
        ov = f"{ov_in}scale={w}:{h}[ov]"
    else:
        ov = f"{ov_in}copy[ov]"

    # shortest=1 is essential here: the overlay image is a `-loop 1` input
    # with no natural end, and the `overlay` filter's default eof_action is
    # to repeat its last frame forever once the *other* input (the actual
    # clip) ends — without shortest=1, [v] would never reach EOF on its own,
    # and -shortest at the output level can't reliably bound it either once
    # any other mapped stream (e.g. a filtered soundtrack mix) has a
    # different natural length.
    if spec.position == "custom":
        x = f"W*{spec.x / 100:.6f}"
        y = f"H*{spec.y / 100:.6f}"
        return f"{bg};{ov};[bg][ov]overlay={x}:{y}:format=auto:shortest=1[v]"

    if spec.position == "full" and spec.scale is None:
        return f"{bg};{ov};[bg][ov]overlay=0:0:format=auto:shortest=1[v]"

    positions = {
        "full": ("0", "0"),
        "top-left": (str(_OVERLAY_MARGIN), str(_OVERLAY_MARGIN)),
        "top-right": (f"W-w-{_OVERLAY_MARGIN}", str(_OVERLAY_MARGIN)),
        "bottom-left": (str(_OVERLAY_MARGIN), f"H-h-{_OVERLAY_MARGIN}"),
        "bottom-right": (f"W-w-{_OVERLAY_MARGIN}", f"H-h-{_OVERLAY_MARGIN}"),
    }
    x, y = positions[spec.position]
    return f"{bg};{ov};[bg][ov]overlay={x}:{y}:format=auto:shortest=1[v]"


def build_ffmpeg_cmd(input_path: Path, output_path: Path, config: ProjectConfig,
                      trim_start: str | None, trim_end: str | None, ffmpeg_bin: str = "ffmpeg",
                      width: int | None = None, height: int | None = None,
                      bitrate: str | None = None, overlay_spec: OverlaySpec | None = None,
                      grade=None, speed_ramp=None, src_duration: float | None = None,
                      source_fps: float | None = None) -> list[str]:
    w = width if width is not None else config.width
    h = height if height is not None else config.height
    bitrate = bitrate if bitrate is not None else config.bitrate
    if overlay_spec is None:
        overlay_spec = _primary_overlay_spec(config)

    def _fps_str(v):
        return str(int(v)) if float(v) == int(v) else f"{float(v):g}"

    cmd = [ffmpeg_bin, "-y"]
    # Reinterpret raw footage at the wanted playback rate (a Phantom .cine's
    # header stores its high-speed capture rate). Must precede -i.
    if source_fps:
        cmd += ["-r", _fps_str(source_fps)]
    if trim_start:
        cmd += ["-ss", trim_start]
    if trim_end:
        cmd += ["-to", trim_end]
    cmd += ["-i", str(input_path)]

    # Raw Phantom .cine: neutralise the green/flat Bayer decode before anything
    # else touches the picture (so the speed ramp + grade see real colour).
    cine_fix = ""
    if input_path.suffix.lower() == ".cine":
        cine_fix = build_cine_source_filter(input_path, _FFPROBE, config.color_profile)

    # Input indices are assigned dynamically: [0]=video always; the overlay
    # (if any) is [1]; the soundtrack (if any) is whatever comes next.
    next_index = 1
    overlay_index = None
    if overlay_spec.path:
        cmd += ["-loop", "1", "-i", overlay_spec.path]
        overlay_index = next_index
        next_index += 1

    grade_filter = build_grade_filter(grade, ffmpeg_bin)

    video_in = "[0:v]"
    pre_fragment = ""
    if cine_fix:
        pre_fragment = f"[0:v]{cine_fix}[cv]"
        video_in = "[cv]"

    # Speed ramp: retime the source along the curve first, then feed the result
    # into the normal scale/crop/grade/overlay chain. Original audio is dropped
    # on a ramped clip (only the soundtrack, at normal speed, survives).
    ramp_fragment = ""
    src_label = video_in
    ramped = False
    if speed_ramp is not None:
        if src_duration is None:
            src_duration = _effective_duration(input_path, trim_start, trim_end, source_fps)
        ramp_fragment, src_label, _ = build_speed_ramp_filtergraph(speed_ramp, video_in, src_duration)
        ramped = bool(ramp_fragment)

    filter_complex = _overlay_filter(w, h, config.rotation, config.position_x, config.position_y,
                                     overlay_spec, overlay_index, src_label=src_label,
                                     grade_filter=grade_filter)
    if ramp_fragment:
        filter_complex = f"{ramp_fragment};{filter_complex}"
    if pre_fragment:
        filter_complex = f"{pre_fragment};{filter_complex}"

    audio_mapped = False
    audio_none = False
    if config.soundtrack:
        if config.soundtrack_trim and config.soundtrack_trim.start:
            cmd += ["-ss", config.soundtrack_trim.start]
        if config.soundtrack_trim and config.soundtrack_trim.end:
            cmd += ["-to", config.soundtrack_trim.end]
        cmd += ["-i", config.soundtrack]
        snd_index = next_index

        if not ramped and _has_audio_stream(input_path):
            filter_complex += (
                f";[0:a]volume={config.original_volume_db}dB[origa]"
                f";[{snd_index}:a]volume={config.soundtrack_volume_db}dB[snda]"
                f";[origa][snda]amix=inputs=2:duration=first:dropout_transition=0[a]"
            )
        else:
            filter_complex += f";[{snd_index}:a]volume={config.soundtrack_volume_db}dB[a]"
        audio_mapped = True
    elif ramped:
        # Ramped, no soundtrack - there is no sensible audio to keep.
        audio_none = True

    # A long speed-ramp setpts expression can blow the OS command-line limit;
    # hand it to ffmpeg as a sidecar file instead. Cleaned up by the caller.
    if len(filter_complex) > 7000:
        script_path = output_path.with_suffix(output_path.suffix + ".filtergraph")
        script_path.write_text(filter_complex, encoding="utf-8")
        cmd += ["-filter_complex_script", str(script_path)]
    else:
        cmd += ["-filter_complex", filter_complex]
    cmd += ["-map", "[v]"]
    if audio_mapped:
        cmd += ["-map", "[a]"]
    elif audio_none:
        cmd += ["-an"]
    else:
        cmd += ["-map", "0:a?"]

    # Network/streaming-preferred output: yuv420p + a broadly-decodable
    # profile/level plus a regular closed GOP reduce the re-encoding work
    # Google Drive's backend has to do before a clip is smoothly previewable.
    # Level 5.1 (rather than the more commonly cited 4.1) is needed because
    # this app's presets go up to 3840x2160 at 60fps, which 4.1 doesn't cover.
    out_fps = config.fps or source_fps
    effective_fps = out_fps or _probe_fps(input_path) or 30
    gop = max(1, round(2 * effective_fps))
    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-level", "5.1", "-pix_fmt", "yuv420p",
        "-b:v", bitrate, "-preset", "medium",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
    ]
    if cine_fix:
        # A raw .cine carries no colour signalling; tag the output like the
        # working MXF path does so players don't guess.
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv"]
    if out_fps is not None:
        cmd += ["-r", _fps_str(out_fps)]
    cmd += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    return cmd


def _probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW_FLAGS,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


# Footage whose reported frame rate is above this is treated as high-speed
# capture (a Phantom .cine, phone slo-mo, ...) and played back at the default
# rate unless the project sets its own "Source frame rate".
_HIGH_SPEED_FPS = 60.0
_DEFAULT_PLAYBACK_FPS = float(os.environ.get("GLAMBOT_DEFAULT_PLAYBACK_FPS", "25") or 25)


def _effective_source_fps(path: Path, configured: float | None) -> float | None:
    """The rate to reinterpret the source at: the project's explicit setting,
    else the default playback rate for footage that reports a high capture
    rate, else None (trust the file)."""
    if configured:
        return float(configured)
    rate = _probe_fps(path)
    return _DEFAULT_PLAYBACK_FPS if (rate and rate > _HIGH_SPEED_FPS) else None


def _source_duration(path: Path, source_fps: float | None) -> float | None:
    """The clip's true length. When `source_fps` is set (raw footage whose
    header lies about its rate - a Phantom .cine reports its capture rate) the
    container duration is wrong, so scale it: reported * reported_fps / wanted."""
    reported = _probe_duration(path)
    if not source_fps or source_fps <= 0 or reported is None:
        return reported
    rfps = _probe_fps(path)
    if not rfps:
        return reported
    return reported * rfps / source_fps


def _probe_fps(path: Path) -> float | None:
    """Best-effort source framerate probe, used to size the GOP/keyframe
    interval when a project doesn't force an explicit fps. Approximate for
    variable-frame-rate sources - that only shifts the keyframe interval
    slightly, it doesn't break the closed-GOP guarantee sc_threshold=0
    provides."""
    try:
        result = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW_FLAGS,
        )
        num, _, den = result.stdout.strip().partition("/")
        den = den or "1"
        return float(num) / float(den) if float(den) else None
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


def _timestamp_to_seconds(value: str | None) -> float | None:
    """Parse a trim timestamp (HH:MM:SS[.frac] or a plain number of seconds)."""
    if not value:
        return None
    value = value.strip()
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            secs = 0.0
            for p in parts:
                secs = secs * 60 + p
            return secs
        return float(value)
    except ValueError:
        return None


def _effective_duration(source_path: Path, trim_start: str | None, trim_end: str | None,
                        source_fps: float | None = None) -> float | None:
    """How many seconds of source this render will consume, for progress % and
    the speed-ramp timeline. Best-effort — returns None if it can't be
    determined (bar goes indeterminate). Trim timestamps are in the same
    (possibly reinterpreted, see `source_fps`) timeline as `-ss`/`-to`."""
    start = _timestamp_to_seconds(trim_start)
    end = _timestamp_to_seconds(trim_end)
    if start is not None and end is not None:
        return max(0.01, end - start)
    source_dur = _source_duration(source_path, source_fps)
    if source_dur is None:
        return None
    if end is not None:
        return max(0.01, end)
    if start is not None:
        return max(0.01, source_dur - start)
    return source_dur


def _run_ffmpeg_with_progress(cmd: list[str], total_seconds: float | None, on_progress,
                               job_id: int | None = None):
    """Run an ffmpeg render via Popen, streaming its live -progress output and
    calling on_progress(percent 0-100) as it advances. Returns
    (returncode, stderr_tail). on_progress is only called when the integer
    percent changes, so it stays cheap even for long clips.

    When job_id is given, the running process is registered so cancel_job()
    can reach and terminate it from another thread (e.g. a Flask request)."""
    # -progress writes machine-readable key=value lines to stdout; -nostats
    # silences the usual human progress spam on stderr.
    full_cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(
        full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=_NO_WINDOW_FLAGS,
    )
    assert proc.stdout is not None and proc.stderr is not None

    if job_id is not None:
        with _active_lock:
            _active_processes[job_id] = proc

    try:
        # ffmpeg writes stream/filter info and warnings to stderr throughout
        # the run. If we only read stdout (the -progress stream) and leave
        # stderr unread, ffmpeg blocks once stderr fills the OS pipe buffer —
        # a deadlock that "sticks" processing forever (small Windows pipe
        # buffers hit this readily). Drain stderr on a background thread so
        # it can never fill up.
        stderr_chunks: list[str] = []

        def _drain_stderr():
            for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        last_pct = -1
        for line in proc.stdout:
            line = line.strip()
            micros = None
            if line.startswith("out_time_us="):
                raw = line.split("=", 1)[1]
                micros = float(raw) if raw not in ("N/A", "") else None
            elif line.startswith("out_time_ms="):
                # some ffmpeg builds mislabel this field but it's microseconds too
                raw = line.split("=", 1)[1]
                micros = float(raw) if raw not in ("N/A", "") else None
            if micros is not None and total_seconds:
                pct = int(min(100, max(0, micros / 1_000_000 / total_seconds * 100)))
                if pct != last_pct:
                    last_pct = pct
                    on_progress(pct)
        proc.wait()
        stderr_thread.join(timeout=5)
        tail = "\n".join("".join(stderr_chunks).strip().splitlines()[-20:])
        return proc.returncode, tail
    finally:
        if job_id is not None:
            with _active_lock:
                _active_processes.pop(job_id, None)


def _make_thumbnail(output_path: Path, thumbnail_path: Path, ffmpeg_bin: str) -> bool:
    """Grab a representative frame at 75% of the finished clip's duration,
    for the kiosk QR screen. Runs on the already-rendered, already-trimmed
    output, so this is 75% of the final post-trim duration. Best-effort: a
    failure here must not fail the job."""
    duration = _probe_duration(output_path) or 2.0
    offset = max(0.1, duration * 0.75)
    cmd = [
        ffmpeg_bin, "-y", "-ss", f"{offset:.2f}", "-i", str(output_path),
        "-frames:v", "1", "-q:v", "3", str(thumbnail_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW_FLAGS)
    except FileNotFoundError:
        return False
    return result.returncode == 0 and thumbnail_path.exists()


# A render shorter than this fraction of what was asked for is treated as
# truncated. Encoders legitimately land a little short (frame-rate rounding,
# a dropped trailing partial frame), so the tolerance is generous — this is
# looking for a render that stopped early, not for a rounding error.
_DURATION_TOLERANCE = 0.05
_MIN_OUTPUT_BYTES = 1024


def verify_output(output_path: Path, expected_duration: float | None) -> tuple[bool, str]:
    """Check a finished render is actually playable before anything acts on
    it. Returns (ok, reason); `reason` is empty when ok.

    A killed or stalled ffmpeg still leaves a file behind, and an mp4
    truncated mid-write opens fine in some players — so file existence alone
    proves nothing. Probing the duration is what distinguishes a complete
    render from one that stopped early, and it reads headers rather than
    decoding, so it costs milliseconds."""
    if not output_path.exists():
        return False, "render produced no output file"
    size = output_path.stat().st_size
    if size < _MIN_OUTPUT_BYTES:
        return False, f"render produced an empty output file ({size} bytes)"

    actual = _probe_duration(output_path)
    if actual is None:
        # ffprobe missing is not the file's fault; ffprobe *failing* on a
        # file that exists means it isn't valid video.
        if shutil.which(_FFPROBE) is None and not Path(_FFPROBE).exists():
            logger.warning("ffprobe unavailable - skipping integrity check for %s", output_path)
            return True, ""
        return False, "rendered file is not readable as video (ffprobe could not parse it)"
    if actual <= 0:
        return False, "rendered file has zero duration"

    if expected_duration:
        shortfall = expected_duration - actual
        allowed = max(1.0, expected_duration * _DURATION_TOLERANCE)
        if shortfall > allowed:
            return False, (
                f"render is truncated: {actual:.1f}s of an expected "
                f"{expected_duration:.1f}s"
            )
    return True, ""


def _resolve_source(job, config: ProjectConfig) -> Path:
    """Where this job's source footage actually is right now. After a
    successful render the original is moved into Footage/, but the DB keeps the
    original import path - so a Re-render / per-clip override falls back to the
    archived copy. `_archive_original` archives next to the *import* folder;
    older/default-output projects archive next to the output base - check both."""
    p = Path(job.source_path)
    if p.exists():
        return p
    name = Path(job.filename).name
    project_dir = (config.project_dir or p.parent).resolve()
    for archived in (p.parent / FOOTAGE_SUBDIR / name,
                     resolve_output_base(project_dir, config) / FOOTAGE_SUBDIR / name):
        if archived.exists():
            return archived
    return p


def source_available(job, config: ProjectConfig) -> bool:
    """True if _resolve_source() would find a real file to re-render from -
    the eligibility the Re-render button / route should use so they agree with
    what process_job can actually do."""
    return _resolve_source(job, config).exists()


def _archive_original(source_path: Path) -> None:
    """Move a processed original out of its import folder into the
    "Footage" subfolder (excluded from watching), alongside the rendered
    output. Best-effort — a failed move must never fail the job."""
    try:
        if not source_path.exists():
            return
        if managed_subdir_in(source_path):
            # Already archived (e.g. this was a re-render from Footage/).
            return
        archive_dir = source_path.parent / FOOTAGE_SUBDIR
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / source_path.name
        if dest.exists():
            dest = archive_dir / f"{source_path.stem}_{uuid4().hex[:8]}{source_path.suffix}"
        shutil.move(str(source_path), str(dest))
        logger.info("Archived original %s -> %s", source_path, dest)
    except Exception:
        logger.warning("Could not archive original %s", source_path, exc_info=True)


_FFMPEG_MISSING_MESSAGE = (
    "ffmpeg not found — install it (e.g. `brew install ffmpeg` on macOS, or add it to PATH "
    "on Windows) or reinstall dependencies so the bundled fallback is available"
)


def process_job(job: Job, config: ProjectConfig, store: JobStore) -> None:
    """Run ffmpeg for a single job and update its status in the store."""
    ffmpeg_bin = _resolve_ffmpeg()
    if ffmpeg_bin is None:
        store.mark_error(job.id, _FFMPEG_MISSING_MESSAGE)
        return

    source_path = _resolve_source(job, config)
    project_dir = (config.project_dir or source_path.parent).resolve()
    output_base = resolve_output_base(project_dir, config)
    footage_dir = output_base / FOOTAGE_SUBDIR
    thumb_dir = output_base / THUMBNAIL_SUBDIR
    try:
        footage_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        store.mark_error(job.id, f"Output location not accessible: {exc}")
        return
    # Always store an absolute path: it's persisted in the DB and later read
    # back by send_file()/shutil.move() in a process that may have a
    # different working directory than the one that created this job.
    output_path = (footage_dir / (source_path.stem + ".mp4")).resolve()

    trim = config.trim_for(job.filename)
    grade = config.grade_for(job.filename)
    ramp = config.speed_ramp_for(job.filename)
    eff_src_fps = _effective_source_fps(source_path, config.source_fps)
    if eff_src_fps and not config.source_fps:
        logger.info(
            "Job %s: %s reports a high capture rate; playing it back at %s fps "
            "(set the project's Source frame rate to override)",
            job.id, source_path.name, eff_src_fps,
        )
    src_duration = _effective_duration(source_path, trim.start, trim.end, eff_src_fps)
    # Progress + integrity checks measure the OUTPUT, which a speed ramp makes
    # shorter/longer than the source.
    duration = src_duration
    if ramp is not None and src_duration:
        duration = expected_ramp_duration(ramp, src_duration)
    # One bar spanning every render pass: pass p of n fills the bar from
    # p/n to (p+1)/n as that pass runs.
    num_passes = 2 if config.second_resolution else 1

    def _progress_cb(pass_index):
        return lambda pct: store.set_progress(
            job.id, int((pass_index + pct / 100) / num_passes * 100)
        )

    store.set_progress(job.id, 0)
    cmd = build_ffmpeg_cmd(source_path, output_path, config, trim.start, trim.end, ffmpeg_bin=ffmpeg_bin,
                           overlay_spec=_primary_overlay_spec(config),
                           grade=grade, speed_ramp=ramp, src_duration=src_duration,
                           source_fps=eff_src_fps)
    logger.info("Processing job %s: %s", job.id, " ".join(cmd))

    try:
        returncode, tail = _run_ffmpeg_with_progress(cmd, duration, _progress_cb(0), job_id=job.id)
    except FileNotFoundError:
        store.mark_error(job.id, _FFMPEG_MISSING_MESSAGE)
        return
    finally:
        _cleanup_filtergraph(output_path)

    if returncode != 0:
        if _pop_cancelled(job.id):
            _cleanup_partial(output_path)
            store.mark_error(job.id, "Stopped by operator.")
        else:
            logger.error("ffmpeg failed for job %s: %s", job.id, tail)
            store.mark_error(job.id, f"ffmpeg failed: {tail}")
        return

    ok, reason = verify_output(output_path, duration)
    if not ok:
        logger.error("Integrity check failed for job %s: %s", job.id, reason)
        # Deliberately NOT archiving the original: leaving it in the import
        # folder is what makes the "Re-render" button in the review UI able
        # to run this job again from source.
        store.mark_error(job.id, reason)
        return

    secondary_output_path: Path | None = None
    if config.second_resolution:
        secondary_output_path = (
            footage_dir / f"{source_path.stem}_{config.second_resolution}.mp4"
        ).resolve()
        second_cmd = build_ffmpeg_cmd(
            source_path, secondary_output_path, config, trim.start, trim.end, ffmpeg_bin=ffmpeg_bin,
            width=config.second_width, height=config.second_height,
            bitrate=config.second_bitrate or config.bitrate,
            overlay_spec=_second_overlay_spec(config),
            grade=grade, speed_ramp=ramp, src_duration=src_duration,
            source_fps=eff_src_fps,
        )
        logger.info("Processing job %s (second resolution): %s", job.id, " ".join(second_cmd))
        try:
            second_returncode, second_tail = _run_ffmpeg_with_progress(
                second_cmd, duration, _progress_cb(1), job_id=job.id
            )
        except FileNotFoundError:
            second_returncode, second_tail = 1, "ffmpeg not found"
        finally:
            _cleanup_filtergraph(secondary_output_path)
        if second_returncode != 0:
            if _pop_cancelled(job.id):
                # Stopping mid-second-pass aborts the whole job, not just
                # the second resolution - otherwise it would silently finish
                # with just the primary output, which isn't what "Stop"
                # means to the operator.
                _cleanup_partial(output_path)
                _cleanup_partial(secondary_output_path)
                store.mark_error(job.id, "Stopped by operator.")
                return
            logger.error("Second-resolution ffmpeg failed for job %s: %s", job.id, second_tail)
            # Non-fatal: the primary output is still good, just drop the second one.
            secondary_output_path = None
        else:
            second_ok, second_reason = verify_output(secondary_output_path, duration)
            if not second_ok:
                # Same reasoning as above — a bad second render must not sink
                # a good primary one, so drop it rather than failing the job.
                logger.error("Second-resolution integrity check failed for job %s: %s",
                             job.id, second_reason)
                secondary_output_path = None

    thumbnail_path = (thumb_dir / (source_path.stem + THUMBNAIL_SUFFIX)).resolve()
    thumb_ok = _make_thumbnail(output_path, thumbnail_path, ffmpeg_bin)
    if not thumb_ok:
        logger.warning("Thumbnail generation failed for job %s; kiosk view will show no preview image", job.id)

    store.mark_ready(
        job.id, str(output_path),
        thumbnail_path=str(thumbnail_path) if thumb_ok else None,
        secondary_output_path=str(secondary_output_path) if secondary_output_path else None,
        duration_seconds=_probe_duration(output_path),
    )
    logger.info("Job %s ready: %s", job.id, output_path)

    # Clip is processed — move the original out of the import folder so it's
    # archived and never reprocessed. The DB keeps the original source_path
    # for identity; nothing downstream needs the source file.
    _archive_original(source_path)

    job = store.get_job(job.id)

    if config.delivery_mode == "qr_only" and not config.auto_deliver and not config.offline_mode:
        # Upload to Drive right away so the download link is already sitting
        # there by the time an operator clicks Approve at a live event — no
        # upload wait in front of the client. A failure here is non-fatal:
        # the job still reaches `ready` and Approve will retry the upload.
        folder_id = config.drive_folder_id or os.environ.get("DRIVE_FOLDER_ID", "")
        try:
            link = upload_and_share(output_path, folder_id)
            store.update_job(job.id, drive_link=link)
        except DriveError as exc:
            logger.warning("Auto-upload failed for job %s: %s", job.id, exc)
            store.update_job(job.id, error=f"Drive pre-upload failed (will retry on Approve): {exc}")

    if config.auto_deliver:
        # Full automation: run the exact same upload/email/QR delivery a
        # human would trigger via Approve, right now, using the config's
        # default recipient + the default email template. A failure here
        # is non-fatal — the job simply stays `ready` with an error note,
        # same as any other failed delivery, so it can be approved manually.
        from .delivery import DeliveryError, deliver
        from .emailer import load_default_template, resolve_placeholders

        subject, body = "", ""
        try:
            raw_subject, raw_body = load_default_template()
            # Project's own template overrides the global default.
            raw_subject = config.email_subject or raw_subject
            raw_body = config.email_body or raw_body
            subject = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
            body = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
        except Exception:
            logger.warning("Could not load default email template for auto-delivery of job %s", job.id)

        job = store.get_job(job.id)
        try:
            deliver(job, config, store, project_dir.parent, recipient=config.recipient_email,
                    subject=subject, body=body, delivery_mode=config.delivery_mode)
        except (DriveError, EmailError, DeliveryError) as exc:
            logger.exception("Auto-delivery failed for job %s", job.id)
            store.update_job(job.id, error=f"Auto-delivery failed: {exc}")
