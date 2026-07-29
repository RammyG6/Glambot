"""Local Flask review app — the human approval gate.

Lists every processed clip that's waiting for review, lets the operator
preview it, edit the recipient email and the email subject/body, and then
either Approve (upload to Drive, email the link, archive the file) or Reject.
Nothing leaves the machine until Approve is clicked.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from .config import EMAIL_RE, ConfigError, load_config
from .db import Job, JobStore
from .drive import DriveError, upload_and_share
from .emailer import EmailError, load_default_template, resolve_placeholders, send_delivery_email
from .processor import QR_APPROVED_SUBDIR, QR_DOWNLOAD_SUBDIR, SENT_SUBDIR
from .qr import make_delivery_photo, make_qr_data_uri

logger = logging.getLogger(__name__)

RESOLUTION_PRESETS = [
    ("1280x720", "720p (1280x720)"),
    ("1920x1080", "1080p (1920x1080)"),
    ("1080x1920", "1080x1920 (vertical)"),
    ("3840x2160", "4K (3840x2160)"),
    ("2160x3840", "4K vertical (2160x3840)"),
]
FPS_PRESETS = [24, 25, 30, 60]
BITRATE_PRESETS = ["2M", "5M", "8M", "15M", "40M"]
OVERLAY_POSITIONS = ["full", "top-left", "top-right", "bottom-left", "bottom-right", "custom"]
OVERLAY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DELIVERY_MODES = [("email", "Email to client"), ("qr_only", "Instant QR download (kiosk)")]
_VALID_DELIVERY_MODES = {mode for mode, _ in DELIVERY_MODES}

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


_REPO_ROOT = Path(__file__).resolve().parent.parent


def create_app(inbox_dir: Path, store: JobStore) -> Flask:
    app = Flask(__name__, template_folder=str(_REPO_ROOT / "templates"))
    app.secret_key = os.environ.get("FLASK_SECRET", "glambot-local-dev")
    app.config["INBOX_DIR"] = Path(inbox_dir)
    app.config["STORE"] = store

    @app.get("/")
    def index():
        ready_jobs = store.list_jobs(status="ready")
        error_jobs = store.list_jobs(status="error")
        sent_jobs = store.list_jobs(status="sent")[:20]
        cards = [_build_card(job, app.config["INBOX_DIR"]) for job in ready_jobs]
        return render_template("review.html", cards=cards, error_jobs=error_jobs, sent_jobs=sent_jobs)

    @app.get("/video/<int:job_id>")
    def video(job_id):
        job = store.get_job(job_id)
        if job is None or not job.output_path or not Path(job.output_path).exists():
            abort(404)
        return send_file(job.output_path)

    @app.get("/thumbnail/<int:job_id>")
    def thumbnail(job_id):
        job = store.get_job(job_id)
        if job is None or not job.thumbnail_path or not Path(job.thumbnail_path).exists():
            abort(404)
        return send_file(job.thumbnail_path)

    @app.post("/jobs/<int:job_id>/approve")
    def approve(job_id):
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            flash("Job is not ready for approval.", "error")
            return redirect(url_for("index"))

        delivery_mode = request.form.get("delivery_mode", job.delivery_mode or "email")
        if delivery_mode not in _VALID_DELIVERY_MODES:
            delivery_mode = "email"

        recipient = request.form.get("recipient_email", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "")

        if delivery_mode == "email" and (not recipient or "@" not in recipient):
            flash("A valid recipient email is required.", "error")
            return redirect(url_for("index"))

        folder_id = os.environ.get("DRIVE_FOLDER_ID", "")
        try:
            if delivery_mode == "qr_only" and job.drive_link:
                # Already uploaded automatically right after processing — no
                # need to re-upload, so Approve is instant at the booth.
                link = job.drive_link
            else:
                link = upload_and_share(Path(job.output_path), folder_id)
            if delivery_mode == "email":
                final_subject = resolve_placeholders(subject, link=link, project=job.project, filename=job.filename)
                final_body = resolve_placeholders(body, link=link, project=job.project, filename=job.filename)
                send_delivery_email(recipient=recipient, subject=final_subject, body=final_body, link=link)
        except (DriveError, EmailError) as exc:
            logger.exception("Delivery failed for job %s", job_id)
            # Leave status as "ready" (not "error") so the clip stays in the
            # approval queue and can simply be retried once the underlying
            # problem (credentials, network, ...) is fixed.
            store.update_job(job_id, error=f"Delivery failed: {exc}")
            flash(f"Delivery failed: {exc}", "error")
            return redirect(url_for("index"))

        inbox_dir = app.config["INBOX_DIR"]
        archive_subdir = QR_APPROVED_SUBDIR if delivery_mode == "qr_only" else SENT_SUBDIR
        archived_path, archived_thumb = _archive(job, inbox_dir, archive_subdir)

        if delivery_mode == "qr_only" and archived_thumb:
            try:
                photo_bytes = make_delivery_photo(archived_thumb, link)
                download_dir = inbox_dir / job.project / QR_DOWNLOAD_SUBDIR
                download_dir.mkdir(parents=True, exist_ok=True)
                photo_path = download_dir / f"{Path(job.output_path).stem}_download.jpg"
                photo_path.write_bytes(photo_bytes)
            except Exception:
                logger.exception("Failed to generate delivery photo for job %s", job_id)

        job = store.mark_sent(
            job_id, drive_link=link, output_path=str(archived_path),
            recipient_email=recipient or None,
            delivery_mode=delivery_mode,
            thumbnail_path=str(archived_thumb) if archived_thumb else job.thumbnail_path,
        )

        qr_data_uri = make_qr_data_uri(link)
        if delivery_mode == "qr_only":
            return render_template("kiosk.html", job=job, link=link, qr_data_uri=qr_data_uri)
        return render_template("sent.html", job=job, link=link, qr_data_uri=qr_data_uri)

    @app.post("/jobs/<int:job_id>/reject")
    def reject(job_id):
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            flash("Job is not ready.", "error")
            return redirect(url_for("index"))
        store.mark_rejected(job_id)
        flash(f"Rejected {job.filename}.", "info")
        return redirect(url_for("index"))

    @app.get("/projects/new")
    def new_project_form():
        return render_template(
            "project_form.html",
            resolution_presets=RESOLUTION_PRESETS,
            fps_presets=FPS_PRESETS,
            bitrate_presets=BITRATE_PRESETS,
            overlay_positions=OVERLAY_POSITIONS,
            delivery_modes=DELIVERY_MODES,
            error=None,
            values={},
        )

    @app.post("/projects/new")
    def create_project():
        def _form_error(message: str):
            return render_template(
                "project_form.html",
                resolution_presets=RESOLUTION_PRESETS,
                fps_presets=FPS_PRESETS,
                bitrate_presets=BITRATE_PRESETS,
                overlay_positions=OVERLAY_POSITIONS,
                delivery_modes=DELIVERY_MODES,
                error=message,
                values=request.form,
            ), 400

        data, overlay_file, project_name, error = _parse_project_form(request)
        if error:
            return _form_error(error)

        inbox_dir = app.config["INBOX_DIR"]
        project_dir = inbox_dir / project_name
        if (project_dir / "config.json").exists():
            return _form_error(f"A project named '{project_name}' already exists.")

        created_dir = not project_dir.exists()
        project_dir.mkdir(parents=True, exist_ok=True)

        overlays_dir = _REPO_ROOT / "overlays"
        overlays_dir.mkdir(parents=True, exist_ok=True)
        overlay_filename = secure_filename(f"{project_name}_{overlay_file.filename}")
        overlay_path = overlays_dir / overlay_filename
        overlay_file.save(overlay_path)
        data["overlay"] = f"overlays/{overlay_filename}"

        config_path = project_dir / "config.json"
        config_path.write_text(json.dumps(data, indent=2))

        try:
            load_config(project_dir)
        except ConfigError as exc:
            config_path.unlink(missing_ok=True)
            overlay_path.unlink(missing_ok=True)
            if created_dir:
                try:
                    project_dir.rmdir()
                except OSError:
                    pass
            return _form_error(str(exc))

        flash(
            f"Project '{project_name}' created. Drop footage into inbox/{project_name}/ to begin processing.",
            "info",
        )
        return redirect(url_for("index"))

    return app


def _build_card(job: Job, inbox_dir: Path) -> dict:
    """Assemble everything the template needs to render one review card,
    including the editable recipient/subject/body prefill values."""
    project_dir = inbox_dir / job.project
    recipient_default = job.recipient_email or ""
    delivery_mode_default = job.delivery_mode or "email"
    config_error = None
    try:
        config = load_config(project_dir)
        recipient_default = job.recipient_email or config.recipient_email
        delivery_mode_default = job.delivery_mode or config.delivery_mode
    except ConfigError as exc:
        config_error = str(exc)

    subject_default, body_default = "", ""
    try:
        raw_subject, raw_body = load_default_template()
        # {link} is intentionally left unresolved here — the real Drive link
        # doesn't exist until Approve triggers the upload. project/filename
        # are already known, so those get filled in now.
        subject_default = resolve_placeholders(
            raw_subject, link="{link}", project=job.project, filename=job.filename
        )
        body_default = resolve_placeholders(
            raw_body, link="{link}", project=job.project, filename=job.filename
        )
    except Exception as exc:  # a missing/malformed template shouldn't break the review page
        logger.warning("Could not load default email template: %s", exc)

    return {
        "job": job,
        "recipient_default": recipient_default,
        "subject_default": subject_default,
        "body_default": body_default,
        "delivery_mode_default": delivery_mode_default,
        "config_error": config_error,
    }


def _archive(job: Job, inbox_dir: Path, subdir: str) -> tuple[Path, Path | None]:
    project_dir = inbox_dir / job.project
    archive_dir = project_dir / subdir
    archive_dir.mkdir(parents=True, exist_ok=True)
    src = Path(job.output_path)
    dest = archive_dir / src.name
    shutil.move(str(src), str(dest))

    archived_thumb: Path | None = None
    if job.thumbnail_path:
        thumb_src = Path(job.thumbnail_path)
        if thumb_src.exists():
            thumb_dest = archive_dir / thumb_src.name
            shutil.move(str(thumb_src), str(thumb_dest))
            archived_thumb = thumb_dest
    return dest, archived_thumb


def _safe_project_name(raw: str) -> str | None:
    name = raw.strip()
    if not _PROJECT_NAME_RE.match(name):
        return None
    return name


def _aspect_ratio_label(width: int, height: int) -> str:
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _parse_resolution(form) -> tuple[int, int] | None:
    preset = form.get("resolution_preset", "")
    if preset == "custom":
        w, h = form.get("custom_width", "").strip(), form.get("custom_height", "").strip()
        if not (w.isdigit() and h.isdigit()) or int(w) <= 0 or int(h) <= 0:
            return None
        return int(w), int(h)
    if re.match(r"^\d+x\d+$", preset):
        w, h = preset.split("x")
        return int(w), int(h)
    return None


def _parse_fps(form) -> tuple[int | None, str | None]:
    """Returns (fps, error). fps of None means "leave unset" (no error)."""
    preset = form.get("fps_preset", "")
    if preset == "":
        return None, None
    if preset == "custom":
        raw = form.get("custom_fps", "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            return None, "Custom FPS must be a positive whole number."
        return int(raw), None
    if preset.isdigit():
        return int(preset), None
    return None, "Invalid fps selection."


def _parse_bitrate(form) -> tuple[str | None, str | None]:
    preset = form.get("bitrate_preset", "")
    if preset == "custom":
        raw = form.get("custom_bitrate", "").strip()
        if not raw:
            return None, "Custom bitrate is required (e.g. 10M)."
        return raw, None
    if preset in BITRATE_PRESETS:
        return preset, None
    return None, "Invalid bitrate selection."


def _parse_project_form(req):
    """Validate the New Project form. Returns (config_dict_without_overlay,
    overlay_file_storage, project_name, error_message_or_None)."""
    form = req.form

    name = _safe_project_name(form.get("project_name", ""))
    if not name:
        return None, None, None, "Project name is required (letters, numbers, spaces, - or _ only)."

    resolution = _parse_resolution(form)
    if resolution is None:
        return None, None, None, "Choose a valid resolution (or fill in a valid custom width/height)."
    width, height = resolution

    fps, fps_error = _parse_fps(form)
    if fps_error:
        return None, None, None, fps_error

    bitrate, bitrate_error = _parse_bitrate(form)
    if bitrate_error:
        return None, None, None, bitrate_error

    delivery_mode = form.get("delivery_mode", "email")
    if delivery_mode not in _VALID_DELIVERY_MODES:
        return None, None, None, "Invalid delivery mode."

    recipient_email = form.get("recipient_email", "").strip()
    if delivery_mode == "email":
        if not EMAIL_RE.match(recipient_email):
            return None, None, None, "A valid client email address is required."
    elif recipient_email and not EMAIL_RE.match(recipient_email):
        return None, None, None, "Client email, if provided, must be a valid address."

    trim_start = form.get("trim_start", "").strip()
    trim_end = form.get("trim_end", "").strip()
    if not trim_start or not trim_end:
        return None, None, None, "Trim start and end are required (e.g. 00:00:02)."

    overlay_file = req.files.get("overlay_file")
    if not overlay_file or not overlay_file.filename:
        return None, None, None, "An overlay image is required."
    if Path(overlay_file.filename).suffix.lower() not in OVERLAY_EXTENSIONS:
        return None, None, None, "Overlay must be an image file (.png, .jpg, .jpeg, .webp, .gif)."

    overlay_position = form.get("overlay_position", "full")
    if overlay_position not in OVERLAY_POSITIONS:
        return None, None, None, "Invalid overlay position."

    overlay_scale_raw = form.get("overlay_scale", "").strip()
    overlay_scale = None
    if overlay_scale_raw:
        try:
            overlay_scale = int(float(overlay_scale_raw))
        except ValueError:
            return None, None, None, "Overlay size must be a number."
        if not (1 <= overlay_scale <= 100):
            return None, None, None, "Overlay size must be between 1 and 100 percent."

    data: dict = {
        "recipient_email": recipient_email,
        "delivery_mode": delivery_mode,
        "bitrate": bitrate,
        "resolution": f"{width}x{height}",
        "aspect_ratio": _aspect_ratio_label(width, height),
        "overlay_position": overlay_position,
        "trim": {"start": trim_start, "end": trim_end},
    }
    if fps is not None:
        data["fps"] = fps
    if overlay_scale is not None:
        data["overlay_scale"] = overlay_scale

    if overlay_position == "custom":
        try:
            ox = float(form.get("overlay_x", "").strip())
            oy = float(form.get("overlay_y", "").strip())
        except ValueError:
            return None, None, None, "Custom overlay position needs numeric X/Y percentages."
        if not (0 <= ox <= 100 and 0 <= oy <= 100):
            return None, None, None, "Custom overlay X/Y must be between 0 and 100."
        data["overlay_x"] = ox
        data["overlay_y"] = oy

    return data, overlay_file, name, None
