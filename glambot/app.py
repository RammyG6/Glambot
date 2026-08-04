"""Local Flask review app — the human approval gate.

Lists every processed clip that's waiting for review, lets the operator
preview it, edit the recipient email and the email subject/body, and then
either Approve (upload to Drive, email the link, archive the file) or Reject.
Nothing leaves the machine until Approve is clicked — unless a project has
`auto_deliver` enabled, in which case delivery already happened automatically
right after processing (see glambot/processor.py).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from .config import (
    AUDIO_EXTENSIONS,
    EMAIL_RE,
    ConfigError,
    extract_drive_folder_id,
    load_config,
    managed_subdir_in,
    save_config,
)
from .db import Job, JobStore
from .delivery import deliver
from .drive import DriveError
from .emailer import EmailError, load_default_template, resolve_placeholders, send_delivery_email
from .folders import all_project_dirs, group_by_folder, project_watch_dirs
from .qr import make_qr_data_uri

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
ROTATION_CHOICES = [(0, "No rotation"), (90, "+90° (clockwise)"), (-90, "-90° (counter-clockwise)"), (180, "180°")]
_VALID_ROTATIONS = {val for val, _ in ROTATION_CHOICES}

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOUNDTRACKS_DIR = _REPO_ROOT / "soundtracks"


def create_app(inbox_dir: Path, store: JobStore) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_REPO_ROOT / "templates"),
        static_folder=str(_REPO_ROOT / "static"),
    )
    app.secret_key = os.environ.get("FLASK_SECRET", "glambot-local-dev")
    app.config["INBOX_DIR"] = Path(inbox_dir)
    app.config["STORE"] = store

    @app.get("/")
    def index():
        inbox_dir = app.config["INBOX_DIR"]
        error_jobs = store.list_jobs(status="error")
        sent_jobs = store.list_jobs(status="sent")[:20]
        processing_jobs = store.list_jobs(status="processing")

        # Full-automation kiosk clips (qr_only + auto_deliver) don't get an
        # approve/reject card — they deliver themselves. The only reason one
        # would be sitting in "ready" is that its automatic delivery failed;
        # surface those in a compact read-only "needs attention" list with a
        # Retry button instead of a full review card.
        cards = []
        auto_failed = []
        for job in store.list_jobs(status="ready"):
            try:
                config = load_config(inbox_dir / job.project)
                is_auto_kiosk = config.delivery_mode == "qr_only" and config.auto_deliver
            except ConfigError:
                is_auto_kiosk = False
            if is_auto_kiosk:
                if job.error:
                    auto_failed.append(job)
                # else: momentarily ready, about to auto-deliver — skip silently
                continue
            cards.append(_build_card(job, inbox_dir))

        project_groups = _compute_project_groups(inbox_dir, store)
        output_log = store.list_jobs()[:60]
        email_log = [j for j in store.list_jobs(status="sent") if j.delivery_mode == "email"][:60]
        return render_template(
            "review.html", cards=cards, error_jobs=error_jobs, sent_jobs=sent_jobs,
            project_groups=project_groups, processing_jobs=processing_jobs,
            output_log=output_log, email_log=email_log, auto_failed=auto_failed,
        )

    @app.get("/status")
    def status():
        """Live JSON of clips currently being processed in the background —
        polled by the review page to animate progress bars."""
        jobs = store.list_jobs(status="processing")
        resp = jsonify([
            {"id": j.id, "project": j.project, "filename": j.filename,
             "progress": j.progress if j.progress is not None else 0}
            for j in jobs
        ])
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/folders/active")
    def set_active_folder_project():
        folder = request.form.get("folder", "")
        project = request.form.get("project", "")
        watch_dirs = project_watch_dirs(app.config["INBOX_DIR"])
        valid_projects = {p for p, d in watch_dirs.items() if str(d) == folder}
        if not folder or project not in valid_projects:
            flash("Invalid folder/project selection.", "error")
            return redirect(url_for("index"))
        store.set_active_project(folder, project)
        flash(f"'{project}' is now the active project for that shared folder.", "info")
        return redirect(url_for("index"))

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

    @app.get("/projects/<project>/kiosk")
    def kiosk_live(project):
        """Auto-refreshing "now showing" screen for a project — displays
        every delivered clip (thumbnail + QR) in a newest-first grid, so an
        operator can leave it open on a venue monitor as a wall of
        scan-your-clip codes that stays current on its own as new clips
        auto-deliver (see `auto_deliver` in config.json)."""
        jobs = [j for j in store.list_jobs(project=project, status="sent") if j.drive_link][:12]
        items = []
        for job in jobs:
            items.append({
                "job": job,
                "qr_data_uri": make_qr_data_uri(job.drive_link),
                "qr_data_uri2": make_qr_data_uri(job.secondary_drive_link) if job.secondary_drive_link else None,
            })
        latest_id = jobs[0].id if jobs else None
        return render_template("kiosk_live.html", project=project, items=items, latest_id=latest_id)

    @app.get("/projects/<project>/kiosk.json")
    def kiosk_live_json(project):
        """Live JSON for the monitoring page — polled so the video panel and
        grid stay current without a full page reload restarting playback."""
        jobs = [j for j in store.list_jobs(project=project, status="sent") if j.drive_link][:12]
        resp = jsonify({
            "latest_id": jobs[0].id if jobs else None,
            "clips": [
                {"id": j.id, "filename": j.filename,
                 "qr": make_qr_data_uri(j.drive_link),
                 "qr2": make_qr_data_uri(j.secondary_drive_link) if j.secondary_drive_link else None}
                for j in jobs
            ],
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/browse")
    def browse():
        """Read-only directory listing (names only, no file contents) for
        the New Project form's in-app folder browser. Consistent with this
        app's existing no-auth/local-machine-only design."""
        raw = request.args.get("path", "")
        base = Path(raw).expanduser() if raw else Path.home()
        if not base.is_dir():
            base = Path.home()
        base = base.resolve()
        try:
            folders = sorted(
                (p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=str.lower,
            )
        except PermissionError:
            folders = []
        parent = str(base.parent) if base.parent != base else None
        return jsonify({"path": str(base), "parent": parent, "folders": folders})

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

        try:
            config = load_config(app.config["INBOX_DIR"] / job.project)
        except ConfigError as exc:
            flash(f"Config problem: {exc}", "error")
            return redirect(url_for("index"))

        try:
            result = deliver(
                job, config, store, app.config["INBOX_DIR"],
                recipient=recipient, subject=subject, body=body, delivery_mode=delivery_mode,
            )
        except (DriveError, EmailError) as exc:
            logger.exception("Delivery failed for job %s", job_id)
            # Leave status as "ready" (not "error") so the clip stays in the
            # approval queue and can simply be retried once the underlying
            # problem (credentials, network, ...) is fixed.
            store.update_job(job_id, error=f"Delivery failed: {exc}")
            flash(f"Delivery failed: {exc}", "error")
            return redirect(url_for("index"))

        template = "kiosk.html" if delivery_mode == "qr_only" else "sent.html"
        return render_template(
            template, job=result.job, link=result.link, qr_data_uri=result.qr_data_uri,
            link2=result.link2, qr_data_uri2=result.qr_data_uri2,
        )

    @app.post("/jobs/<int:job_id>/reject")
    def reject(job_id):
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            flash("Job is not ready.", "error")
            return redirect(url_for("index"))
        store.mark_rejected(job_id)
        flash(f"Rejected {job.filename}.", "info")
        return redirect(url_for("index"))

    @app.post("/jobs/<int:job_id>/retry_delivery")
    def retry_delivery(job_id):
        """Re-run automatic delivery for a full-auto kiosk clip whose first
        auto-delivery failed — uses the project's default recipient/template,
        the same as the automatic path, so there's no approve/reject step."""
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            flash("Job is not ready.", "error")
            return redirect(url_for("index"))
        inbox_dir = app.config["INBOX_DIR"]
        try:
            config = load_config(inbox_dir / job.project)
        except ConfigError as exc:
            flash(f"Config problem: {exc}", "error")
            return redirect(url_for("index"))

        subject, body = "", ""
        try:
            raw_subject, raw_body = load_default_template()
            subject = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
            body = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
        except Exception:
            pass

        try:
            deliver(
                job, config, store, inbox_dir,
                recipient=config.recipient_email or None,
                subject=subject, body=body, delivery_mode=config.delivery_mode,
            )
        except (DriveError, EmailError) as exc:
            logger.exception("Retry delivery failed for job %s", job_id)
            store.update_job(job_id, error=f"Auto-delivery failed: {exc}")
            flash(f"Delivery still failing: {exc}", "error")
            return redirect(url_for("index"))
        flash(f"Delivered {job.filename}.", "info")
        return redirect(url_for("index"))

    @app.get("/clips")
    def edited_clips():
        """Browse every delivered clip (has a Drive link) and email any of
        them — useful for kiosk/auto clips that were never emailed."""
        jobs = [j for j in store.list_jobs(status="sent") if j.drive_link]
        cards = []
        for job in jobs:
            subject_default, body_default = "", ""
            try:
                raw_subject, raw_body = load_default_template()
                subject_default = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
                body_default = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
            except Exception:
                pass
            cards.append({
                "job": job,
                "recipient_default": job.recipient_email or "",
                "subject_default": subject_default,
                "body_default": body_default,
            })
        return render_template("edited_clips.html", cards=cards)

    @app.post("/jobs/<int:job_id>/email")
    def email_clip(job_id):
        """Email an already-delivered clip's existing Drive link (no
        re-upload / re-process) — the same email an email-mode Approve sends."""
        job = store.get_job(job_id)
        if job is None or job.status != "sent" or not job.drive_link:
            flash("That clip isn't available to email.", "error")
            return redirect(url_for("edited_clips"))
        recipient = request.form.get("recipient_email", "").strip()
        if not recipient or "@" not in recipient:
            flash("A valid recipient email is required.", "error")
            return redirect(url_for("edited_clips"))
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "")
        link, link2 = job.drive_link, job.secondary_drive_link
        final_subject = resolve_placeholders(subject, link=link, project=job.project, filename=job.filename, link2=link2)
        final_body = resolve_placeholders(body, link=link, project=job.project, filename=job.filename, link2=link2)
        if link2 and "{link2}" not in body:
            final_body = f"{final_body}\n\nAlternate version: {link2}"
        try:
            send_delivery_email(recipient=recipient, subject=final_subject, body=final_body, link=link, link2=link2)
        except EmailError as exc:
            logger.exception("Emailing clip %s failed", job_id)
            flash(f"Email failed: {exc}", "error")
            return redirect(url_for("edited_clips"))
        flash(f"Emailed {job.filename} to {recipient}.", "info")
        return redirect(url_for("edited_clips"))

    @app.get("/projects/new")
    def new_project_form():
        return render_template(
            "project_form.html", mode="create", error=None, values={},
            **_project_form_kwargs(),
        )

    @app.post("/projects/new")
    def create_project():
        def _form_error(message: str):
            return render_template(
                "project_form.html", mode="create", error=message, values=request.form,
                **_project_form_kwargs(),
            ), 400

        fields, error = _parse_project_form(request)
        if error:
            return _form_error(error)
        data = fields["data"]
        overlay_file = fields["overlay_file"]
        second_overlay_file = fields["second_overlay_file"]
        soundtrack_file = fields["soundtrack_file"]
        project_name = fields["name"]

        inbox_dir = app.config["INBOX_DIR"]
        project_dir = inbox_dir / project_name
        if (project_dir / "config.json").exists():
            return _form_error(f"A project named '{project_name}' already exists.")

        created_dir = not project_dir.exists()
        project_dir.mkdir(parents=True, exist_ok=True)

        overlays_dir = _REPO_ROOT / "overlays"
        overlays_dir.mkdir(parents=True, exist_ok=True)
        saved_overlays: list[Path] = []

        def _save_overlay(file_storage) -> str:
            fn = secure_filename(f"{project_name}_{file_storage.filename}")
            dest = overlays_dir / fn
            if dest.exists():
                dest = overlays_dir / f"{dest.stem}_{uuid4().hex[:8]}{dest.suffix}"
            file_storage.save(dest)
            saved_overlays.append(dest)
            return f"overlays/{dest.name}"

        # Overlay is optional now — only save/reference it when uploaded.
        if overlay_file is not None:
            data["overlay"] = _save_overlay(overlay_file)
        if second_overlay_file is not None:
            data["second_overlay"] = _save_overlay(second_overlay_file)

        soundtrack_path = None
        if soundtrack_file is not None:
            _SOUNDTRACKS_DIR.mkdir(parents=True, exist_ok=True)
            soundtrack_filename = secure_filename(soundtrack_file.filename)
            soundtrack_path = _SOUNDTRACKS_DIR / soundtrack_filename
            if soundtrack_path.exists():
                soundtrack_path = _SOUNDTRACKS_DIR / f"{soundtrack_path.stem}_{uuid4().hex[:8]}{soundtrack_path.suffix}"
            soundtrack_file.save(soundtrack_path)
            data["soundtrack"] = f"soundtracks/{soundtrack_path.name}"

        config_path = project_dir / "config.json"
        save_config(project_dir, data, merge=False)

        try:
            load_config(project_dir)
        except ConfigError as exc:
            config_path.unlink(missing_ok=True)
            for ov in saved_overlays:
                ov.unlink(missing_ok=True)
            if soundtrack_path is not None:
                soundtrack_path.unlink(missing_ok=True)
            if created_dir:
                try:
                    project_dir.rmdir()
                except OSError:
                    pass
            return _form_error(str(exc))

        flash(
            f"Project '{project_name}' created. Drop footage into "
            f"{inbox_dir.name}/{project_name}/ (or its custom source folder) to begin processing.",
            "info",
        )
        return redirect(url_for("index"))

    @app.get("/projects/<project>/edit")
    def edit_project_form(project):
        inbox_dir = app.config["INBOX_DIR"]
        project_dir = inbox_dir / project
        config_path = project_dir / "config.json"
        if not config_path.exists():
            abort(404)

        # Read the raw dict directly rather than via load_config(), so a
        # currently-broken config (invalid JSON or failed validation) can
        # still be opened here and fixed, instead of being unreachable.
        error = None
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json must contain a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            data = {}
            error = f"config.json is not valid JSON ({exc}) — fix and save to repair it."
        else:
            try:
                load_config(project_dir)
            except ConfigError as exc:
                error = str(exc)

        values = _project_values_for_edit(data, project)
        return render_template(
            "project_form.html", mode="edit", error=error, values=values,
            existing_overlay=data.get("overlay"), existing_second_overlay=data.get("second_overlay"),
            **_project_form_kwargs(),
        )

    @app.post("/projects/<project>/edit")
    def update_project(project):
        inbox_dir = app.config["INBOX_DIR"]
        project_dir = inbox_dir / project
        config_path = project_dir / "config.json"
        if not config_path.exists():
            abort(404)

        previous_text = config_path.read_text(encoding="utf-8")
        try:
            existing_data = json.loads(previous_text)
            if not isinstance(existing_data, dict):
                existing_data = {}
        except json.JSONDecodeError:
            existing_data = {}

        def _form_error(message: str):
            return render_template(
                "project_form.html", mode="edit", error=message, values=request.form,
                existing_overlay=existing_data.get("overlay"),
                existing_second_overlay=existing_data.get("second_overlay"),
                **_project_form_kwargs(),
            ), 400

        fields, error = _parse_project_form(request)
        if error:
            return _form_error(error)
        data = fields["data"]
        overlay_file = fields["overlay_file"]
        second_overlay_file = fields["second_overlay_file"]
        soundtrack_file = fields["soundtrack_file"]
        # Renaming isn't supported here — project_name is read-only on the
        # edit form; whatever the URL says for `project` always wins.

        overlays_dir = _REPO_ROOT / "overlays"
        overlays_dir.mkdir(parents=True, exist_ok=True)

        def _save_overlay(file_storage) -> str:
            fn = secure_filename(f"{project}_{file_storage.filename}")
            dest = overlays_dir / fn
            if dest.exists():
                dest = overlays_dir / f"{dest.stem}_{uuid4().hex[:8]}{dest.suffix}"
            file_storage.save(dest)
            return f"overlays/{dest.name}"

        # Only touch overlay/second_overlay/soundtrack when something new was
        # actually uploaded this submission — otherwise leave the key out of
        # `data` so save_config()'s merge preserves the existing reference.
        if overlay_file is not None:
            data["overlay"] = _save_overlay(overlay_file)
        if second_overlay_file is not None:
            data["second_overlay"] = _save_overlay(second_overlay_file)
        if soundtrack_file is not None:
            _SOUNDTRACKS_DIR.mkdir(parents=True, exist_ok=True)
            soundtrack_filename = secure_filename(soundtrack_file.filename)
            soundtrack_path = _SOUNDTRACKS_DIR / soundtrack_filename
            if soundtrack_path.exists():
                soundtrack_path = _SOUNDTRACKS_DIR / f"{soundtrack_path.stem}_{uuid4().hex[:8]}{soundtrack_path.suffix}"
            soundtrack_file.save(soundtrack_path)
            data["soundtrack"] = f"soundtracks/{soundtrack_path.name}"

        save_config(project_dir, data, merge=True)

        try:
            load_config(project_dir)
        except ConfigError as exc:
            # Unlike create, there's a known-good prior config here — restore
            # it instead of deleting anything.
            config_path.write_text(previous_text, encoding="utf-8")
            return _form_error(str(exc))

        flash(
            f"Project '{project}' updated. Already-processed clips stay in their current location.",
            "info",
        )
        return redirect(url_for("index"))

    return app


