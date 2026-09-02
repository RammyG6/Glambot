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
import secrets
import shutil
import sys
import threading
from datetime import datetime
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
from .delivery import DeliveryError, deliver
from .drive import DriveError
from .emailer import EmailError, load_default_template, resolve_placeholders, send_delivery_email
from .folders import all_project_dirs, group_by_folder, project_watch_dirs
from .ftp_import import load_ftp_settings, parse_passive_ports, save_ftp_settings
from . import lan, nativeui
from .processor import cancel_job, content_hash, is_footage_file
from .qr import make_qr_data_uri, make_wifi_qr_data_uri
from .watcher import InboxWatcher

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


# Code assets (templates/static/logo) live wherever the app was installed -
# _REPO_ROOT tracks that. A PyInstaller-frozen build extracts to a temp/
# install dir rather than preserving glambot/app.py's normal package layout,
# so __file__ isn't reliable there; use the executable's own folder instead.
_REPO_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent

# User-editable data (uploaded via the New Project form) - lives next to the
# inbox/project data, not the code, so it survives an app upgrade/reinstall.
# Resolved against the CWD rather than a parameter here because CWD is set to
# the data folder once at process startup (see glambot/pipeline.py) - every
# other data-relative path in this codebase (`.env`, credentials.json,
# config.json's relative overlay/soundtrack paths) already relies on that
# same convention.
_SOUNDTRACKS_DIR = Path.cwd() / "soundtracks"
_BACKGROUNDS_DIR = Path.cwd() / "backgrounds"

# size -> PNG bytes, rendered once from logo/glambotlogo.png for the PWA icon.
_ICON_CACHE: dict[int, bytes] = {}

# Short, disposable low-res clips from the Advanced-editing "Render preview".
_PREVIEW_DIR = Path.cwd() / ".glambot" / "preview"


