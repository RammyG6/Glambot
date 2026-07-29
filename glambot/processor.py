"""Turn raw footage into a trimmed, overlaid, compressed clip via ffmpeg."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import ProjectConfig
from .db import Job, JobStore
from .drive import DriveError, upload_and_share

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

OUTPUT_SUBDIR = "Output_ReadytoSend"
SENT_SUBDIR = "Email Sent File"

# QR-only ("Instant Download" / kiosk) projects use a separate on-disk layout
# from email-mode projects.
QR_OUTPUT_SUBDIR = "Output"
QR_APPROVED_SUBDIR = "Selected Output"
QR_DOWNLOAD_SUBDIR = "Instant Download"

_OVERLAY_MARGIN = 20
THUMBNAIL_SUFFIX = ".jpg"


def is_footage_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


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


def _overlay_filter(config: ProjectConfig) -> str:
    """Return the filter_complex fragment that composites the overlay onto
    the scaled/cropped background, producing output pad [v]."""
    w, h = config.width, config.height
    bg = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1[bg]"
    )

    # overlay_scale (% of frame width) overrides the per-position default
    # sizing below; without it, `full` fills the frame and corner/custom
    # positions keep the overlay at its native pixel size (legacy behavior).
    if config.overlay_scale is not None:
        target_w = max(1, round(w * config.overlay_scale / 100))
        ov = f"[1:v]scale={target_w}:-1[ov]"
    elif config.overlay_position == "full":
        ov = f"[1:v]scale={w}:{h}[ov]"
    else:
        ov = "[1:v]copy[ov]"

    if config.overlay_position == "custom":
        x = f"W*{config.overlay_x / 100:.6f}"
        y = f"H*{config.overlay_y / 100:.6f}"
        return f"{bg};{ov};[bg][ov]overlay={x}:{y}:format=auto[v]"

    if config.overlay_position == "full" and config.overlay_scale is None:
        return f"{bg};{ov};[bg][ov]overlay=0:0:format=auto[v]"

    positions = {
        "full": ("0", "0"),
        "top-left": (str(_OVERLAY_MARGIN), str(_OVERLAY_MARGIN)),
        "top-right": (f"W-w-{_OVERLAY_MARGIN}", str(_OVERLAY_MARGIN)),
        "bottom-left": (str(_OVERLAY_MARGIN), f"H-h-{_OVERLAY_MARGIN}"),
        "bottom-right": (f"W-w-{_OVERLAY_MARGIN}", f"H-h-{_OVERLAY_MARGIN}"),
    }
    x, y = positions[config.overlay_position]
    return f"{bg};{ov};[bg][ov]overlay={x}:{y}:format=auto[v]"


def build_ffmpeg_cmd(input_path: Path, output_path: Path, config: ProjectConfig,
                      trim_start: str, trim_end: str, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    filter_complex = _overlay_filter(config)
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", trim_start, "-to", trim_end, "-i", str(input_path),
        "-loop", "1", "-i", config.overlay,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-b:v", config.bitrate, "-preset", "medium",
    ]
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
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


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
    cmd = build_ffmpeg_cmd(source_path, output_path, config, trim.start, trim.end, ffmpeg_bin=ffmpeg_bin)
    logger.info("Processing job %s: %s", job.id, " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        store.mark_error(job.id, _FFMPEG_MISSING_MESSAGE)
        return

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        logger.error("ffmpeg failed for job %s: %s", job.id, tail)
        store.mark_error(job.id, f"ffmpeg failed: {tail}")
        return

    thumbnail_path = output_path.with_suffix(THUMBNAIL_SUFFIX)
    thumb_ok = _make_thumbnail(output_path, thumbnail_path, ffmpeg_bin)
    if not thumb_ok:
        logger.warning("Thumbnail generation failed for job %s; kiosk view will show no preview image", job.id)

    store.mark_ready(job.id, str(output_path), thumbnail_path=str(thumbnail_path) if thumb_ok else None)
    logger.info("Job %s ready: %s", job.id, output_path)

    if config.delivery_mode == "qr_only":
        # Upload to Drive right away so the download link is already sitting
        # there by the time an operator clicks Approve at a live event — no
        # upload wait in front of the client. A failure here is non-fatal:
        # the job still reaches `ready` and Approve will retry the upload.
        folder_id = os.environ.get("DRIVE_FOLDER_ID", "")
        try:
            link = upload_and_share(output_path, folder_id)
            store.update_job(job.id, drive_link=link)
        except DriveError as exc:
            logger.warning("Auto-upload failed for job %s: %s", job.id, exc)
            store.update_job(job.id, error=f"Drive pre-upload failed (will retry on Approve): {exc}")