def _compute_project_groups(inbox_dir: Path, store: JobStore) -> list[dict]:
    """Every project under inbox_dir, grouped by effective footage folder —
    projects sharing a folder are grouped together (with an "active" marker
    and "Make active" control); every other project gets its own singleton
    group. Projects with a broken config.json still get a row (flagged with
    an error) so the main page's Edit link can be used to fix them, unlike
    project_watch_dirs() which silently skips them."""
    watch_dirs = project_watch_dirs(inbox_dir)  # valid configs only
    groups = group_by_folder(watch_dirs)

    errors: dict[str, str] = {}
    for project_dir in all_project_dirs(inbox_dir):
        name = project_dir.name
        if name in watch_dirs:
            continue
        try:
            load_config(project_dir)
        except ConfigError as exc:
            errors[name] = str(exc)
            groups.setdefault(project_dir, []).append(name)

    result = []
    for folder, projects in groups.items():
        shared = len(projects) > 1
        active = (store.get_active_project(str(folder)) or sorted(projects)[0]) if shared else None
        result.append({
            "folder": str(folder),
            "shared": shared,
            "projects": sorted(projects),
            "active": active,
            "errors": errors,
        })
    return sorted(result, key=lambda g: g["folder"])


def _list_soundtracks() -> list[str]:
    if not _SOUNDTRACKS_DIR.exists():
        return []
    return sorted(
        p.name for p in _SOUNDTRACKS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def _project_form_kwargs() -> dict:
    """Template kwargs shared by every render of project_form.html (create
    and edit alike), besides mode/error/values which differ per call site."""
    return {
        "resolution_presets": RESOLUTION_PRESETS,
        "fps_presets": FPS_PRESETS,
        "bitrate_presets": BITRATE_PRESETS,
        "overlay_positions": OVERLAY_POSITIONS,
        "delivery_modes": DELIVERY_MODES,
        "rotation_choices": ROTATION_CHOICES,
        "existing_soundtracks": _list_soundtracks(),
    }


def _project_values_for_edit(data: dict, project_name: str) -> dict:
    """Reverse-map a raw config.json dict into the flat form-field keys that
    project_form.html / _parse_project_form use, so an existing project's
    settings can be pre-filled onto the same form used for creation."""
    values: dict[str, str] = {"project_name": project_name}

    def _split_resolution(res, prefix: str = "") -> None:
        if not res:
            return
        preset_values = {val for val, _ in RESOLUTION_PRESETS}
        if res in preset_values:
            values[f"{prefix}resolution_preset"] = res
        else:
            values[f"{prefix}resolution_preset"] = "custom"
            if "x" in res:
                w, h = res.split("x", 1)
                values[f"{prefix}custom_width"] = w
                values[f"{prefix}custom_height"] = h

    _split_resolution(data.get("resolution"))
    second_resolution = data.get("second_resolution")
    if second_resolution:
        values["second_resolution_enabled"] = "on"
        _split_resolution(second_resolution, "second_")

    fps = data.get("fps")
    if fps is not None:
        if fps in FPS_PRESETS:
            values["fps_preset"] = str(fps)
        else:
            values["fps_preset"] = "custom"
            values["custom_fps"] = str(fps)

    def _split_bitrate(bitrate, prefix: str = "") -> None:
        if not bitrate:
            return
        if bitrate in BITRATE_PRESETS:
            values[f"{prefix}bitrate_preset"] = bitrate
        else:
            values[f"{prefix}bitrate_preset"] = "custom"
            values[f"{prefix}custom_bitrate"] = bitrate

    _split_bitrate(data.get("bitrate"))
    _split_bitrate(data.get("second_bitrate"), "second_")

    values["delivery_mode"] = data.get("delivery_mode", "email")
    values["recipient_email"] = data.get("recipient_email", "")

    trim = data.get("trim") or {}
    values["trim_start"] = trim.get("start") or ""
    values["trim_end"] = trim.get("end") or ""

    values["overlay_position"] = data.get("overlay_position", "full")
    if data.get("overlay_scale") is not None:
        values["overlay_scale"] = str(data["overlay_scale"])
    if data.get("overlay_x") is not None:
        values["overlay_x"] = str(data["overlay_x"])
    if data.get("overlay_y") is not None:
        values["overlay_y"] = str(data["overlay_y"])

    values["second_overlay_position"] = data.get("second_overlay_position", "full")
    if data.get("second_overlay_scale") is not None:
        values["second_overlay_scale"] = str(data["second_overlay_scale"])
    if data.get("second_overlay_x") is not None:
        values["second_overlay_x"] = str(data["second_overlay_x"])
    if data.get("second_overlay_y") is not None:
        values["second_overlay_y"] = str(data["second_overlay_y"])

    values["rotation"] = str(data.get("rotation", 0))
    values["position_x"] = str(data.get("position_x", 0))
    values["position_y"] = str(data.get("position_y", 0))

    if data.get("auto_deliver"):
        values["auto_deliver"] = "on"

    soundtrack = data.get("soundtrack")
    if soundtrack:
        name = Path(soundtrack).name
        values["soundtrack_choice"] = name if name in set(_list_soundtracks()) else "__none__"
    else:
        values["soundtrack_choice"] = "__none__"
    if data.get("soundtrack_volume_db") is not None:
        values["soundtrack_volume_db"] = str(data["soundtrack_volume_db"])
    if data.get("original_volume_db") is not None:
        values["original_volume_db"] = str(data["original_volume_db"])
    soundtrack_trim = data.get("soundtrack_trim") or {}
    values["soundtrack_trim_start"] = soundtrack_trim.get("start") or ""
    values["soundtrack_trim_end"] = soundtrack_trim.get("end") or ""

    values["source_dir"] = data.get("source_dir") or ""
    values["output_dir"] = data.get("output_dir") or ""
    values["drive_folder_id"] = data.get("drive_folder_id") or ""

    return values


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


def _safe_project_name(raw: str) -> str | None:
    name = raw.strip()
    if not _PROJECT_NAME_RE.match(name):
        return None
    return name


def _aspect_ratio_label(width: int, height: int) -> str:
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _parse_resolution(form, prefix: str = "") -> tuple[int, int] | None:
    preset = form.get(f"{prefix}resolution_preset", "")
    if preset == "custom":
        w = form.get(f"{prefix}custom_width", "").strip()
        h = form.get(f"{prefix}custom_height", "").strip()
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


def _parse_overlay_group_form(form, files, prefix: str):
    """Parse one overlay group (`<prefix>overlay_file` + position/scale/x/y) from
    the New Project form. The overlay itself is optional. Returns
    (file_or_None, config_updates_dict, error_or_None) — config_updates has the
    position/scale/x/y keys to merge into the config when a file is present."""
    file = files.get(f"{prefix}overlay_file")
    if file is not None and not file.filename:
        file = None
    if file is not None and Path(file.filename).suffix.lower() not in OVERLAY_EXTENSIONS:
        return None, None, "Overlay must be an image file (.png, .jpg, .jpeg, .webp, .gif)."

    updates: dict = {}
    position = form.get(f"{prefix}overlay_position", "full")
    if position not in OVERLAY_POSITIONS:
        return None, None, "Invalid overlay position."
    updates[f"{prefix}overlay_position"] = position

    scale_raw = form.get(f"{prefix}overlay_scale", "").strip()
    if scale_raw:
        try:
            scale = int(float(scale_raw))
        except ValueError:
            return None, None, "Overlay size must be a number."
        if not (1 <= scale <= 100):
            return None, None, "Overlay size must be between 1 and 100 percent."
        updates[f"{prefix}overlay_scale"] = scale

    if position == "custom":
        try:
            ox = float(form.get(f"{prefix}overlay_x", "").strip())
            oy = float(form.get(f"{prefix}overlay_y", "").strip())
        except ValueError:
            return None, None, "Custom overlay position needs numeric X/Y percentages."
        if not (0 <= ox <= 100 and 0 <= oy <= 100):
            return None, None, "Custom overlay X/Y must be between 0 and 100."
        updates[f"{prefix}overlay_x"] = ox
        updates[f"{prefix}overlay_y"] = oy

    return file, updates, None


def _parse_optional_db(raw: str, name: str) -> tuple[float | None, str | None]:
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        val = float(raw)
    except ValueError:
        return None, f"{name} must be a number."
    if not (-60 <= val <= 12):
        return None, f"{name} must be between -60 and 12 dB."
    return val, None


def _parse_project_form(req):
    """Validate the New Project form. Returns (fields, error): `fields` is a
    dict {data, overlay_file, second_overlay_file, soundtrack_file, name} on
    success, or None with an error message on failure."""
    form = req.form

    name = _safe_project_name(form.get("project_name", ""))
    if not name:
        return None, "Project name is required (letters, numbers, spaces, - or _ only)."

    resolution = _parse_resolution(form)
    if resolution is None:
        return None, "Choose a valid resolution (or fill in a valid custom width/height)."
    width, height = resolution

    fps, fps_error = _parse_fps(form)
    if fps_error:
        return None, fps_error

    bitrate, bitrate_error = _parse_bitrate(form)
    if bitrate_error:
        return None, bitrate_error

    delivery_mode = form.get("delivery_mode", "email")
    if delivery_mode not in _VALID_DELIVERY_MODES:
        return None, "Invalid delivery mode."

    recipient_email = form.get("recipient_email", "").strip()
    if delivery_mode == "email":
        if not EMAIL_RE.match(recipient_email):
            return None, "A valid client email address is required."
    elif recipient_email and not EMAIL_RE.match(recipient_email):
        return None, "Client email, if provided, must be a valid address."

    # Trim is fully optional now — leaving either side blank means "don't
    # trim that side."
    trim_start = form.get("trim_start", "").strip()
    trim_end = form.get("trim_end", "").strip()

    # Overlay is optional — no file uploaded means no overlay.
    overlay_file, overlay_updates, err = _parse_overlay_group_form(form, req.files, "")
    if err:
        return None, err

    # --- Rotation / repositioning ---------------------------------------
    try:
        rotation = int(form.get("rotation", "0") or "0")
    except ValueError:
        return None, "Invalid rotation value."
    if rotation not in _VALID_ROTATIONS:
        return None, "Invalid rotation value."

    position_x_raw = form.get("position_x", "").strip()
    position_y_raw = form.get("position_y", "").strip()
    position_x = position_y = None
    if position_x_raw:
        try:
            position_x = int(position_x_raw)
        except ValueError:
            return None, "Position X must be a whole number of pixels."
    if position_y_raw:
        try:
            position_y = int(position_y_raw)
        except ValueError:
            return None, "Position Y must be a whole number of pixels."

    # --- Full automation --------------------------------------------------
    auto_deliver = form.get("auto_deliver") == "on"

    # --- Soundtrack ---------------------------------------------------
    soundtrack_choice = form.get("soundtrack_choice", "")
    soundtrack_file = None
    soundtrack_rel_path = None
    if soundtrack_choice == "__upload__":
        soundtrack_file = req.files.get("soundtrack_file")
        if not soundtrack_file or not soundtrack_file.filename:
            return None, "Choose a soundtrack file to upload, or set soundtrack to 'None'."
        if Path(soundtrack_file.filename).suffix.lower() not in AUDIO_EXTENSIONS:
            return None, "Soundtrack must be an audio file (.mp3, .wav, .m4a, .aac, .flac, .ogg)."
    elif soundtrack_choice and soundtrack_choice != "__none__":
        if soundtrack_choice not in set(_list_soundtracks()):
            return None, "Invalid soundtrack selection."
        soundtrack_rel_path = f"soundtracks/{soundtrack_choice}"

    soundtrack_volume_db, err = _parse_optional_db(form.get("soundtrack_volume_db", ""), "Soundtrack volume")
    if err:
        return None, err
    original_volume_db, err = _parse_optional_db(form.get("original_volume_db", ""), "Original volume")
    if err:
        return None, err

    soundtrack_trim_start = form.get("soundtrack_trim_start", "").strip()
    soundtrack_trim_end = form.get("soundtrack_trim_end", "").strip()

    # --- Dual-resolution export (+ its own optional overlay) --------------
    second_resolution_enabled = form.get("second_resolution_enabled") == "on"
    second_resolution = None
    second_bitrate = None
    second_overlay_file = None
    second_overlay_updates: dict = {}
    if second_resolution_enabled:
        second_res = _parse_resolution(form, prefix="second_")
        if second_res is None:
            return None, "Choose a valid second resolution (or fill in a valid custom width/height)."
        second_resolution = f"{second_res[0]}x{second_res[1]}"

        sb_preset = form.get("second_bitrate_preset", "")
        if sb_preset == "custom":
            sb_raw = form.get("second_custom_bitrate", "").strip()
            if not sb_raw:
                return None, "Custom second bitrate is required (e.g. 10M)."
            second_bitrate = sb_raw
        elif sb_preset in BITRATE_PRESETS:
            second_bitrate = sb_preset
        elif sb_preset != "":
            return None, "Invalid second bitrate selection."

        second_overlay_file, second_overlay_updates, err = _parse_overlay_group_form(form, req.files, "second_")
        if err:
            return None, err

    # --- Custom footage source folder ------------------------------------
    source_dir_raw = form.get("source_dir", "").strip()
    if source_dir_raw:
        source_path = Path(source_dir_raw).expanduser()
        if not source_path.is_dir():
            return None, "Footage source folder does not exist or is not a directory."
        managed = managed_subdir_in(source_path.resolve())
        if managed:
            return None, (
                f"Footage source folder cannot be inside Glambot's own '{managed}' folder. "
                f"That folder holds clips Glambot has already processed, so watching it "
                f"would re-process every clip. Pick the import folder instead."
            )

    # --- Custom output location (parent dir only; "<project>_Output" is
    # appended automatically, never user-typed) ----------------------------
    output_dir_raw = form.get("output_dir", "").strip()
    if output_dir_raw:
        output_path = Path(output_dir_raw).expanduser()
        if output_path.exists() and not output_path.is_dir():
            return None, "Output location must be a folder, not a file."

    # --- Per-project Google Drive destination folder ----------------------
    drive_folder_raw = form.get("drive_folder_id", "").strip()
    drive_folder_id = extract_drive_folder_id(drive_folder_raw) if drive_folder_raw else None

    # Every field below is written explicitly (even at its "unset" default)
    # rather than only when truthy. This matters for editing an existing
    # project: save_config() merges these keys over the existing config.json,
    # so a field the operator resets to its default (e.g. unchecking "auto
    # deliver", clearing a custom position) must still overwrite the old
    # value instead of silently leaving it in place because the key was
    # omitted. Harmless for a brand-new project either way.
    data: dict = {
        "recipient_email": recipient_email,
        "delivery_mode": delivery_mode,
        "bitrate": bitrate,
        "resolution": f"{width}x{height}",
        "aspect_ratio": _aspect_ratio_label(width, height),
        "trim": {k: v for k, v in (("start", trim_start), ("end", trim_end)) if v},
        "fps": fps,
        "rotation": rotation,
        "position_x": position_x if position_x is not None else 0,
        "position_y": position_y if position_y is not None else 0,
        "auto_deliver": auto_deliver,
        "soundtrack_volume_db": soundtrack_volume_db if soundtrack_volume_db is not None else 0.0,
        "original_volume_db": original_volume_db if original_volume_db is not None else 0.0,
        "soundtrack_trim": {
            k: v for k, v in (("start", soundtrack_trim_start), ("end", soundtrack_trim_end)) if v
        },
        "second_resolution": second_resolution,
        "second_bitrate": second_bitrate,
        "source_dir": str(Path(source_dir_raw).expanduser().resolve()) if source_dir_raw else None,
        "output_dir": str(Path(output_dir_raw).expanduser().resolve()) if output_dir_raw else None,
        "drive_folder_id": drive_folder_id,
    }
    # Overlay position/scale/x/y are saved even without a new upload, so an
    # existing overlay's placement can be nudged/resized on its own (edit
    # mode). Harmless on create with no overlay — the keys just go unused.
    data.update(overlay_updates)
    data.update(second_overlay_updates)
    # soundtrack: an explicit "None" selection clears it; an existing-file
    # choice sets it; "__upload__" is left for the caller to fill in once the
    # uploaded file is actually saved to disk.
    if soundtrack_choice == "__none__":
        data["soundtrack"] = None
    elif soundtrack_rel_path is not None:
        data["soundtrack"] = soundtrack_rel_path

    return {
        "data": data,
        "overlay_file": overlay_file,
        "second_overlay_file": second_overlay_file,
        "soundtrack_file": soundtrack_file,
        "name": name,
    }, None