def _cleanup_previews() -> None:
    """Keep the preview folder small: drop files older than an hour, then keep
    only the newest few."""
    try:
        files = sorted(_PREVIEW_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    import time as _time
    cutoff = _time.time() - 3600
    for i, p in enumerate(files):
        try:
            if i >= 5 or p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _resolve_output_footage(project_dir: Path) -> Path | None:
    try:
        from .processor import resolve_output_base
        return resolve_output_base(project_dir, load_config(project_dir)) / "Footage"
    except ConfigError:
        return None


def create_app(inbox_dir: Path, store: JobStore, watcher: InboxWatcher, ftp_server=None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_REPO_ROOT / "templates"),
        static_folder=str(_REPO_ROOT / "static"),
    )
    app.secret_key = _resolve_secret(Path(inbox_dir))
    app.config["INBOX_DIR"] = Path(inbox_dir)
    app.config["STORE"] = store
    app.config["FTP_SERVER"] = ftp_server

    from .auth import init_auth
    init_auth(app)

    from .guest import guest_bp
    app.register_blueprint(guest_bp)

    @app.template_filter("mmss")
    def _mmss(seconds):
        """Format a seconds value (float/int, possibly None) as M:SS for
        the review page's render-time/clip-length display."""
        if seconds is None:
            return "—"
        total = int(round(seconds))
        return f"{total // 60}:{total % 60:02d}"

    @app.get("/")
    def index():
        inbox_dir = app.config["INBOX_DIR"]
        # Re-render is offerable whenever the source can still be found - either
        # in its import folder (a failed render never archives it) or in the
        # Footage/ archive next to a successful one. Mirror what process_job's
        # _resolve_source() can actually do so the button and the route agree.
        from .processor import source_available
        _rr_cfg: dict[str, object] = {}

        def _can_rerender(j):
            try:
                cfg = _rr_cfg.get(j.project) or _rr_cfg.setdefault(
                    j.project, load_config(inbox_dir / j.project))
                return source_available(j, cfg)
            except ConfigError:
                return bool(j.source_path) and Path(j.source_path).exists()

        error_jobs = [
            {"id": j.id, "project": j.project, "filename": j.filename, "error": j.error,
             "can_rerender": _can_rerender(j)}
            for j in store.list_jobs(status="error")
        ]
        sent_jobs = store.list_jobs(status="sent")[:20]
        _sent_cfg_cache: dict[str, object] = {}

        def _sent_cfg(project: str):
            if project not in _sent_cfg_cache:
                try:
                    _sent_cfg_cache[project] = load_config(inbox_dir / project)
                except ConfigError:
                    _sent_cfg_cache[project] = None
            return _sent_cfg_cache[project]

        sent_cards = []
        for j in sent_jobs:
            cfg = _sent_cfg(j.project)
            sent_cards.append({
                "job": j,
                "render_seconds": _render_seconds(j),
                "adv_values": _adv_values(cfg, j.filename),
                **_delivered_clip_prefill(j, cfg),
            })
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
            sent_cards=sent_cards,
            project_groups=project_groups, processing_jobs=processing_jobs,
            output_log=output_log, email_log=email_log, auto_failed=auto_failed,
            project_orientations=_project_quick_toggles(inbox_dir),
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

    @app.post("/jobs/<int:job_id>/stop")
    def stop_job(job_id):
        """Kill a render that's running too long (e.g. an accidental
        over-length recording). The source file is untouched - process_job
        only archives the original after a successful render - so the
        existing Re-render button (and bulk retry) can reprocess it."""
        job = store.get_job(job_id)
        if job is None or job.status != "processing":
            flash("That clip isn't currently processing.", "error")
            return redirect(url_for("index"))
        if cancel_job(job_id):
            flash(f"Stopping {job.filename}...", "info")
        else:
            flash("Nothing is actively rendering for that clip yet - try again in a moment.", "error")
        return redirect(url_for("index"))

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
        delivered = [j for j in store.list_jobs(project=project, status="sent")
                     if (j.drive_link or j.download_token) and not j.hidden_from_kiosk]
        count = _kiosk_count(request.args.get("count"))
        jobs = delivered[:count]
        has_more = len(delivered) > count
        items = []
        for job in jobs:
            primary = _guest_or_drive_link(job)
            items.append({
                "job": job,
                "qr_data_uri": make_qr_data_uri(primary) if primary else None,
                "qr_data_uri2": make_qr_data_uri(job.secondary_drive_link) if job.secondary_drive_link else None,
            })
        latest_id = jobs[0].id if jobs else None
        return render_template("kiosk_live.html", project=project, items=items, latest_id=latest_id,
                               wifi_qr=_wifi_qr_data_uri(), has_more=has_more, count=count)

    @app.get("/projects/<project>/kiosk.json")
    def kiosk_live_json(project):
        """Live JSON for the monitoring page — polled so the video panel and
        grid stay current without a full page reload restarting playback.
        `?count=` grows the grid via the page's Load more button."""
        delivered = [j for j in store.list_jobs(project=project, status="sent")
                     if (j.drive_link or j.download_token) and not j.hidden_from_kiosk]
        count = _kiosk_count(request.args.get("count"))
        jobs = delivered[:count]
        state = store.get_kiosk_state(project)
        visible_ids = {j.id for j in jobs}
        resp = jsonify({
            "latest_id": jobs[0].id if jobs else None,
            "live_id": state["live_job_id"] if state["live_job_id"] in visible_ids else None,
            "paused": state["paused"],
            "has_more": len(delivered) > count,
            "clips": [
                {"id": j.id, "filename": j.filename,
                 "qr": make_qr_data_uri(_guest_or_drive_link(j)),
                 "qr2": make_qr_data_uri(j.secondary_drive_link) if j.secondary_drive_link else None}
                for j in jobs
            ],
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/projects/<project>/kiosk/hidden")
    def kiosk_hidden(project):
        """Clips an operator hid from the kiosk screen - viewable and
        reversible here, never deleted."""
        jobs = [j for j in store.list_jobs(project=project, status="sent")
                if (j.drive_link or j.download_token) and j.hidden_from_kiosk]
        items = [{"job": j, "qr_data_uri": make_qr_data_uri(_guest_or_drive_link(j))} for j in jobs]
        return render_template("kiosk_hidden.html", project=project, items=items)

    @app.get("/projects/<project>/playback-background")
    def playback_background(project):
        """Serves the effective background image for the reel - the
        project's uploaded one, or the default Glambot logo if none is set."""
        project_dir = _resolve_project(project)
        path = None
        try:
            config = load_config(project_dir)
            if config.playback_background:
                path = Path(config.playback_background)
        except ConfigError:
            path = None
        if path is None:
            path = _REPO_ROOT / "logo" / "glambotlogo.png"
        if not path.exists():
            abort(404)
        return send_file(path)

    @app.get("/projects/<project>/playback")
    def playback_reel(project):
        """Dedicated full-screen page that just plays every delivered clip
        for a project on a loop, newest first - a second venue display
        distinct from the QR-code Kiosk monitor."""
        project_dir = _resolve_project(project)
        try:
            opacity = load_config(project_dir).playback_background_opacity
        except ConfigError:
            opacity = 50
        return render_template("playback.html", project=project, background_opacity=opacity)

    @app.get("/projects/<project>/playback.json")
    def playback_reel_json(project):
        """Every delivered, non-hidden clip for the project, newest-first,
        with no cap (unlike kiosk.json's [:8]) - the reel loops through the
        full history, not just the newest handful."""
        jobs = [j for j in store.list_jobs(project=project, status="sent")
                if (j.drive_link or j.download_token) and not j.hidden_from_kiosk]
        state = store.get_kiosk_state(project)
        resp = jsonify({
            "paused": state["paused"],
            "clips": [{"id": j.id, "filename": j.filename} for j in jobs],
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/jobs/<int:job_id>/hide")
    def hide_job(job_id):
        job = store.get_job(job_id)
        if job is None:
            abort(404)
        store.set_hidden(job_id, True)
        return jsonify({"ok": True})

    @app.post("/jobs/<int:job_id>/unhide")
    def unhide_job(job_id):
        job = store.get_job(job_id)
        if job is None:
            abort(404)
        store.set_hidden(job_id, False)
        return jsonify({"ok": True})

    @app.get("/browse")
    def browse():
        """Read-only directory listing for the New Project form's in-app
        folder browser (subfolders) and the Import footage page (subfolders
        + matching footage files, via ?files=1). Consistent with this app's
        existing no-auth/local-machine-only design."""
        # Strip surrounding quotes/whitespace: Windows Explorer's "Copy as
        # path" wraps the result in double quotes, which would otherwise
        # silently fail to resolve and fall back to the home folder below -
        # with no error shown, so a pasted path just quietly shows the wrong
        # folder's contents.
        raw = request.args.get("path", "").strip().strip('"').strip("'")
        base = Path(raw).expanduser() if raw else Path.home()
        # Surfaced in the response so the browser can warn the user instead
        # of silently showing an unrelated folder's contents - a typo'd or
        # since-deleted path would otherwise look like "this folder is
        # empty" with no indication it wasn't even the folder asked for.
        not_found = bool(raw) and not base.is_dir()
        if not base.is_dir():
            base = Path.home()
        base = base.resolve()
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            entries = []
        folders = [p.name for p in entries if p.is_dir() and not p.name.startswith(".")]
        parent = str(base.parent) if base.parent != base else None
        resp = {"path": str(base), "parent": parent, "folders": folders, "not_found": not_found}
        if request.args.get("files"):
            resp["files"] = [
                {"name": p.name, "size": p.stat().st_size}
                for p in entries if p.is_file() and is_footage_file(p)
            ]
        return jsonify(resp)

    @app.post("/rescan")
    def rescan():
        """Force an immediate re-scan of every project's watch folder,
        instead of waiting for the watcher's periodic ~10s cycle - e.g. right
        after starting the camera before the Glambot PC was ready."""
        watcher.rescan_now()
        flash("Rescanned every project's watch folder.", "info")
        return redirect(url_for("index"))

    # ---- FTP import: built-in FTP server for camera auto-import -------

    def _ftp():
        return app.config.get("FTP_SERVER")

    @app.get("/ftp-import")
    def ftp_import_page():
        settings = load_ftp_settings(app.config["INBOX_DIR"])
        server = _ftp()
        if server is not None:
            status = server.status()
        else:
            from .ftp_import import DEFAULT_USERNAME, firewall_command, firewall_rule_state
            ports = parse_passive_ports(settings["passive_ports"]) or (50000, 50050)
            connect_host = str(settings["passive_host"]).strip() or lan.lan_ip()
            status = {
                "running": False, "port": settings["port"], "root_dir": settings["root_dir"],
                "lan_ip": lan.lan_ip(), "sessions": 0, "uploads_total": 0,
                "anonymous": bool(settings["anonymous"]),
                "username": str(settings["username"]).strip() or DEFAULT_USERNAME,
                "passive_host": settings["passive_host"], "passive_ports": settings["passive_ports"],
                "connect_host": connect_host,
                "firewall_state": firewall_rule_state(int(settings["port"]), ports),
                "firewall_command": firewall_command(int(settings["port"]), ports),
                "clients": [], "uploads": [],
            }
        root_exists = bool(str(settings["root_dir"]).strip()) and Path(settings["root_dir"]).is_dir()
        return render_template(
            "ftp_import.html", settings=settings, status=status, root_exists=root_exists,
            available=server is not None,
        )

    @app.get("/ftp-import/status.json")
    def ftp_import_status():
        server = _ftp()
        if server is None:
            resp = jsonify({"running": False, "available": False})
        else:
            resp = jsonify({"available": True, **server.status()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/ftp-import/settings")
    def ftp_import_save():
        inbox_dir = app.config["INBOX_DIR"]
        form = request.form
        root_dir = form.get("root_dir", "").strip().strip('"').strip("'")
        if not root_dir:
            flash("An import folder is required.", "error")
            return redirect(url_for("ftp_import_page"))
        try:
            port = int(form.get("port", "2121"))
        except ValueError:
            flash("Port must be a number.", "error")
            return redirect(url_for("ftp_import_page"))
        if not (1 <= port <= 65535):
            flash("Port must be between 1 and 65535.", "error")
            return redirect(url_for("ftp_import_page"))
        if parse_passive_ports(form.get("passive_ports", "")) is None:
            flash("Passive port range must look like '50000-50050' (1024-65535, low < high).", "error")
            return redirect(url_for("ftp_import_page"))
        anonymous = form.get("anonymous") == "on"
        # Blank username always resolves back to the default ("glambot"); the
        # named account is always registered alongside anonymous access.
        username = form.get("username", "").strip() or "glambot"
        password = form.get("password", "")

        settings = save_ftp_settings(inbox_dir, {
            "port": port, "root_dir": root_dir, "anonymous": anonymous,
            "username": username, "password": password,
            "passive_host": form.get("passive_host", "").strip(),
            "passive_ports": form.get("passive_ports", "").strip(),
        })
        server = _ftp()
        if server is not None and server.running:
            err = server.restart(settings)
            flash(f"Saved, but restart failed: {err}" if err else "Saved and restarted the FTP server.",
                  "error" if err else "info")
        else:
            flash("FTP import settings saved.", "info")
        return redirect(url_for("ftp_import_page"))

    @app.post("/ftp-import/root/create")
    def ftp_import_create_root():
        settings = load_ftp_settings(app.config["INBOX_DIR"])
        try:
            Path(settings["root_dir"]).mkdir(parents=True, exist_ok=True)
            flash(f"Created {settings['root_dir']}.", "info")
        except OSError as exc:
            flash(f"Could not create that folder: {exc}", "error")
        return redirect(url_for("ftp_import_page"))

    @app.post("/ftp-import/start")
    def ftp_import_start():
        inbox_dir = app.config["INBOX_DIR"]
        server = _ftp()
        if server is None:
            flash("The FTP server component isn't available in this build.", "error")
            return redirect(url_for("ftp_import_page"))
        settings = save_ftp_settings(inbox_dir, {"enabled": True})
        err = server.start(settings)
        if err:
            save_ftp_settings(inbox_dir, {"enabled": False})
            flash(f"Could not start: {err}", "error")
        else:
            flash("FTP import server started.", "info")
        return redirect(url_for("ftp_import_page"))

    @app.post("/ftp-import/stop")
    def ftp_import_stop():
        save_ftp_settings(app.config["INBOX_DIR"], {"enabled": False})
        server = _ftp()
        if server is not None:
            server.stop()
        flash("FTP import server stopped.", "info")
        return redirect(url_for("ftp_import_page"))

    @app.post("/ftp-import/firewall/apply")
    def ftp_import_firewall_apply():
        """Add/refresh the Windows Firewall rule for the FTP + passive ports.
        Loopback-only: it raises a UAC prompt on the Glambot PC."""
        if (request.remote_addr or "") not in {"127.0.0.1", "::1", "localhost"}:
            flash("Firewall changes can only be made from the Glambot PC.", "error")
            return redirect(url_for("ftp_import_page"))
        server = _ftp()
        if server is None:
            flash("The FTP server component isn't available in this build.", "error")
            return redirect(url_for("ftp_import_page"))
        err = server.apply_firewall()
        if err:
            flash(err, "error")
        elif server.refresh_firewall_state() == "ok":
            flash("Windows Firewall rule applied.", "info")
        else:
            flash("Firewall rule not confirmed yet - it may take a moment, or was declined.", "error")
        return redirect(url_for("ftp_import_page"))

    @app.post("/ftp-import/open-folder")
    def ftp_import_open_folder():
        """Open the import folder in the OS file manager. Loopback-only, like /pick."""
        if (request.remote_addr or "") not in {"127.0.0.1", "::1", "localhost"}:
            flash("The folder can only be opened on the Glambot PC.", "error")
            return redirect(url_for("ftp_import_page"))
        root = Path(load_ftp_settings(app.config["INBOX_DIR"])["root_dir"])
        if not root.is_dir():
            flash(f"That folder doesn't exist: {root}", "error")
            return redirect(url_for("ftp_import_page"))
        try:
            if sys.platform == "win32":
                os.startfile(str(root))  # noqa: S606 - local path from local settings
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", str(root)], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", str(root)], check=False)
        except OSError as exc:
            flash(f"Could not open the folder: {exc}", "error")
        return redirect(url_for("ftp_import_page"))

    @app.post("/pick")
    def pick():
        """Pop a native OS file/folder dialog on the machine hosting Glambot and
        return what the operator picked. Loopback-only: from a LAN iPad this
        would pop a dialog on the Glambot PC, which is never what the tapper
        wants - the page falls back to a typed path there."""
        if (request.remote_addr or "") not in {"127.0.0.1", "::1", "localhost"}:
            return jsonify({"ok": False, "reason": "unsupported"})
        kind = request.form.get("kind", "folder")
        if kind not in {"folder", "footage"}:
            return jsonify({"ok": False, "reason": "error", "error": "bad kind"}), 400
        initial = request.form.get("initial", "").strip().strip('"').strip("'")

        selection = nativeui.pick(kind, initial)
        if selection is None:
            return jsonify({"ok": False, "reason": "unsupported"})
        if not selection:
            return jsonify({"ok": False, "reason": "cancelled"})

        if kind == "folder":
            folder = Path(selection[0])
            if not folder.is_dir():
                return jsonify({"ok": False, "reason": "error",
                                "error": "That isn't a folder."}), 400
            return jsonify({"ok": True, "path": str(folder)})

        files = []
        parent = None
        for raw in selection:
            p = Path(raw)
            if not (p.is_file() and is_footage_file(p)):
                continue
            if parent is None:
                parent = p.parent
            if p.parent != parent:
                return jsonify({"ok": False, "reason": "multi_folder"})
            files.append({"name": p.name, "size": p.stat().st_size})
        if not files:
            return jsonify({"ok": False, "reason": "error",
                            "error": "None of those were footage files."}), 400
        return jsonify({"ok": True, "folder": str(parent), "files": files})

    @app.get("/projects/<project>/import")
    def import_footage_form(project):
        _resolve_project(project)
        return render_template("import_footage.html", project=project)

    def _resolve_import_target(project):
        """Shared by /import and /import/check: returns (dest_dir, folder,
        chosen_names, error). `error` is a user-facing string when the import
        can't proceed; the caller decides how to surface it."""
        project_dir = _resolve_project(project)
        try:
            config = load_config(project_dir)
        except ConfigError as exc:
            return None, None, [], f"Config problem: {exc}"

        # Shared-folder safety: refuse if this project isn't the active
        # owner of its watch folder, so imported footage is never silently
        # attributed to a different project.
        inbox_dir = app.config["INBOX_DIR"]
        watch_dirs = project_watch_dirs(inbox_dir)
        dest_dir = watch_dirs.get(project, config.source_dir or project_dir)
        groups = group_by_folder(watch_dirs)
        sharing = [p for p in groups.get(dest_dir, []) if p != project]
        if sharing:
            active = store.get_active_project(str(dest_dir))
            if active != project:
                return None, None, [], (
                    f"This project's footage folder is shared with {', '.join(sharing)} "
                    f"and '{active}' is currently active for it - imported files would be "
                    f"attributed there instead. Switch the active project first."
                )

        folder_path = request.form.get("folder", "").strip()
        folder = Path(folder_path) if folder_path else None
        if not folder or not folder.is_dir():
            return None, None, [], "That folder no longer exists."

        # Never trust submitted filenames directly - recompute the real
        # footage listing for this exact folder fresh, same anti-tampering
        # pattern as delete_all_projects.
        available = {p.name for p in folder.iterdir() if p.is_file() and is_footage_file(p)}
        chosen = [name for name in request.form.getlist("files") if name in available]
        return dest_dir, folder, chosen, None

    @app.post("/projects/<project>/import/check")
    def import_footage_check(project):
        """Report, per selected file, whether importing it would collide with a
        file already in the destination folder or with footage already rendered
        for this project - so the page can ask the operator how to resolve it."""
        dest_dir, folder, chosen, error = _resolve_import_target(project)
        if error:
            return jsonify({"ok": False, "error": error}), 400
        conflicts = []
        clean = []
        for name in chosen:
            dupe = None
            digest = content_hash(folder / name)
            if digest is not None:
                job = store.find_by_hash(digest, project)
                if job is not None:
                    dupe = {"id": job.id, "filename": job.filename, "status": job.status}
            dest = dest_dir / name
            # A file that IS the one already in the watch folder isn't a
            # collision - importing it just re-queues it in place.
            name_collision = dest.exists() and dest.resolve() != (folder / name).resolve()
            if dupe or name_collision:
                conflicts.append({
                    "name": name, "name_collision": name_collision, "duplicate_job": dupe,
                })
            else:
                clean.append(name)
        return jsonify({"ok": True, "conflicts": conflicts, "clean": clean})

    @app.post("/projects/<project>/import")
    def import_footage(project):
        dest_dir, folder, chosen, error = _resolve_import_target(project)
        if error:
            flash(error, "error")
            return redirect(url_for("import_footage_form", project=project))
        if not chosen:
            flash("Nothing was selected - nothing was imported.", "info")
            return redirect(url_for("import_footage_form", project=project))

        # decision_<name> = rename | replace | skip  (absent => plain import).
        decisions = {
            name: request.form.get(f"decision_{name}", "").strip().lower()
            for name in chosen
        }
        decisions = {k: v for k, v in decisions.items() if v in {"rename", "replace", "skip"}}
        to_import = [name for name in chosen if decisions.get(name) != "skip"]
        if not to_import:
            flash("Every selected file was skipped - nothing was imported.", "info")
            return redirect(url_for("import_footage_form", project=project))

        dest_dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(
            target=_import_worker,
            args=(folder, to_import, dest_dir, watcher, decisions, store, project),
            daemon=True,
        ).start()
        flash(f"Importing {len(to_import)} file(s) into {project}...", "info")
        return redirect(url_for("index"))

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
            deliver(
                job, config, store, app.config["INBOX_DIR"],
                recipient=recipient, subject=subject, body=body, delivery_mode=delivery_mode,
            )
        except (DriveError, EmailError, DeliveryError) as exc:
            logger.exception("Delivery failed for job %s", job_id)
            # Leave status as "ready" (not "error") so the clip stays in the
            # approval queue and can simply be retried once the underlying
            # problem (credentials, network, ...) is fixed.
            store.update_job(job_id, error=f"Delivery failed: {exc}")
            flash(f"Delivery failed: {exc}", "error")
            return redirect(url_for("index"))

        if delivery_mode == "email":
            flash(f"Sent {job.filename} to {recipient}.", "info")
        else:
            flash(f"Approved {job.filename} — now on the kiosk.", "info")
        return redirect(url_for("index") + "#clips")

    @app.post("/jobs/<int:job_id>/reject")
    def reject(job_id):
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            flash("Job is not ready.", "error")
            return redirect(url_for("index"))
        store.mark_rejected(job_id)
        flash(f"Rejected {job.filename}.", "info")
        return redirect(url_for("index"))

    @app.post("/jobs/<int:job_id>/rerender")
    def rerender(job_id):
        """Re-run the render for a job that failed, or one already rendered that
        needs a fresh pass after a config change. process_job()/_resolve_source
        find the source either in its import folder or in the Footage/ archive."""
        job = store.get_job(job_id)
        if job is None:
            flash("Job not found.", "error")
            return redirect(url_for("index"))
        try:
            config = load_config(app.config["INBOX_DIR"] / job.project)
        except ConfigError as exc:
            flash(f"Config problem: {exc}", "error")
            return redirect(url_for("index"))
        from .processor import source_available
        if not source_available(job, config):
            flash(
                f"Cannot re-render {job.filename}: its source footage could not be "
                f"found (looked at {job.source_path} and the Footage archive).", "error",
            )
            return redirect(url_for("index"))

        store.update_job(job_id, status="processing", error=None, progress=0)
        # Runs on a worker thread so the browser gets its redirect straight
        # away instead of holding the request open for the whole render.
        threading.Thread(
            target=_rerender_worker, args=(store.get_job(job_id), config, store),
            daemon=True,
        ).start()
        flash(f"Re-rendering {job.filename}...", "info")
        return redirect(url_for("index"))

    @app.post("/retry_all_failed_renders")
    def retry_all_failed_renders():
        """Bulk version of /rerender - retries every job across every
        project that's in 'error' status with its source file still on disk
        (the same eligibility the single-clip Re-render button already uses,
        app.py's index() error_jobs/can_rerender), so this never does
        anything the operator couldn't already trigger one-by-one."""
        inbox_dir = app.config["INBOX_DIR"]
        from .processor import source_available
        targets = []
        for job in store.list_jobs(status="error"):
            try:
                config = load_config(inbox_dir / job.project)
            except ConfigError as exc:
                store.update_job(job.id, error=f"Config problem: {exc}")
                continue
            if not source_available(job, config):
                continue
            targets.append((job, config))
        if not targets:
            flash("No failed renders to retry (or their source files are missing).", "info")
            return redirect(url_for("index"))
        for job, _ in targets:
            store.update_job(job.id, status="processing", error=None, progress=0)
        threading.Thread(target=_rerender_all_worker, args=(targets, store), daemon=True).start()
        flash(f"Re-rendering {len(targets)} clip(s)...", "info")
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

        try:
            _retry_one_delivery(job, config, store, inbox_dir)
        except (DriveError, EmailError, DeliveryError) as exc:
            logger.exception("Retry delivery failed for job %s", job_id)
            store.update_job(job_id, error=f"Auto-delivery failed: {exc}")
            flash(f"Delivery still failing: {exc}", "error")
            return redirect(url_for("index"))
        flash(f"Delivered {job.filename}.", "info")
        return redirect(url_for("index"))

    @app.post("/retry_all_failed_deliveries")
    def retry_all_failed_deliveries():
        """Bulk version of /jobs/<id>/retry_delivery - retries every job
        across every project that's a full-auto kiosk clip stuck in 'ready'
        with a recorded delivery error (the same eligibility index()'s
        auto_failed list already uses), so a stretch of stuck clips from an
        internet outage can be cleared in one click instead of one at a
        time."""
        inbox_dir = app.config["INBOX_DIR"]
        targets = []
        for job in store.list_jobs(status="ready"):
            if not job.error:
                continue
            try:
                config = load_config(inbox_dir / job.project)
            except ConfigError:
                continue
            if config.delivery_mode == "qr_only" and config.auto_deliver:
                targets.append((job, config))
        if not targets:
            flash("No failed deliveries to retry.", "info")
            return redirect(url_for("index"))
        threading.Thread(
            target=_retry_all_deliveries_worker, args=(targets, store, inbox_dir), daemon=True,
        ).start()
        flash(f"Retrying delivery for {len(targets)} clip(s)...", "info")
        return redirect(url_for("index"))

    @app.get("/clips")
    def edited_clips():
        """Browse every delivered clip (has a Drive link) and email any of
        them — useful for kiosk/auto clips that were never emailed."""
        jobs = [j for j in store.list_jobs(status="sent") if j.drive_link]
        cards = []
        config_cache: dict[str, object] = {}
        for job in jobs:
            if job.project not in config_cache:
                try:
                    config_cache[job.project] = load_config(app.config["INBOX_DIR"] / job.project)
                except ConfigError:
                    config_cache[job.project] = None
            cfg = config_cache[job.project]
            cards.append({"job": job, **_delivered_clip_prefill(job, cfg)})

        template_projects = [
            {"name": name,
             "email_subject": cfg.email_subject or "" if cfg else "",
             "email_body": cfg.email_body or "" if cfg else ""}
            for name, cfg in sorted(config_cache.items()) if cfg is not None
        ]
        return render_template(
            "edited_clips.html", cards=cards, template_projects=template_projects,
            default_email_subject=_safe_default_template()[0],
            default_email_body=_safe_default_template()[1],
        )

    @app.post("/projects/<project>/email-template")
    def save_email_template(project):
        """Set a project's email_subject / email_body directly (used by the
        'Email a delivered clip' page's template panel). Raw read-modify-write,
        same pattern as override_job."""
        project_dir = _resolve_project(project)
        config_path = project_dir / "config.json"
        back = request.referrer or url_for("edited_clips")
        prev = config_path.read_text(encoding="utf-8")
        try:
            data = json.loads(prev)
            assert isinstance(data, dict)
        except (json.JSONDecodeError, AssertionError):
            flash(f"Could not read {project}'s config.", "error")
            return redirect(back)
        data["email_subject"] = request.form.get("email_subject", "").strip() or None
        data["email_body"] = request.form.get("email_body", "") or None
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            load_config(project_dir)
        except ConfigError as exc:
            config_path.write_text(prev, encoding="utf-8")
            flash(f"Could not save template: {exc}", "error")
            return redirect(back)
        flash(f"Updated {project}'s email template.", "info")
        return redirect(back)

    @app.post("/jobs/<int:job_id>/email")
    def email_clip(job_id):
        """Email an already-delivered clip's existing Drive link (no
        re-upload / re-process) — the same email an email-mode Approve sends."""
        back = request.referrer or url_for("edited_clips")
        job = store.get_job(job_id)
        if job is None or job.status != "sent" or not job.drive_link:
            flash("That clip isn't available to email.", "error")
            return redirect(back)
        recipient = request.form.get("recipient_email", "").strip()
        if not recipient or "@" not in recipient:
            flash("A valid recipient email is required.", "error")
            return redirect(back)
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
            return redirect(back)
        flash(f"Emailed {job.filename} to {recipient}.", "info")
        return redirect(back)

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
        vertical_overlay_file = fields["vertical_overlay_file"]
        horizontal_overlay_file = fields["horizontal_overlay_file"]
        soundtrack_file = fields["soundtrack_file"]
        background_file = fields["background_file"]
        project_name = fields["name"]

        inbox_dir = app.config["INBOX_DIR"]
        project_dir = inbox_dir / project_name
        if (project_dir / "config.json").exists():
            return _form_error(f"A project named '{project_name}' already exists.")

        created_dir = not project_dir.exists()
        project_dir.mkdir(parents=True, exist_ok=True)

        overlays_dir = Path.cwd() / "overlays"
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

        # Overlays are optional — only save/reference one when uploaded; a
        # "remove" tick clears it (upload wins over remove if both arrive).
        if vertical_overlay_file is not None:
            data["vertical_overlay"] = _save_overlay(vertical_overlay_file)
        elif fields["vertical_overlay_remove"]:
            data["vertical_overlay"] = None
        if horizontal_overlay_file is not None:
            data["horizontal_overlay"] = _save_overlay(horizontal_overlay_file)
        elif fields["horizontal_overlay_remove"]:
            data["horizontal_overlay"] = None

        soundtrack_path = None
        if soundtrack_file is not None:
            _SOUNDTRACKS_DIR.mkdir(parents=True, exist_ok=True)
            soundtrack_filename = secure_filename(soundtrack_file.filename)
            soundtrack_path = _SOUNDTRACKS_DIR / soundtrack_filename
            if soundtrack_path.exists():
                soundtrack_path = _SOUNDTRACKS_DIR / f"{soundtrack_path.stem}_{uuid4().hex[:8]}{soundtrack_path.suffix}"
            soundtrack_file.save(soundtrack_path)
            data["soundtrack"] = f"soundtracks/{soundtrack_path.name}"

        background_path = None
        if background_file is not None:
            _BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
            fn = secure_filename(f"{project_name}_{background_file.filename}")
            background_path = _BACKGROUNDS_DIR / fn
            if background_path.exists():
                background_path = _BACKGROUNDS_DIR / f"{background_path.stem}_{uuid4().hex[:8]}{background_path.suffix}"
            background_file.save(background_path)
            data["playback_background"] = f"backgrounds/{background_path.name}"

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
            if background_path is not None:
                background_path.unlink(missing_ok=True)
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

    def _resolve_project(project: str) -> Path:
        """Project dir for a name from the URL, 404ing on anything that
        isn't a real project. _safe_project_name also blocks path traversal
        (".."/separators) before the name is ever joined to a path."""
        safe = _safe_project_name(project)
        if not safe or safe != project:
            abort(404)
        project_dir = app.config["INBOX_DIR"] / safe
        if not (project_dir / "config.json").exists():
            abort(404)
        return project_dir

    @app.post("/projects/<project>/toggle-orientation")
    def toggle_orientation(project):
        """Swap a project's resolution width/height (and recompute the
        aspect_ratio label from that) so whatever footage lands next renders
        in the other orientation. Nothing else in config.json changes, and
        nothing already processed is touched - aspect_ratio is purely a
        display label (see _aspect_ratio_label); the renderer only ever
        reads width/height (see processor.py's build_ffmpeg_cmd)."""
        project_dir = _resolve_project(project)
        try:
            config = load_config(project_dir)
        except ConfigError as exc:
            flash(f"Config problem: {exc}", "error")
            return redirect(url_for("index") + "#clips")
        new_width, new_height = config.height, config.width
        save_config(project_dir, {
            "resolution": f"{new_width}x{new_height}",
            "aspect_ratio": _aspect_ratio_label(new_width, new_height),
        }, merge=True)
        orientation = "horizontal" if new_width > new_height else "vertical"
        flash(
            f"“{project}” now renders {orientation} ({new_width}x{new_height}) "
            "for new footage — clips already processed are unaffected.",
            "info",
        )
        return redirect(url_for("index") + "#clips")

    @app.post("/projects/<project>/toggle-delivery-mode")
    def toggle_delivery_mode(project):
        """Flip a project between manual approval and full-auto delivery -
        the quick review-page equivalent of the Mode radio's
        standard<->auto choice. Only valid for projects not in LAN / Offline
        mode (auto_deliver is one of four exclusive modes; see the Mode
        fieldset in project_form.html). Newly-processed clips follow the new
        setting; anything already rendered / delivered is untouched."""
        project_dir = _resolve_project(project)
        try:
            config = load_config(project_dir)
        except ConfigError as exc:
            flash(f"Config problem: {exc}", "error")
            return redirect(url_for("index") + "#clips")
        if config.lan_delivery or config.offline_mode:
            flash(
                f"“{project}” uses a Wi-Fi / offline delivery mode — change it "
                "from the project's settings instead.",
                "error",
            )
            return redirect(url_for("index") + "#clips")
        new_value = not config.auto_deliver
        save_config(project_dir, {"auto_deliver": new_value}, merge=True)
        flash(
            f"“{project}” now delivers automatically as soon as clips are "
            "processed — no Approve click." if new_value else
            f"“{project}” now waits for manual approval before delivering.",
            "info",
        )
        return redirect(url_for("index") + "#clips")

    @app.route("/projects/<project>/clear-history", methods=["GET", "POST"])
    def clear_history(project):
        """Wipe a project's job rows. Files on disk are untouched, so this is
        recoverable in the sense that nothing rendered is lost — but it does
        make already-processed footage eligible for processing again."""
        _resolve_project(project)
        jobs = store.list_jobs(project=project)
        if request.method == "GET":
            return render_template("clear_history.html", project=project, jobs=jobs)
        if request.form.get("confirm") != "yes":
            flash("Clear history cancelled.", "info")
            return redirect(url_for("index"))
        removed = store.delete_jobs_for_project(project)
        flash(f"Cleared {removed} job record(s) for '{project}'. No files were deleted.", "info")
        return redirect(url_for("index"))

    @app.route("/projects/delete-all", methods=["GET", "POST"])
    def delete_all_projects():
        """Delete every project, with a checkbox per path so nothing goes by
        assumption. Footage folders start unticked."""
        inbox_dir = app.config["INBOX_DIR"]
        groups = _all_deletion_targets(inbox_dir, store)

        if request.method == "GET":
            return render_template("delete_all.html", groups=groups)

        if request.form.get("password", "") != _delete_password():
            flash("Wrong password - nothing was deleted.", "error")
            return redirect(url_for("delete_all_projects"))

        # The ticked paths are matched against the set just recomputed above,
        # never used directly. Deleting whatever path strings arrive in the
        # form would make this route an arbitrary-delete endpoint; a stale or
        # tampered-with form can only ever select a subset of real targets.
        allowed = {str(t["path"]): t for g in groups for t in g["targets"]}
        chosen = [allowed[p] for p in request.form.getlist("paths") if p in allowed]
        if not chosen:
            flash("Nothing was selected - nothing was deleted.", "info")
            return redirect(url_for("delete_all_projects"))

        removed, failed = _delete_paths(t["path"] for t in chosen)

        # Only forget a project whose own folder was actually removed -- if
        # the user unticked it, the project still exists and must keep its
        # job history.
        chosen_paths = {t["path"] for t in chosen}
        forgotten = []
        for group in groups:
            project_target = next((t for t in group["targets"] if t["kind"] == "project"), None)
            if project_target is not None and project_target["path"] in chosen_paths:
                store.forget_project(group["project"])
                forgotten.append(group["project"])

        summary = f"Deleted {removed} path(s) across {len(forgotten)} project(s)."
        if failed:
            flash(summary + f" {len(failed)} could not be removed: " + "; ".join(failed), "error")
        else:
            flash(summary, "info")
        return redirect(url_for("index"))

    @app.route("/projects/<project>/delete", methods=["GET", "POST"])
    def delete_project(project):
        """Delete a project and its files. Irreversible, so the confirmation
        page lists every path that will go before anything is touched, and
        the POST needs the password."""
        project_dir = _resolve_project(project)
        targets = _deletion_targets(app.config["INBOX_DIR"], project, project_dir, store)

        if request.method == "GET":
            return render_template("delete_project.html", project=project, targets=targets)

        if request.form.get("password", "") != _delete_password():
            flash("Wrong password - nothing was deleted.", "error")
            return redirect(url_for("delete_project", project=project))

        removed, failed = _delete_paths(t["path"] for t in targets if t["will_delete"])
        store.forget_project(project)
        if failed:
            flash(
                f"Deleted '{project}', but {len(failed)} path(s) could not be removed: "
                + "; ".join(failed), "error",
            )
        else:
            flash(f"Deleted project '{project}' and {removed} path(s).", "info")
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
        asset_warnings: list[str] = []
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json must contain a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            data = {}
            error = f"config.json is not valid JSON ({exc}) — fix and save to repair it."
        else:
            try:
                asset_warnings = list(load_config(project_dir).missing_assets)
            except ConfigError as exc:
                error = str(exc)

        values = _project_values_for_edit(data, project)
        return render_template(
            "project_form.html", mode="edit", error=error, values=values,
            asset_warnings=asset_warnings,
            existing_vertical_overlay=data.get("vertical_overlay") or data.get("overlay"),
            existing_horizontal_overlay=data.get("horizontal_overlay") or data.get("second_overlay"),
            existing_background=data.get("playback_background"),
            **_project_form_kwargs(_project_footage_files(inbox_dir, project)),
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
                existing_vertical_overlay=existing_data.get("vertical_overlay") or existing_data.get("overlay"),
                existing_horizontal_overlay=existing_data.get("horizontal_overlay") or existing_data.get("second_overlay"),
                existing_background=existing_data.get("playback_background"),
                **_project_form_kwargs(_project_footage_files(inbox_dir, project)),
            ), 400

        fields, error = _parse_project_form(request)
        if error:
            return _form_error(error)
        data = fields["data"]
        vertical_overlay_file = fields["vertical_overlay_file"]
        horizontal_overlay_file = fields["horizontal_overlay_file"]
        soundtrack_file = fields["soundtrack_file"]
        background_file = fields["background_file"]
        # Renaming isn't supported here — project_name is read-only on the
        # edit form; whatever the URL says for `project` always wins.

        overlays_dir = Path.cwd() / "overlays"
        overlays_dir.mkdir(parents=True, exist_ok=True)

        def _save_overlay(file_storage) -> str:
            fn = secure_filename(f"{project}_{file_storage.filename}")
            dest = overlays_dir / fn
            if dest.exists():
                dest = overlays_dir / f"{dest.stem}_{uuid4().hex[:8]}{dest.suffix}"
            file_storage.save(dest)
            return f"overlays/{dest.name}"

        # One-shot migration: fold legacy overlay keys into the orientation
        # model so a later "remove" isn't masked by the load_config fallback,
        # and a broken legacy `overlay` reference has a home in the new form.
        existing_data = _migrate_overlay_keys(config_path, existing_data)

        # Only touch an overlay/soundtrack/background key when something new was
        # actually uploaded this submission — otherwise leave the key out of
        # `data` so save_config()'s merge preserves the existing reference.
        # A "remove" tick clears it; an upload wins over a remove.
        if vertical_overlay_file is not None:
            data["vertical_overlay"] = _save_overlay(vertical_overlay_file)
        elif fields["vertical_overlay_remove"]:
            data["vertical_overlay"] = None
        if horizontal_overlay_file is not None:
            data["horizontal_overlay"] = _save_overlay(horizontal_overlay_file)
        elif fields["horizontal_overlay_remove"]:
            data["horizontal_overlay"] = None
        if soundtrack_file is not None:
            _SOUNDTRACKS_DIR.mkdir(parents=True, exist_ok=True)
            soundtrack_filename = secure_filename(soundtrack_file.filename)
            soundtrack_path = _SOUNDTRACKS_DIR / soundtrack_filename
            if soundtrack_path.exists():
                soundtrack_path = _SOUNDTRACKS_DIR / f"{soundtrack_path.stem}_{uuid4().hex[:8]}{soundtrack_path.suffix}"
            soundtrack_file.save(soundtrack_path)
            data["soundtrack"] = f"soundtracks/{soundtrack_path.name}"
        if background_file is not None:
            _BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
            fn = secure_filename(f"{project}_{background_file.filename}")
            background_path = _BACKGROUNDS_DIR / fn
            if background_path.exists():
                background_path = _BACKGROUNDS_DIR / f"{background_path.stem}_{uuid4().hex[:8]}{background_path.suffix}"
            background_file.save(background_path)
            data["playback_background"] = f"backgrounds/{background_path.name}"

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

    # ---- iPad / touch remote + PWA -----------------------------------

    @app.get("/manifest.webmanifest")
    def glambot_manifest():
        return jsonify({
            "name": "Glambot",
            "short_name": "Glambot",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0b0b0c",
            "theme_color": "#0b0b0c",
            "icons": [
                {"src": "/icons/192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icons/512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }), 200, {"Content-Type": "application/manifest+json"}

    @app.get("/icons/<int:size>.png")
    def app_icon(size):
        size = max(16, min(1024, size))
        cached = _ICON_CACHE.get(size)
        if cached is None:
            from io import BytesIO
            from PIL import Image
            src = _REPO_ROOT / "logo" / "glambotlogo.png"
            img = Image.open(src).convert("RGBA") if src.exists() else Image.new("RGBA", (size, size), (11, 11, 12, 255))
            img = img.resize((size, size))
            buf = BytesIO()
            img.save(buf, format="PNG")
            cached = buf.getvalue()
            _ICON_CACHE[size] = cached
        return app.response_class(cached, mimetype="image/png")

    @app.get("/remote")
    def remote():
        return render_template("remote.html")

    @app.get("/remote.json")
    def remote_json():
        inbox_dir = app.config["INBOX_DIR"]
        ready = []
        for job in store.list_jobs(status="ready"):
            try:
                config = load_config(inbox_dir / job.project)
                if config.delivery_mode == "qr_only" and config.auto_deliver:
                    continue  # self-delivering; not an operator decision
            except ConfigError:
                pass
            ready.append({
                "id": job.id, "project": job.project, "filename": job.filename,
                "thumb": bool(job.thumbnail_path),
                "recipient_default": job.recipient_email or "",
            })
        # Only the active owner of each (possibly shared) watch folder gets a
        # section here - a non-active peer project's clips would just be
        # operator confusion (see _compute_project_groups / Projects tab).
        active_names = set()
        for group in _compute_project_groups(inbox_dir, store):
            if group["shared"]:
                active_names.add(group["active"])
            else:
                active_names.update(group["projects"])
        count = _kiosk_count(request.args.get("count"))
        projects = []
        for name in sorted({j.project for j in store.list_jobs(status="sent")}):
            if name not in active_names:
                continue
            clips = [j for j in store.list_jobs(project=name, status="sent")
                     if (j.drive_link or j.download_token) and not j.hidden_from_kiosk]
            if not clips:
                continue
            state = store.get_kiosk_state(name)
            projects.append({
                "project": name,
                "paused": state["paused"],
                "live_job_id": state["live_job_id"],
                "has_more": len(clips) > count,
                "clips": [{"id": j.id, "filename": j.filename, "thumb": bool(j.thumbnail_path),
                           "recipient_default": j.recipient_email or ""} for j in clips[:count]],
            })
        resp = jsonify({"ready": ready, "projects": projects})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/remote/jobs/<int:job_id>/approve")
    def remote_approve(job_id):
        job = store.get_job(job_id)
        if job is None or job.status != "ready":
            return jsonify({"ok": False, "error": "not ready"}), 409
        inbox_dir = app.config["INBOX_DIR"]
        try:
            config = load_config(inbox_dir / job.project)
        except ConfigError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        recipient = request.form.get("recipient_email", "").strip()
        delivery_mode = request.form.get("delivery_mode", "").strip()
        try:
            if recipient or delivery_mode:
                # Operator picked an explicit recipient/mode on the remote.
                mode = delivery_mode if delivery_mode in _VALID_DELIVERY_MODES else "email"
                if mode == "email" and (not recipient or "@" not in recipient):
                    return jsonify({"ok": False, "error": "A valid recipient email is required."}), 400
                subject, body = "", ""
                try:
                    raw_s, raw_b = _resolve_email_template(config)
                    subject = resolve_placeholders(raw_s, link="{link}", project=job.project, filename=job.filename)
                    body = resolve_placeholders(raw_b, link="{link}", project=job.project, filename=job.filename)
                except Exception:
                    pass
                deliver(job, config, store, inbox_dir, recipient=recipient or None,
                        subject=subject, body=body, delivery_mode=mode)
            else:
                _retry_one_delivery(job, config, store, inbox_dir)
        except (DriveError, EmailError, DeliveryError) as exc:
            logger.exception("Remote approve failed for job %s", job_id)
            store.update_job(job_id, error=f"Delivery failed: {exc}")
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({"ok": True})

    @app.post("/remote/jobs/<int:job_id>/email")
    def remote_email(job_id):
        """Re-email an already-delivered clip's link (Drive link, or the LAN
        guest link for an offline clip) - no re-upload."""
        job = store.get_job(job_id)
        if job is None or job.status != "sent":
            return jsonify({"ok": False, "error": "That clip isn't available to email."}), 409
        link = _guest_or_drive_link(job)
        if not link:
            return jsonify({"ok": False, "error": "This clip has no shareable link."}), 409
        recipient = request.form.get("recipient_email", "").strip()
        if not recipient or "@" not in recipient:
            return jsonify({"ok": False, "error": "A valid recipient email is required."}), 400
        link2 = job.secondary_drive_link
        try:
            cfg = load_config(app.config["INBOX_DIR"] / job.project)
        except ConfigError:
            cfg = None
        raw_s, raw_b = _resolve_email_template(cfg)
        if not raw_s and not raw_b:
            raw_s, raw_b = "Your video", "Here is your video: {link}"
        subject = resolve_placeholders(raw_s, link=link, project=job.project, filename=job.filename, link2=link2)
        body = resolve_placeholders(raw_b, link=link, project=job.project, filename=job.filename, link2=link2)
        if link2 and "{link2}" not in raw_b:
            body = f"{body}\n\nAlternate version: {link2}"
        try:
            send_delivery_email(recipient=recipient, subject=subject, body=body, link=link, link2=link2)
        except EmailError as exc:
            logger.exception("Remote email of clip %s failed", job_id)
            return jsonify({"ok": False, "error": str(exc)}), 502
        store.update_job(job_id, recipient_email=recipient)
        return jsonify({"ok": True})

    @app.post("/jobs/<int:job_id>/delete")
    def delete_clip(job_id):
        """Delete one clip: its rendered files + its job record. Password-gated
        like project deletion. Used from the /remote page."""
        job = store.get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if job.status not in {"sent", "rejected", "error"}:
            return jsonify({"ok": False, "error": "Only delivered / rejected / errored clips can be deleted."}), 409
        if request.form.get("password", "") != _delete_password():
            return jsonify({"ok": False, "error": "Wrong password."}), 403
        _delete_job_files(job)
        store.delete_job(job_id)
        return jsonify({"ok": True})

    @app.post("/projects/<project>/kiosk/live")
    def set_kiosk_live(project):
        _resolve_project(project)
        raw_id = request.form.get("job_id", "").strip()
        live_id = int(raw_id) if raw_id.isdigit() else None
        paused = request.form.get("paused", "").lower() in {"1", "true", "yes", "on"}
        store.set_kiosk_state(project, live_id, paused)
        return jsonify({"ok": True})

    # ---- Advanced editing: per-clip override + on-demand preview -----

    @app.post("/jobs/<int:job_id>/override")
    def override_job(job_id):
        job = store.get_job(job_id)
        if job is None or job.status not in {"ready", "error", "sent"}:
            flash("That clip can't be edited right now.", "error")
            return redirect(url_for("index"))
        advanced, err = _parse_advanced_fields(request.form)
        if err:
            flash(err, "error")
            return redirect(url_for("index"))
        project_dir = app.config["INBOX_DIR"] / job.project
        config_path = project_dir / "config.json"
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
        except (OSError, json.JSONDecodeError, AssertionError):
            flash("Project config could not be read.", "error")
            return redirect(url_for("index"))
        previous = json.dumps(data)
        overrides = data.setdefault("overrides", {})
        entry = overrides.setdefault(job.filename, {})

        # Grade: a non-neutral grade is stored as an override; a neutral one
        # clears any existing override (back to the project default).
        if advanced["grade"] is not None:
            entry["grade"] = advanced["grade"]
        else:
            entry.pop("grade", None)

        # Speed ramp per clip:
        #  - the ramp editor sent a curve  -> store it as a full per-clip ramp
        #  - the project has a ramp but the clip's ramp toggle is off
        #                                   -> store {"enabled": False} (opt out)
        #  - otherwise                      -> no override (project default)
        project_has_ramp = bool(data.get("speed_ramp", {}).get("enabled"))
        if advanced["speed_ramp"] is not None:
            entry["speed_ramp"] = advanced["speed_ramp"]
        elif project_has_ramp and request.form.get("speed_ramp_enabled") != "on":
            entry["speed_ramp"] = {"enabled": False}
        else:
            entry.pop("speed_ramp", None)

        if not entry:
            overrides.pop(job.filename, None)
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            config = load_config(project_dir)
        except ConfigError as exc:
            config_path.write_text(previous, encoding="utf-8")
            flash(f"Could not apply the edit: {exc}", "error")
            return redirect(url_for("index"))
        # A re-render of an already-delivered clip must re-upload the new
        # output - clear the stale links so deliver() doesn't short-circuit
        # on the old drive_link when it's re-approved.
        if job.status == "sent":
            store.update_job(job_id, drive_link=None, secondary_drive_link=None)
        store.update_job(job_id, status="processing", error=None, progress=0)
        threading.Thread(target=_rerender_worker, args=(store.get_job(job_id), config, store),
                         daemon=True).start()
        flash(f"Re-rendering {job.filename} with the new edit...", "info")
        return redirect(url_for("index") + "#clips")

    @app.post("/jobs/<int:job_id>/advanced-to-project")
    def advanced_to_project(job_id):
        """Push this clip's current Advanced-editing settings (grade + speed
        ramp) up to the project default. Folds the clip's own override in so it
        now inherits the default it just set. Does NOT re-render anything -
        already-rendered clips keep their look until re-rendered, exactly like
        editing the project form."""
        job = store.get_job(job_id)
        if job is None:
            flash("Job not found.", "error")
            return redirect(url_for("index"))
        advanced, err = _parse_advanced_fields(request.form)
        if err:
            flash(err, "error")
            return redirect(url_for("index"))
        project_dir = app.config["INBOX_DIR"] / job.project
        config_path = project_dir / "config.json"
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
        except (OSError, json.JSONDecodeError, AssertionError):
            flash("Project config could not be read.", "error")
            return redirect(url_for("index"))
        previous = json.dumps(data)

        data["grade"] = advanced["grade"]
        data["speed_ramp"] = advanced["speed_ramp"]
        entry = data.get("overrides", {}).get(job.filename)
        if isinstance(entry, dict):
            entry.pop("grade", None)
            entry.pop("speed_ramp", None)
            if not entry:
                data["overrides"].pop(job.filename, None)

        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            load_config(project_dir)
        except ConfigError as exc:
            config_path.write_text(previous, encoding="utf-8")
            flash(f"Could not apply to the project: {exc}", "error")
            return redirect(url_for("index"))
        flash(
            f"Saved these Advanced settings as the default for '{job.project}'. "
            f"Clips already rendered keep their look until re-rendered.", "info",
        )
        return redirect(url_for("index") + "#clips")

    def _do_preview(sample_path: Path, form) -> tuple[str | None, float | None, str | None]:
        from .processor import _resolve_ffmpeg, _probe_duration
        from .effects import Grade, SpeedRamp, build_grade_filter, build_speed_ramp_filtergraph
        ffmpeg_bin = _resolve_ffmpeg()
        if not ffmpeg_bin:
            return None, None, "ffmpeg is not available."
        if not sample_path.exists():
            return None, None, "The sample clip could not be found."
        advanced, err = _parse_advanced_fields(form)
        if err:
            return None, None, err
        full_dur = _probe_duration(sample_path) or 6.0
        dur = min(6.0, full_dur)
        grade = Grade(**advanced["grade"]) if advanced["grade"] else None
        ramp = None
        if advanced["speed_ramp"]:
            sr = advanced["speed_ramp"]
            ramp = SpeedRamp(points=sr["points"], interpolation=sr["interpolation"],
                             smooth_frames=sr["smooth_frames"])
        parts = []
        src = "[0:v]"
        if ramp:
            frag, out_label, _ = build_speed_ramp_filtergraph(ramp, "[0:v]", dur)
            if frag:
                parts.append(frag)
                src = out_label
        grade_str = build_grade_filter(grade, ffmpeg_bin)
        chain = f"{src}scale=480:-2:force_original_aspect_ratio=decrease"
        if grade_str:
            chain += f",{grade_str}"
        parts.append(chain + "[v]")
        _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_previews()
        name = f"{uuid4().hex}.mp4"
        out_path = _PREVIEW_DIR / name
        cmd = [ffmpeg_bin, "-y", "-t", f"{dur:.2f}", "-i", str(sample_path),
               "-filter_complex", ";".join(parts), "-map", "[v]", "-an",
               "-preset", "ultrafast", "-crf", "28", "-movflags", "+faststart", str(out_path)]
        try:
            import subprocess
            from .processor import _NO_WINDOW_FLAGS
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                                    creationflags=_NO_WINDOW_FLAGS)
        except subprocess.SubprocessError as exc:
            return None, None, f"Preview render failed: {exc}"
        if result.returncode != 0 or not out_path.exists():
            logger.warning("Preview ffmpeg failed: %s", "\n".join(result.stderr.splitlines()[-8:]))
            return None, None, "Preview render failed."
        return name, full_dur, None

    @app.post("/projects/<project>/preview")
    def preview_project(project):
        project_dir = _resolve_project(project)
        samples = _project_footage_files(app.config["INBOX_DIR"], project)
        chosen = request.form.get("preview_sample", "").strip()
        if chosen and chosen not in samples:
            return jsonify({"ok": False, "error": "Unknown sample clip."}), 400
        target = chosen or (samples[0] if samples else None)
        if not target:
            return jsonify({"ok": False, "error": "No footage in this project to preview yet."}), 400
        for base in (project_watch_dirs(app.config["INBOX_DIR"]).get(project, project_dir),
                     _resolve_output_footage(project_dir)):
            if base and (base / target).exists():
                sample_path = base / target
                break
        else:
            return jsonify({"ok": False, "error": "Sample clip not found."}), 400
        name, duration, err = _do_preview(sample_path, request.form)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        from .processor import _effective_source_fps
        try:
            _cfg = load_config(project_dir)
            trim = _cfg.trim_for(target)
            src_fps = _effective_source_fps(sample_path, _cfg.source_fps)
        except ConfigError:
            trim, src_fps = None, None
        return jsonify({"ok": True, "url": url_for("serve_preview", project=project, name=name),
                        "duration": _trimmed_preview_duration(sample_path, trim, duration, src_fps)})

    @app.post("/jobs/<int:job_id>/preview")
    def preview_job(job_id):
        job = store.get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        try:
            config = load_config(app.config["INBOX_DIR"] / job.project)
        except ConfigError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        from .processor import _resolve_source, _effective_source_fps
        sample_path = _resolve_source(job, config)
        name, duration, err = _do_preview(sample_path, request.form)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "url": url_for("serve_preview", project=job.project, name=name),
                        "duration": _trimmed_preview_duration(
                            sample_path, config.trim_for(job.filename), duration,
                            _effective_source_fps(sample_path, config.source_fps))})

    @app.get("/projects/<project>/preview/<name>")
    def serve_preview(project, name):
        if secure_filename(name) != name or not name.endswith(".mp4"):
            abort(404)
        path = _PREVIEW_DIR / name
        if not path.exists():
            abort(404)
        return send_file(path, conditional=True)

    @app.get("/soundtracks/<name>")
    def serve_soundtrack(name):
        """Serve a library track so the project form's Test button can audition
        it. The name is whitelisted against the actual library listing, which
        doubles as the path-traversal guard."""
        if secure_filename(name) != name or name not in set(_list_soundtracks()):
            abort(404)
        return send_file(_SOUNDTRACKS_DIR / name, conditional=True)

    @app.post("/soundtracks/<name>/delete")
    def delete_soundtrack(name):
        """Remove an uploaded library track. Refuses while any project still
        references it (checked against the raw config.json so a project with a
        currently-broken config still counts). Gated by the same password as
        project deletion."""
        back = request.referrer or url_for("new_project_form")
        if secure_filename(name) != name or name not in set(_list_soundtracks()):
            abort(404)
        if request.form.get("password", "") != _delete_password():
            flash("Wrong password - the soundtrack was not deleted.", "error")
            return redirect(back)
        users = sorted(
            project_dir.name
            for project_dir in all_project_dirs(app.config["INBOX_DIR"])
            if _raw_soundtrack_name(project_dir) == name
        )
        if users:
            flash(
                f"'{name}' is still used by: {', '.join(users)}. Change those projects' "
                f"soundtrack first, then delete it.", "error",
            )
            return redirect(back)
        try:
            (_SOUNDTRACKS_DIR / name).unlink(missing_ok=True)
        except OSError as exc:
            flash(f"Could not delete '{name}': {exc}", "error")
            return redirect(back)
        flash(f"Deleted soundtrack '{name}' from the library.", "info")
        return redirect(back)

    return app


_KIOSK_PAGE = 8


def _kiosk_count(raw) -> int:
    """Requested kiosk-grid size from ?count= — clamped, defaults to one page."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _KIOSK_PAGE
    return max(_KIOSK_PAGE, min(n, 200))


def _guest_or_drive_link(job) -> str | None:
    """The URL a guest should scan for this clip: the LAN download page when the
    clip has a token (project uses LAN delivery), else its Google Drive link."""
    if job.download_token:
        return f"{lan.public_base_url().rstrip('/')}/d/{job.download_token}"
    return job.drive_link


def _wifi_qr_data_uri() -> str | None:
    """A 'join this Wi-Fi' QR for kiosk screens, when LAN_SSID is configured."""
    ssid = lan.wifi_ssid()
    if not ssid:
        return None
    return make_wifi_qr_data_uri(lan.wifi_qr_payload(ssid, lan.wifi_password()))


def _raw_soundtrack_name(project_dir: Path) -> str | None:
    """The bare filename of the soundtrack a project references, read straight
    from config.json without validation."""
    try:
        data = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    soundtrack = data.get("soundtrack")
    return Path(soundtrack).name if soundtrack else None


def _resolve_secret(inbox_dir: Path) -> str:
    """Signed session cookies (the operator PIN, guest download PINs) must
    survive a restart, so a fixed dev-string default won't do once a PIN is in
    play. Prefer an explicit FLASK_SECRET; otherwise keep a random one on disk
    next to the SQLite db (that folder already exists and is per-install)."""
    explicit = os.environ.get("FLASK_SECRET", "").strip()
    if explicit:
        return explicit
    secret_path = inbox_dir / ".glambot" / "secret"
    try:
        if secret_path.exists():
            existing = secret_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        secret_path.write_text(value, encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        return value
    except OSError:
        logger.warning("Could not persist a Flask secret at %s - using an ephemeral one", secret_path)
        return secrets.token_hex(32)


def _delete_password() -> str:
    """Guard against a misclick, not a security control - the default is
    visible in this source file and the app binds to localhost. It exists so
    that deleting a project takes deliberate typing."""
    return os.environ.get("GLAMBOT_DELETE_PASSWORD", "glambot")


def _deletion_targets(inbox_dir: Path, project: str, project_dir: Path,
                      store: JobStore) -> list[dict]:
    """Every path deleting `project` would touch, each flagged with whether
    it will actually be removed and why.

    The important case this handles: a footage folder shared with another
    project must never be emptied. Deleting one project cannot be allowed to
    destroy another project's incoming footage, so a shared source folder is
    listed as skipped rather than silently spared or silently wiped."""
    targets: list[dict] = []
    seen: set[Path] = set()

    def add(path: Path, label: str, will_delete: bool, kind: str, note: str = "") -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        targets.append({
            "path": resolved, "label": label, "will_delete": will_delete,
            "kind": kind, "note": note, "exists": resolved.exists(),
        })

    add(project_dir, "Project folder (config, overlays, soundtrack)", True, "project")

    try:
        config = load_config(project_dir)
    except ConfigError:
        config = None

    if config is not None:
        from .processor import resolve_output_base
        output_base = resolve_output_base(project_dir, config)
        # When no custom output_dir is set this IS the project folder, which
        # add() already deduplicates away.
        add(output_base, "Rendered output folder", True, "output")

        source = config.source_dir
        if source is not None:
            others = sorted(
                name for name, folder in project_watch_dirs(inbox_dir).items()
                if name != project and folder.resolve() == source.resolve()
            )
            if others:
                add(source, "Footage source folder", False, "footage",
                    f"shared with {', '.join(others)} - left untouched")
            else:
                add(source, "Footage source folder (imported clips)", True, "footage")
    return targets


def _all_deletion_targets(inbox_dir: Path, store: JobStore) -> list[dict]:
    """Per-project deletion targets for the delete-everything page.

    Unlike the single-project page, sharing is not used to protect anything
    here -- if every project is going, there is no surviving project left to
    protect a shared folder for. Instead every path is offered as a checkbox
    and footage folders start unticked, so raw footage is only ever deleted
    by an explicit tick. Sharing is still reported, because seeing that one
    folder feeds four projects is exactly what makes that tick a considered
    one."""
    groups = []
    for project_dir in all_project_dirs(inbox_dir):
        name = project_dir.name
        targets = _deletion_targets(inbox_dir, name, project_dir, store)
        for t in targets:
            # Footage is the user's raw material and the one thing Glambot
            # cannot regenerate, so it never starts ticked.
            t["default_checked"] = t["kind"] != "footage"
        groups.append({
            "project": name,
            "targets": targets,
            "job_count": len(store.list_jobs(project=name)),
        })
    return groups


def _delete_paths(paths) -> tuple[int, list[str]]:
    """Delete each path, collecting failures rather than aborting partway --
    a locked file must not leave the rest of a confirmed deletion undone."""
    removed, failed = 0, []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed += 1
            elif path.exists():
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Could not delete %s", path, exc_info=True)
            failed.append(f"{path} ({exc.strerror or exc})")
    return removed, failed


def _rerender_worker(job: Job, config, store: JobStore) -> None:
    """Run one job's render off the request thread. process_job imports
    lazily here because processor.py pulls in ffmpeg helpers that the web
    app doesn't otherwise need."""
    from .processor import process_job
    try:
        process_job(job, config, store)
    except Exception as exc:
        logger.exception("Re-render failed for job %s", job.id)
        store.mark_error(job.id, f"Re-render failed: {exc}")


def _rerender_all_worker(targets: list, store: JobStore) -> None:
    """Bulk version of _rerender_worker - one ffmpeg render at a time (not
    parallel), same reasoning as the watcher's own single-worker design."""
    from .processor import process_job
    for job, config in targets:
        current = store.get_job(job.id)
        if current is None:
            continue
        try:
            process_job(current, config, store)
        except Exception as exc:
            logger.exception("Bulk re-render failed for job %s", current.id)
            store.mark_error(current.id, f"Re-render failed: {exc}")


def _retry_one_delivery(job: Job, config, store: JobStore, inbox_dir: Path) -> None:
    """Re-run automatic delivery for one job whose auto-delivery previously
    failed. Raises DriveError/EmailError/DeliveryError on failure - callers
    decide how to surface/record it. Shared by the single-clip
    retry_delivery route and the bulk retry_all_failed_deliveries route so
    both run the exact same sequence."""
    subject, body = "", ""
    try:
        raw_subject, raw_body = _resolve_email_template(config)
        subject = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
        body = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
    except Exception:
        pass
    deliver(
        job, config, store, inbox_dir,
        recipient=config.recipient_email or None,
        subject=subject, body=body, delivery_mode=config.delivery_mode,
    )


def _delete_job_files(job: Job) -> None:
    """Unlink a job's rendered outputs + thumbnails from disk. Leaves the DB
    row alone - callers decide whether to also store.delete_job()."""
    for attr in ("output_path", "secondary_output_path", "thumbnail_path"):
        p = getattr(job, attr, None)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    if job.thumbnail_path and job.output_path:
        photo = Path(job.thumbnail_path).with_name(Path(job.output_path).stem + "_download.jpg")
        try:
            photo.unlink(missing_ok=True)
        except OSError:
            pass


def _import_worker(folder: Path, filenames: list, dest_dir: Path, watcher: InboxWatcher,
                   decisions: dict, store: JobStore, project: str) -> None:
    """Move selected external files into a project's watch folder, then nudge
    an immediate rescan. Each moved file gets a force-import marker so the
    watcher renders it even if identical footage was already processed - that
    is the whole point of a hand-picked import. `decisions[name]` is 'rename'
    or 'replace' when the operator resolved a collision ('skip' files were
    already filtered out by the caller)."""
    for name in filenames:
        src = folder / name
        if not src.exists():
            continue
        decision = decisions.get(name, "")

        # Already sitting in the watch folder (the operator browsed to it there
        # to force a re-render): don't move/rename it - just re-queue in place.
        if src.resolve() == (dest_dir / name).resolve():
            store.mark_forced_import(str(src.resolve()))
            logger.info("Re-queued in place: %s", src)
            continue

        if decision == "replace":
            digest = content_hash(src)
            existing = store.find_by_hash(digest, project) if digest else None
            if existing is not None:
                _delete_job_files(existing)
                store.delete_job(existing.id)

        dest = dest_dir / name
        if dest.exists():
            if decision == "replace":
                try:
                    dest.unlink()
                except OSError:
                    logger.exception("Could not overwrite %s", dest)
                    continue
            else:  # 'rename', or an unresolved collision - keep both
                dest = dest_dir / f"{src.stem}_{uuid4().hex[:8]}{src.suffix}"
        try:
            shutil.move(str(src), str(dest))
            store.mark_forced_import(str(dest.resolve()))
            logger.info("Imported %s -> %s", src, dest)
        except OSError:
            logger.exception("Could not import %s", src)
    watcher.rescan_now()


def _retry_all_deliveries_worker(targets: list, store: JobStore, inbox_dir: Path) -> None:
    """Bulk version of _retry_one_delivery - sequential, not parallel, so a
    pile of stuck clips from an outage doesn't fire off many concurrent
    Drive uploads at once."""
    for job, config in targets:
        current = store.get_job(job.id)
        if current is None or current.status != "ready":
            continue
        try:
            _retry_one_delivery(current, config, store, inbox_dir)
        except (DriveError, EmailError, DeliveryError) as exc:
            logger.warning("Bulk retry delivery failed for job %s: %s", current.id, exc)
            store.update_job(current.id, error=f"Auto-delivery failed: {exc}")


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

    # Guest-download PIN per project (for the Projects tab), best-effort.
    guest_pins: dict[str, str] = {}
    for project_dir in all_project_dirs(inbox_dir):
        try:
            cfg = load_config(project_dir)
        except ConfigError:
            continue
        if (cfg.lan_delivery or cfg.offline_mode) and cfg.download_pin:
            guest_pins[project_dir.name] = cfg.download_pin

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
            "guest_pins": guest_pins,
        })
    return sorted(result, key=lambda g: g["folder"])


def _project_quick_toggles(inbox_dir: Path) -> list[dict]:
    """Every valid project's current orientation + delivery mode, for the
    Clips tab's quick switch bar - alphabetical, skips a project with a
    broken config.json (already surfaced via its Projects-tab Edit link).

    `mode_togglable` is False for LAN / Offline projects: `auto_deliver` is
    one of four mutually-exclusive modes, so a bare standard<->auto flip
    would be ambiguous for those - they keep using the full settings form."""
    items = []
    for project_dir in all_project_dirs(inbox_dir):
        try:
            cfg = load_config(project_dir)
        except ConfigError:
            continue
        items.append({
            "name": project_dir.name,
            "is_vertical": cfg.height > cfg.width,
            "is_auto": cfg.auto_deliver,
            "mode_togglable": not (cfg.lan_delivery or cfg.offline_mode),
        })
    return sorted(items, key=lambda x: x["name"])


def _list_soundtracks() -> list[str]:
    if not _SOUNDTRACKS_DIR.exists():
        return []
    return sorted(
        p.name for p in _SOUNDTRACKS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def _project_form_kwargs(preview_samples: list | None = None) -> dict:
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
        "preview_samples": preview_samples or [],
        "default_email_subject": _safe_default_template()[0],
        "default_email_body": _safe_default_template()[1],
    }


def _safe_default_template() -> tuple[str, str]:
    try:
        return load_default_template()
    except Exception:
        return "", ""


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

    src_fps = data.get("source_fps")
    values["source_fps"] = ("%g" % src_fps) if src_fps else ""

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

    # Orientation-keyed overlay placement, each falling back to the matching
    # legacy overlay group so an old project pre-fills sensibly.
    def _overlay_values(prefix: str, *legacy_prefixes: str) -> None:
        keys = ("position", "scale", "x", "y")
        chosen = prefix
        if data.get(f"{prefix}overlay_position") is None and data.get(f"{prefix}overlay_scale") is None:
            for lp in legacy_prefixes:
                if any(data.get(f"{lp}overlay_{k}") is not None for k in keys):
                    chosen = lp
                    break
        values[f"{prefix}overlay_position"] = data.get(f"{chosen}overlay_position", "full")
        for k in ("scale", "x", "y"):
            v = data.get(f"{chosen}overlay_{k}")
            if v is not None:
                values[f"{prefix}overlay_{k}"] = str(v)

    _overlay_values("vertical_", "")
    _overlay_values("horizontal_", "second_", "")

    values["rotation"] = str(data.get("rotation", 0))
    values["position_x"] = str(data.get("position_x", 0))
    values["position_y"] = str(data.get("position_y", 0))

    # Collapse the three delivery booleans into the exclusive Mode radio.
    if data.get("offline_mode"):
        values["mode"] = "offline"
    elif data.get("lan_delivery"):
        values["mode"] = "lan"
    elif data.get("auto_deliver"):
        values["mode"] = "auto"
    else:
        values["mode"] = "standard"
    values["download_pin"] = data.get("download_pin") or ""
    values["email_subject"] = data.get("email_subject") or ""
    values["email_body"] = data.get("email_body") or ""

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

    grade = data.get("grade") or {}
    values["exposure"] = str(grade.get("exposure", 0))
    values["contrast"] = str(grade.get("contrast", 1))
    values["white_balance"] = str(grade.get("white_balance", 0))
    speed_ramp = data.get("speed_ramp") or {}
    if speed_ramp.get("enabled"):
        values["speed_ramp_enabled"] = "on"
        values["speed_ramp_json"] = json.dumps({
            "points": speed_ramp.get("points", []),
            "interpolation": speed_ramp.get("interpolation", "smooth"),
            "smooth_frames": speed_ramp.get("smooth_frames", False),
            "max_speed": speed_ramp.get("max_speed", 40.0),
        })

    values["source_dir"] = data.get("source_dir") or ""
    values["output_dir"] = data.get("output_dir") or ""
    values["drive_folder_id"] = data.get("drive_folder_id") or ""
    values["playback_background_opacity"] = str(data.get("playback_background_opacity", 50))

    return values


def _adv_values(config, filename: str) -> dict:
    """Prefill for the shared advanced-editor macro (templates/_advanced_editor.html)
    - the clip's *effective* grade + speed ramp (per-clip override if set,
    else the project default)."""
    eff_grade = config.grade_for(filename) if config else None
    eff_ramp = config.speed_ramp_for(filename) if config else None
    values = {
        "exposure": eff_grade.exposure if eff_grade else 0,
        "contrast": eff_grade.contrast if eff_grade else 1,
        "white_balance": eff_grade.white_balance if eff_grade else 0,
        "speed_ramp_enabled": "on" if eff_ramp else "",
        "speed_ramp_json": "",
    }
    if eff_ramp:
        values["speed_ramp_json"] = _ramp_json(eff_ramp)

    # The raw project-level grade + ramp, so the per-clip editor's "Match
    # project" reset knows what to fall back to (independent of any override).
    proj_grade = getattr(config, "grade", None) if config else None
    proj_ramp = getattr(config, "speed_ramp", None) if config else None
    values["adv_project_json"] = json.dumps({
        "exposure": proj_grade.exposure if proj_grade else 0,
        "contrast": proj_grade.contrast if proj_grade else 1,
        "white_balance": proj_grade.white_balance if proj_grade else 0,
        "speed_ramp": json.loads(_ramp_json(proj_ramp)) if proj_ramp else None,
    })
    return values


def _ramp_json(ramp) -> str:
    return json.dumps({
        "points": ramp.points,
        "interpolation": ramp.interpolation,
        "smooth_frames": ramp.smooth_frames,
        "max_speed": getattr(ramp, "max_speed", 40.0),
    })


def _trimmed_preview_duration(sample_path: Path, trim, fallback: float | None,
                              source_fps: float | None = None) -> float | None:
    """The clip's length after any Basic-settings trim (and raw-footage frame
    rate reinterpretation) - what the ramp editor's graph should be labelled
    with. Falls back to the untrimmed length."""
    from .processor import _effective_duration
    start = getattr(trim, "start", None) if trim else None
    end = getattr(trim, "end", None) if trim else None
    if not start and not end and not source_fps:
        return fallback
    try:
        return _effective_duration(sample_path, start, end, source_fps) or fallback
    except OSError:
        return fallback


def _delivered_clip_prefill(job: Job, config) -> dict:
    """Editable recipient / subject / body defaults for re-emailing an
    already-delivered clip - shared by the /clips page and the review
    page's Recently Sent list. {link} stays literal (resolved at send)."""
    subject_default, body_default = "", ""
    try:
        raw_subject, raw_body = _resolve_email_template(config)
        subject_default = resolve_placeholders(raw_subject, link="{link}", project=job.project, filename=job.filename)
        body_default = resolve_placeholders(raw_body, link="{link}", project=job.project, filename=job.filename)
    except Exception:
        pass
    return {
        "recipient_default": job.recipient_email or "",
        "subject_default": subject_default,
        "body_default": body_default,
    }


def _build_card(job: Job, inbox_dir: Path) -> dict:
    """Assemble everything the template needs to render one review card,
    including the editable recipient/subject/body prefill values."""
    project_dir = inbox_dir / job.project
    recipient_default = job.recipient_email or ""
    delivery_mode_default = job.delivery_mode or "email"
    config_error = None
    config = None
    is_vertical = None
    eff_grade = None
    project_has_ramp = False
    ramp_disabled_here = False
    missing_assets: list[str] = []
    try:
        config = load_config(project_dir)
        recipient_default = job.recipient_email or config.recipient_email
        delivery_mode_default = job.delivery_mode or config.delivery_mode
        is_vertical = config.height > config.width
        eff_grade = config.grade_for(job.filename)
        project_has_ramp = config.speed_ramp is not None
        override = (config.overrides or {}).get(job.filename, {})
        ramp_disabled_here = "speed_ramp" in override and not override["speed_ramp"].get("enabled", False)
        missing_assets = list(config.missing_assets)
    except ConfigError as exc:
        config_error = str(exc)

    subject_default, body_default = "", ""
    try:
        raw_subject, raw_body = _resolve_email_template(config)
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
        "missing_assets": missing_assets,
        "render_seconds": _render_seconds(job),
        "is_vertical": is_vertical,
        "adv_exposure": eff_grade.exposure if eff_grade else 0,
        "adv_contrast": eff_grade.contrast if eff_grade else 1,
        "adv_white_balance": eff_grade.white_balance if eff_grade else 0,
        "adv_values": _adv_values(config, job.filename),
        "project_has_ramp": project_has_ramp,
        "ramp_disabled_here": ramp_disabled_here,
    }


def _migrate_overlay_keys(config_path: Path, data: dict) -> dict:
    """Rename any legacy `overlay*` / `second_overlay*` keys to the
    orientation-keyed `vertical_overlay*` / `horizontal_overlay*` form and
    write the result back. Idempotent - does nothing once migrated. Returns
    the (possibly updated) dict."""
    migrated = dict(data)
    changed = False
    _suffixes = ("", "_position", "_scale", "_x", "_y")
    if "overlay" in migrated and "vertical_overlay" not in migrated:
        for s in _suffixes:
            if f"overlay{s}" in migrated:
                migrated[f"vertical_overlay{s}"] = migrated.pop(f"overlay{s}")
        changed = True
    if "second_overlay" in migrated and "horizontal_overlay" not in migrated:
        for s in _suffixes:
            if f"second_overlay{s}" in migrated:
                migrated[f"horizontal_overlay{s}"] = migrated.pop(f"second_overlay{s}")
        changed = True
    if changed:
        try:
            config_path.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("Could not persist overlay-key migration for %s", config_path)
            return data
    return migrated


def _resolve_email_template(config) -> tuple[str, str]:
    """(subject, body) for a project's outgoing emails: the project's own
    template where set, otherwise the global templates/email_default.txt."""
    try:
        subject, body = load_default_template()
    except Exception:
        subject, body = "", ""
    if config is not None:
        subject = config.email_subject or subject
        body = config.email_body or body
    return subject, body


def _render_seconds(job: Job) -> float | None:
    """Wall-clock time from job creation to its last update, in this
    single-worker pipeline that's the render duration once a job reaches
    'ready' - created_at/updated_at start equal (see JobStore.create_job)
    and updated_at is refreshed exactly when mark_ready() runs."""
    try:
        started = datetime.fromisoformat(job.created_at)
        finished = datetime.fromisoformat(job.updated_at)
    except (TypeError, ValueError):
        return None
    return max(0.0, (finished - started).total_seconds())


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


def _parse_advanced_fields(form) -> tuple[dict | None, str | None]:
    """Parse the Advanced-editing controls (grade + speed ramp) from either the
    project form or the per-clip override form. Returns
    ({"grade": <dict|None>, "speed_ramp": <dict|None>}, None) or (None, error)."""
    try:
        exposure = float(form.get("exposure", "0") or 0)
        contrast = float(form.get("contrast", "1") or 1)
        white_balance = int(float(form.get("white_balance", "0") or 0))
    except ValueError:
        return None, "Exposure, contrast and white balance must be numbers."
    if not (-2.0 <= exposure <= 2.0 and 0.5 <= contrast <= 2.0 and -100 <= white_balance <= 100):
        return None, "Exposure (-2..2), contrast (0.5..2) or white balance (-100..100) is out of range."
    grade = None
    if abs(exposure) > 1e-3 or abs(contrast - 1.0) > 1e-3 or white_balance != 0:
        grade = {"exposure": round(exposure, 3), "contrast": round(contrast, 3),
                 "white_balance": white_balance}

    speed_ramp = None
    if form.get("speed_ramp_enabled") == "on":
        from .effects import DEFAULT_MAX_SPEED, HARD_MAX_SPEED
        raw = form.get("speed_ramp_json", "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return None, "Speed ramp curve data is invalid."
        points = parsed.get("points") if isinstance(parsed, dict) else parsed
        if not isinstance(points, list) or len(points) < 2:
            return None, "The speed ramp needs at least two points."

        # "2" / "4" / "8" / "40" / "custom" + a free number for custom.
        raw_max = form.get("speed_ramp_max_speed", "").strip()
        if raw_max == "custom":
            raw_max = form.get("speed_ramp_max_speed_custom", "").strip()
        try:
            max_speed = float(raw_max) if raw_max else DEFAULT_MAX_SPEED
        except ValueError:
            return None, "Max speed must be a number."
        max_speed = max(2.0, min(HARD_MAX_SPEED, max_speed))

        def _pt(p):
            out = {"t": float(p["t"]), "speed": min(max_speed, max(0.1, float(p["speed"])))}
            for key in ("hl", "hr"):
                h = p.get(key)
                if isinstance(h, (list, tuple)) and len(h) == 2:
                    out[key] = [float(h[0]), float(h[1])]
            return out
        try:
            clean = sorted((_pt(p) for p in points), key=lambda p: p["t"])
        except (KeyError, TypeError, ValueError):
            return None, "Speed ramp points must each have a numeric t and speed."
        speed_ramp = {
            "enabled": True,
            "points": clean,
            "interpolation": form.get("speed_ramp_interpolation", "smooth"),
            "smooth_frames": form.get("speed_ramp_smooth_frames") == "on",
            "max_speed": max_speed,
        }
    return {"grade": grade, "speed_ramp": speed_ramp}, None


def _project_footage_files(inbox_dir: Path, project: str) -> list[str]:
    """Footage filenames available to preview for a project - both freshly
    dropped clips and already-archived originals."""
    project_dir = inbox_dir / project
    if not (project_dir / "config.json").exists():
        return []
    try:
        config = load_config(project_dir)
    except ConfigError:
        config = None
    dirs = [project_watch_dirs(inbox_dir).get(project, project_dir)]
    from .processor import resolve_output_base
    if config is not None:
        dirs.append(resolve_output_base(project_dir, config) / "Footage")
    names: set[str] = set()
    for d in dirs:
        if d and d.is_dir():
            names.update(p.name for p in d.iterdir() if p.is_file() and is_footage_file(p))
    return sorted(names)


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

    raw_source_fps = form.get("source_fps", "").strip()
    source_fps = None
    if raw_source_fps:
        try:
            source_fps = float(raw_source_fps)
        except ValueError:
            return None, "Source frame rate must be a number."
        if not (0 < source_fps <= 1000):
            return None, "Source frame rate must be between 0 and 1000."

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

    # Overlays are optional and keyed by orientation - the renderer picks the
    # one matching each output's aspect ratio.
    vertical_overlay_file, vertical_overlay_updates, err = _parse_overlay_group_form(form, req.files, "vertical_")
    if err:
        return None, err
    horizontal_overlay_file, horizontal_overlay_updates, err = _parse_overlay_group_form(form, req.files, "horizontal_")
    if err:
        return None, err

    # --- Playback reel background (optional; falls back to the Glambot logo) ---
    background_file = req.files.get("background_file")
    if background_file is not None and not background_file.filename:
        background_file = None
    background_opacity_raw = form.get("playback_background_opacity", "").strip()
    background_opacity = 50
    if background_opacity_raw:
        try:
            background_opacity = int(background_opacity_raw)
        except ValueError:
            return None, "Background opacity must be a whole number."
    if not (0 <= background_opacity <= 100):
        return None, "Background opacity must be between 0 and 100."

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

    # --- Mode (one exclusive choice) ------------------------------------
    # standard | auto | lan | offline -> the three booleans stored in config.json.
    mode = form.get("mode", "standard")
    if mode not in {"standard", "auto", "lan", "offline"}:
        return None, "Invalid mode selection."
    auto_deliver = mode == "auto"
    lan_delivery = mode == "lan"
    offline_mode = mode == "offline"
    download_pin = form.get("download_pin", "").strip() or None
    if mode in {"lan", "offline"} and not (download_pin and re.match(r"^\d{4,8}$", download_pin)):
        return None, "A 4-8 digit guest download PIN is required for the Wi-Fi / offline modes."

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

    # --- Dual-resolution export -----------------------------------------
    # The second resolution reuses whichever orientation overlay matches it -
    # no separate overlay controls any more.
    second_resolution_enabled = form.get("second_resolution_enabled") == "on"
    second_resolution = None
    second_bitrate = None
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
        "source_fps": source_fps,
        "rotation": rotation,
        "position_x": position_x if position_x is not None else 0,
        "position_y": position_y if position_y is not None else 0,
        "auto_deliver": auto_deliver,
        "lan_delivery": lan_delivery,
        "offline_mode": offline_mode,
        "download_pin": download_pin,
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
        "playback_background_opacity": background_opacity,
    }
    # Overlay position/scale/x/y are saved even without a new upload, so an
    # existing overlay's placement can be nudged/resized on its own (edit
    # mode). Harmless on create with no overlay — the keys just go unused.
    data.update(vertical_overlay_updates)
    data.update(horizontal_overlay_updates)

    advanced, adv_error = _parse_advanced_fields(form)
    if adv_error:
        return None, adv_error
    data["grade"] = advanced["grade"]
    data["speed_ramp"] = advanced["speed_ramp"]

    # Per-project email template (blank clears it, back to email_default.txt).
    data["email_subject"] = form.get("email_subject", "").strip() or None
    data["email_body"] = form.get("email_body", "") or None

    # soundtrack: an explicit "None" selection clears it; an existing-file
    # choice sets it; "__upload__" is left for the caller to fill in once the
    # uploaded file is actually saved to disk.
    if soundtrack_choice == "__none__":
        data["soundtrack"] = None
    elif soundtrack_rel_path is not None:
        data["soundtrack"] = soundtrack_rel_path

    return {
        "data": data,
        "vertical_overlay_file": vertical_overlay_file,
        "horizontal_overlay_file": horizontal_overlay_file,
        "vertical_overlay_remove": form.get("vertical_overlay_remove") == "on",
        "horizontal_overlay_remove": form.get("horizontal_overlay_remove") == "on",
        "soundtrack_file": soundtrack_file,
        "background_file": background_file,
        "name": name,
    }, None
