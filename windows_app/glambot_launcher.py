"""Entry point for the packaged Windows app (built by glambot.spec).

Replaces `python -m glambot.pipeline` + a browser tab with: a native window
(pywebview) showing the same Flask app, a system tray icon (since there's no
console window to show logs in or Ctrl+C to quit from), and file-based
logging. Not used by `run.sh` / `Glambot.bat` / the Mac app - those keep
running the plain CLI entry point (`glambot/pipeline.py`) unchanged.

Reads the data folder (where .env / credentials.json / project/ / overlays/
etc. live, separate from wherever this .exe itself is installed) from
`datadir.txt`, written next to the .exe by the Inno Setup installer.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Dev-mode convenience: `python windows_app/glambot_launcher.py` from a
    # checkout needs the repo root on sys.path to find the `glambot` package
    # (a frozen build gets this from PyInstaller's Analysis instead).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent


def _fatal(message: str) -> None:
    """Show a native message box - there's no console window to print to in
    the packaged (windowed) build - then exit."""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "Glambot", 0x10)  # MB_ICONERROR
    except Exception:
        pass
    sys.exit(1)


def _resolve_data_dir() -> Path:
    marker = APP_DIR / "datadir.txt"
    if not marker.exists():
        _fatal(
            "Glambot can't find datadir.txt next to its own .exe - this "
            "install looks corrupted. Try reinstalling Glambot."
        )
    data_dir = Path(marker.read_text(encoding="utf-8").strip())
    if not data_dir.is_dir():
        _fatal(
            f"Glambot's data folder is missing:\n{data_dir}\n\n"
            "Check it wasn't moved or deleted, or reinstall Glambot."
        )
    return data_dir


def _setup_logging(data_dir: Path) -> Path:
    """Route all output to a file - in a windowed PyInstaller build,
    sys.stdout/sys.stderr are None, which crashes anything that assumes they
    exist, and there's no console to read logs from even if it didn't."""
    log_path = data_dir / "glambot.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=log_file,
        force=True,
    )
    logging.info("Glambot starting (data dir: %s)", data_dir)
    return log_path


def main() -> None:
    data_dir = _resolve_data_dir()
    log_path = _setup_logging(data_dir)
    os.chdir(data_dir)

    from dotenv import load_dotenv
    load_dotenv()

    if not (data_dir / ".env").exists():
        _fatal(
            f".env not found in the data folder:\n{data_dir}\n\n"
            "Copy .env.example to .env and fill in your settings, then "
            "restart Glambot."
        )

    from glambot.app import create_app
    from glambot.db import JobStore
    from glambot.processor import stop_all_active
    from glambot.watcher import InboxWatcher

    inbox_dir = Path(os.environ.get("INBOX_DIR", str(data_dir / "project"))).resolve()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))

    store = JobStore(inbox_dir / ".glambot" / "jobs.sqlite")
    watcher = InboxWatcher(inbox_dir, store)
    watcher.start()
    app = create_app(inbox_dir, store, watcher)

    server_thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    url = f"http://{host}:{port}/"
    import urllib.request
    ready = False
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.5)
    if not ready:
        _fatal(
            f"Glambot's server never became ready at {url}.\n\n"
            f"Check the log for details:\n{log_path}"
        )

    _run_gui(url, watcher, log_path)


def _run_gui(url: str, watcher, log_path: Path) -> None:
    import webview
    import pystray
    from PIL import Image

    window = webview.create_window("Glambot", url, width=1400, height=900)

    _shutting_down = threading.Event()

    def shutdown() -> None:
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        logging.info("Glambot shutting down")
        try:
            from glambot.processor import stop_all_active
            stop_all_active()
        except Exception:
            logging.exception("Error stopping active renders")
        try:
            watcher.stop()
        except Exception:
            logging.exception("Error stopping watcher")
        try:
            tray_icon.stop()
        except Exception:
            pass
        os._exit(0)

    def on_closing():
        shutdown()

    window.events.closing += on_closing

    def _open_logs(icon=None, item=None):
        os.startfile(str(log_path))  # noqa: S606 - local file, not user input

    def _show_window(icon=None, item=None):
        try:
            window.show()
        except Exception:
            pass

    def _quit(icon=None, item=None):
        shutdown()

    icon_path = APP_DIR / "logo" / "glambotlogo.png"
    tray_image = Image.open(icon_path) if icon_path.exists() else Image.new("RGB", (16, 16), "black")
    tray_icon = pystray.Icon(
        "Glambot",
        tray_image,
        "Glambot",
        menu=pystray.Menu(
            pystray.MenuItem("Open Glambot", _show_window, default=True),
            pystray.MenuItem("View logs", _open_logs),
            pystray.MenuItem("Quit", _quit),
        ),
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()

    webview.start(gui="edgechromium")
    # webview.start() only returns if the window closes without going
    # through on_closing (shouldn't normally happen) - fall back to the same
    # clean shutdown rather than leaving the tray icon/watcher/server alive.
    shutdown()


if __name__ == "__main__":
    main()
