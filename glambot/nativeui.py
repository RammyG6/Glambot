"""Native OS file/folder picker, run on the machine that hosts Glambot.

A browser can't hand back a real filesystem path, so the *server* pops the
dialog. This only makes sense when the operator's browser and the Glambot
server are the same machine - the normal single-PC setup; the `/pick` route
is loopback-only and the pages fall back to a typed path otherwise.

Backends, tried in order:
  1. the packaged Windows app runs Flask inside a pywebview window - reuse its
     native `create_file_dialog()` (registered by `glambot_launcher`);
  2. the dev CLI has no such window - shell out to a tiny tkinter dialog
     (`python -m glambot._pickdialog ...`), which the frozen build can't do
     but never needs;
  3. macOS `osascript` / Linux `zenity`.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .processor import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

_webview_window = None


def register_webview_window(window) -> None:
    """Called once by glambot_launcher after webview.create_window(), so
    pick() can raise the OS dialog on top of the app window."""
    global _webview_window
    _webview_window = window


def pick(kind: str, initial: str = "") -> list[str] | None:
    """Return the operator's selection (a 1-item list for `kind="folder"`,
    possibly many for `kind="footage"`), `[]` if they cancelled, or `None`
    if no native dialog is available here."""
    if _webview_window is not None:
        try:
            return _pick_webview(kind, initial)
        except Exception:
            logger.exception("pywebview file dialog failed - falling back")

    if not getattr(sys, "frozen", False):
        picked = _pick_subprocess(kind, initial)
        if picked is not None:
            return picked

    if sys.platform == "darwin":
        return _pick_osascript(kind, initial)
    if sys.platform.startswith("linux"):
        return _pick_zenity(kind, initial)
    return None


def _pick_webview(kind: str, initial: str) -> list[str]:
    import webview

    dialog_type = webview.OPEN_DIALOG if kind == "footage" else webview.FOLDER_DIALOG
    file_types = ()
    if kind == "footage":
        patterns = ";".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))
        file_types = (f"Footage ({patterns})", "All files (*.*)")
    result = _webview_window.create_file_dialog(
        dialog_type,
        directory=initial or "",
        allow_multiple=(kind == "footage"),
        file_types=file_types,
    )
    return list(result) if result else []


def _pick_subprocess(kind: str, initial: str) -> list[str] | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "glambot._pickdialog", kind, initial],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("could not run the tkinter picker subprocess")
        return None
    if proc.returncode != 0:
        logger.error("picker subprocess exited %s: %s", proc.returncode, proc.stderr.strip())
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _pick_osascript(kind: str, initial: str) -> list[str]:
    if kind == "footage":
        script = 'set xs to choose file with multiple selections allowed\n' \
                 'set out to ""\nrepeat with x in xs\nset out to out & POSIX path of x & "\n"\nend repeat\nreturn out'
    else:
        script = "return POSIX path of (choose folder)"
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:  # includes "User canceled"
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _pick_zenity(kind: str, initial: str) -> list[str] | None:
    args = ["zenity", "--file-selection"]
    if kind == "folder":
        args.append("--directory")
    else:
        args += ["--multiple", "--separator=\n"]
    if initial and Path(initial).is_dir():
        args.append(f"--filename={initial.rstrip('/')}/")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]
