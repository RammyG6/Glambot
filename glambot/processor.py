"""Turn raw footage into a trimmed, overlaid, compressed clip via ffmpeg."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import ProjectConfig
from .db import Job, JobStore
from .drive import DriveError, upload_and_share
from .emailer import EmailError

logger = logging.getLogger(__name__)


@dataclass
class OverlaySpec:
    """One overlay to composite onto a render. `path` of None means no overlay
    (the clip is processed without a logo)."""
    path: str | None
    position: str = "full"
    scale: int | None = None
    x: float | None = None
    y: float | None = None


def _primary_overlay_spec(config: ProjectConfig) -> OverlaySpec:
    return OverlaySpec(config.overlay, config.overlay_position, config.overlay_scale,
                       config.overlay_x, config.overlay_y)


def _second_overlay_spec(config: ProjectConfig) -> OverlaySpec:
    return OverlaySpec(config.second_overlay, config.second_overlay_position,
                       config.second_overlay_scale, config.second_overlay_x, config.second_overlay_y)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

OUTPUT_SUBDIR = "Output_ReadytoSend"
SENT_SUBDIR = "Email Sent File"

# QR-only ("Instant Download" / kiosk) projects use a separate on-disk layout
# from email-mode projects.
QR_OUTPUT_SUBDIR = "Output"
QR_APPROVED_SUBDIR = "Selected Output"
QR_DOWNLOAD_SUBDIR = "Instant Download"

# Where processed originals are moved to, inside their own import folder.
EDITED_FOOTAGES_SUBDIR = "Edited Footages"

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


# ffprobe (used for the progress bar's total duration and audio-stream
# detection) is NOT bundled by imageio-ffmpeg — resolve a system one if
# present, else fall back to the bare name (the callers degrade gracefully
# when it's absent).
_FFPROBE = shutil.which("ffprobe") or "ffprobe"

_ffmpeg_path: str | None = None
_ffmpeg_resolved = False


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
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return True
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        return True


def _overlay_filter(w: int, h: int, rotation: int, position_x: int, position_y: int,
                     spec: OverlaySpec, overlay_index: int | None) -> str:
    """Return the filter_complex video fragment: rotate/pan the source, scale/
    crop to WxH, then (if `spec.path`) composite the overlay from input
    `overlay_index`. Ends in output pad [v]."""
    rotate = _ROTATION_FILTERS[rotation]
    # Pan the center-crop window by position_x/position_y (pixels) instead of
    # always cropping dead-center; min(max(...)) clamps the offset so it can
    # never push the crop window outside the actual (per-source, unknown
    # until runtime) scaled frame — ffmpeg evaluates these expressions itself.
    # Single-quoted: the expressions contain commas, which ffmpeg's
    # filtergraph parser would otherwise treat as filter-chain separators.
    x_expr = f"'min(max(0,(in_w-out_w)/2+({position_x})),in_w-out_w)'"
    y_expr = f"'min(max(0,(in_h-out_h)/2+({position_y})),in_h-out_h)'"
    bg_chain = (
        f"[0:v]{rotate}scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:{x_expr}:{y_expr},setsar=1"
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
                      bitrate: str | None = None, overlay_spec: OverlaySpec | None = None) -> list[str]:
    w = width if width is not None else config.width
    h = height if height is not None else config.height
    bitrate = bitrate if bitrate is not None else config.bitrate
    if overlay_spec is None:
        overlay_spec = _primary_overlay_spec(config)

    cmd = [ffmpeg_bin, "-y"]
    if trim_start:
        cmd += ["-ss", trim_start]
    if trim_end:
        cmd += ["-to", trim_end]
    cmd += ["-i", str(input_path)]

    # Input indices are assigned dynamically: [0]=video always; the overlay
    # (if any) is [1]; the soundtrack (if any) is whatever comes next.
    next_index = 1
    overlay_index = None
    if overlay_spec.path:
        cmd += ["-loop", "1", "-i", overlay_spec.path]
        overlay_index = next_index
        next_index += 1

    filter_complex = _overlay_filter(w, h, config.rotation, config.position_x, config.position_y,
                                     overlay_spec, overlay_index)

    audio_mapped = False
    if config.soundtrack:
        if config.soundtrack_trim and config.soundtrack_trim.start:
            cmd += ["-ss", config.soundtrack_trim.start]
        if config.soundtrack_trim and config.soundtrack_trim.end:
            cmd += ["-to", config.soundtrack_trim.end]
        cmd += ["-i", config.soundtrack]
        snd_index = next_index

        if _has_audio_stream(input_path):
            filter_complex += (
                f";[0:a]volume={config.original_volume_db}dB[origa]"
                f";[{snd_index}:a]volume={config.soundtrack_volume_db}dB[snda]"
                f";[origa][snda]amix=inputs=2:duration=first:dropout_transition=0[a]"
            )
        else:
            filter_complex += f";[{snd_index}:a]volume={config.soundtrack_volume_db}dB[a]"
        audio_mapped = True

    cmd += ["-filter_complex", filter_complex, "-map", "[v]"]
    cmd += ["-map", "[a]"] if audio_mapped else ["-map", "0:a?"]
    cmd += ["-c:v", "libx264", "-b:v", bitrate, "-preset", "medium"]
    if config.fps is not None:
        cmd += ["-r", str(config.fps)]
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
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
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


def _effective_duration(source_path: Path, trim_start: str | None, trim_end: str | None) -> float | None:
    """How many seconds of output this render will produce, for progress %.
    Best-effort — returns None if it can't be determined (bar goes
    indeterminate)."""
    start = _timestamp_to_seconds(trim_start)
    end = _timestamp_to_seconds(trim_end)
    if start is not None and end is not None:
        return max(0.01, end - start)
    source_dur = _probe_duration(source_path)
    if source_dur is None:
        return None
    if end is not None:
        return max(0.01, end)
    if start is not None:
        return max(0.01, source_dur - start)
    return source_dur


def _run_ffmpeg_with_progress(cmd: list[str], total_seconds: float | None, on_progress):
    """Run an ffmpeg render via Popen, streaming its live -progress output and
    calling on_progress(percent 0-100) as it advances. Returns
    (returncode, stderr_tail). on_progress is only called when the integer
    percent changes, so it stays cheap even for long clips."""
    # -progress writes machine-readable key=value lines to stdout; -nostats
    # silences the usual human progress spam on stderr.
    full_cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None

    # ffmpeg writes stream/filter info and warnings to stderr throughout the
    # run. If we only read stdout (the -progress stream) and leave stderr
    # unread, ffmpeg blocks once stderr fills the OS pipe buffer — a deadlock
    # that "sticks" processing forever (small Windows pipe buffers hit this
    # readily). Drain stderr on a background thread so it can never fill up.
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


def _make_thumbnail(output_path: Path, thumbnail_path: Path, ffmpeg_bin: str) -> bool:
    """Grab a representative frame from the finished clip's midpoint, for the
    kiosk QR screen. Best-effort: a failure here must not fail the job."""
    duration = _probe_duration(output_path) or 2.0
    midpoint = max(0.1, duration / 2)
    cmd = [
        ffmpeg_bin, "-y", "-ss", f"{midpoint:.2f}", "-i", str(output_path),
        "-frames:v", "1", "-q:v", "3", str(thumbnail_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0 and thumbnail_path.exists()


def _archive_original(source_path: Path) -> None:
    """Move a processed original out of its import folder into an
    "Edited Footages" subfolder (excluded from watching). Best-effort — a
    failed move must never fail the job."""
    try:
        if not source_path.exists():
            return
        archive_dir = source_path.parent / EDITED_FOOTAGES_SUBDIR
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

    source_path = Path(job.source_path)
    project_dir = (config.project_dir or source_path.parent).resolve()
    output_subdir = QR_OUTPUT_SUBDIR if config.delivery_mode == "qr_only" else OUTPUT_SUBDIR
    output_dir = project_dir / output_subdir
    output_dir.mkdir(parents=True, exist_ok=True)
    # Always store an absolute path: it's persisted in the DB and later read
    # back by send_file()/shutil.move() in a process that may have a
    # different working directory than the one that created this job.
    output_path = (output_dir / (source_path.stem + ".mp4")).resolve()

    trim = config.trim_for(job.filename)
    duration = _effective_duration(source_path, trim.start, trim.end)
    # One bar spanning every render pass: pass p of n fills the bar from
    # p/n to (p+1)/n as that pass runs.
    num_passes = 2 if config.second_resolution else 1

    def _progress_cb(pass_index):
        return lambda pct: store.set_progress(
            job.id, int((pass_index + pct / 100) / num_passes * 100)
        )

    store.set_progress(job.id, 0)
    cmd = build_ffmpeg_cmd(source_path, output_path, config, trim.start, trim.end, ffmpeg_bin=ffmpeg_bin,
                           overlay_spec=_primary_overlay_spec(config))
    logger.info("Processing job %s: %s", job.id, " ".join(cmd))

    try:
        returncode, tail = _run_ffmpeg_with_progress(cmd, duration, _progress_cb(0))
    except FileNotFoundError:
        store.mark_error(job.id, _FFMPEG_MISSING_MESSAGE)
        return

    if returncode != 0:
        logger.error("ffmpeg failed for job %s: %s", job.id, tail)
        store.mark_error(job.id, f"ffmpeg failed: {tail}")
        return

    secondary_output_path: Path | None = None
    if config.second_resolution:
        secondary_output_path = (
            output_dir / f"{source_path.stem}_{config.second_resolution}.mp4"
        ).resolve()
        second_cmd = build_ffmpeg_cmd(
            source_path, secondary_output_path, config, trim.start, trim.end, ffmpeg_bin=ffmpeg_bin,
            width=config.second_width, height=config.second_height,
            bitrate=config.second_bitrate or config.bitrate,
            overlay_spec=_second_overlay_spec(config),
        )
        logger.info("Processing job %s (second resolution): %s", job.id, " ".join(second_cmd))
        try:
            second_returncode, second_tail = _run_ffmpeg_with_progress(second_cmd, duration, _progress_cb(1))
        except FileNotFoundError:
            second_returncode, second_tail = 1, "ffmpeg not found"
        if second_returncode != 0:
            logger.error("Second-resolution ffmpeg failed for job %s: %s", job.id, second_tail)
            # Non-fatal: the primary output is still good, just drop the second one.
            secondary_output_path = None

    thumbnail_path = output_path.with_suffix(THUMBNAIL_SUFFIX)
    thumb_ok = _make_thumbnail(output_path, thumbnail_path, ffmpeg_bin)
    if not thumb_ok:
        logger.warning("Thumbnail generation failed for job %s; kiosk view will show no preview image", job.id)

    store.mark_ready(
        job.id, str(output_path),
        thumbnail_path=str(thumbnail_path) if thumb_ok else None,
        secondary_output_path=str(secondary_output_path) if secondary_output_path else None,
    )
    logger.info("Job %s ready: %s", job.id, output_path)

    # Clip is processed — move the original out of the import folder so it's
    # archived and never reprocessed. The DB keeps the original source_path
    # for identity; nothing downstream needs the source file.
    _archive_original(source_path)

    job = store.get_job(job.id)

    if config.delivery_mode == "qr_only" and not config.auto_deliver:
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
        from .delivery import deliver
        from .emailer import load_default_template, resolve_placeholders

        subject, body = "", ""
        try:
            raw_subject, raw_body = load_default_template()
            subject = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
            body = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
        except Exception:
            logger.warning("Could not load default email template for auto-delivery of job %s", job.id)

        job = store.get_job(job.id)
        try:
            deliver(job, config, store, project_dir.parent, recipient=config.recipient_email,
                    subject=subject, body=body, delivery_mode=config.delivery_mode)
        except (DriveError, EmailError) as exc:
            logger.exception("Auto-delivery failed for job %s", job.id)
            store.update_job(job.id, error=f"Auto-delivery failed: {exc}")
