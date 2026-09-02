"""Guest-facing download pages, served over the LAN when there's no internet.

Two entry points, both gated by the project's own `download_pin` (4-8 digits,
printed on the QR card) - never the operator PIN:

  /d/<token>     one clip, reached by scanning that clip's QR code
  /g/<project>   a gallery of every delivered clip for the project

The operator UI's `before_request` hook (glambot/auth.py) sees
`request.blueprint == "guest"` and steps aside, leaving enforcement to this
module.
"""
from __future__ import annotations

import re
import time

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .config import ConfigError, load_config

guest_bp = Blueprint("guest", __name__)

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")

# Crude in-memory throttle on PIN guesses: {ip: [timestamps within window]}.
_ATTEMPTS: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 300


def _store():
    return current_app.config["STORE"]


def _inbox_dir():
    return current_app.config["INBOX_DIR"]


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _ATTEMPTS.get(ip, []) if now - t < _WINDOW_SECONDS]
    _ATTEMPTS[ip] = hits
    return len(hits) >= _MAX_ATTEMPTS


def _record_attempt(ip: str) -> None:
    _ATTEMPTS.setdefault(ip, []).append(time.time())


def _project_config(project: str):
    if not _PROJECT_NAME_RE.match(project or ""):
        abort(404)
    project_dir = _inbox_dir() / project
    if not (project_dir / "config.json").exists():
        abort(404)
    try:
        return load_config(project_dir)
    except ConfigError:
        abort(404)


def _unlocked(project: str) -> bool:
    return session.get(f"guest:{project}") is True


def _job_for_token(token: str):
    job = _store().get_job_by_token(token)
    if job is None or job.status == "rejected":
        abort(404)
    return job


@guest_bp.get("/d/<token>")
def download_page(token):
    job = _job_for_token(token)
    config = _project_config(job.project)
    if not config.download_pin:
        abort(404)
    if not _unlocked(job.project):
        return render_template("guest_download.html", token=token, job=job, locked=True, error=None)
    return render_template("guest_download.html", token=token, job=job, locked=False, error=None)


@guest_bp.post("/d/<token>/unlock")
def unlock(token):
    job = _job_for_token(token)
    config = _project_config(job.project)
    ip = request.remote_addr or "?"
    if _rate_limited(ip):
        return render_template("guest_download.html", token=token, job=job, locked=True,
                               error="Too many attempts. Wait a few minutes and try again."), 429
    _record_attempt(ip)
    if config.download_pin and request.form.get("pin", "").strip() == config.download_pin:
        session[f"guest:{job.project}"] = True
        session.permanent = True
        return redirect(url_for("guest.download_page", token=token))
    return render_template("guest_download.html", token=token, job=job, locked=True,
                           error="Wrong PIN."), 401


@guest_bp.get("/d/<token>/file")
def download_file(token):
    job = _job_for_token(token)
    _project_config(job.project)
    if not _unlocked(job.project):
        abort(403)
    if not job.output_path:
        abort(404)
    _store().bump_lan_download(job.id)
    name = f"{job.project}_{job.filename}".rsplit(".", 1)[0] + ".mp4"
    return send_file(job.output_path, as_attachment=True, download_name=name, conditional=True)


@guest_bp.get("/d/<token>/stream")
def download_stream(token):
    job = _job_for_token(token)
    _project_config(job.project)
    if not _unlocked(job.project):
        abort(403)
    if not job.output_path:
        abort(404)
    return send_file(job.output_path, conditional=True)


@guest_bp.route("/g/<project>", methods=["GET", "POST"])
def gallery(project):
    config = _project_config(project)
    if not config.download_pin:
        abort(404)
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _rate_limited(ip):
            error = "Too many attempts. Wait a few minutes and try again."
        else:
            _record_attempt(ip)
            if request.form.get("pin", "").strip() == config.download_pin:
                session[f"guest:{project}"] = True
                session.permanent = True
                return redirect(url_for("guest.gallery", project=project))
            error = "Wrong PIN."
    if not _unlocked(project):
        return render_template("guest_gallery.html", project=project, locked=True,
                               error=error, clips=[]), (401 if error else 200)
    clips = [
        j for j in _store().list_jobs(project=project, status="sent")
        if j.download_token and not j.hidden_from_kiosk
    ]
    return render_template("guest_gallery.html", project=project, locked=False, error=None, clips=clips)


@guest_bp.get("/g/<project>/thumb/<int:job_id>")
def gallery_thumb(project, job_id):
    _project_config(project)
    if not _unlocked(project):
        abort(403)
    job = _store().get_job(job_id)
    if job is None or job.project != project or not job.thumbnail_path:
        abort(404)
    return send_file(job.thumbnail_path, conditional=True)
