"""Entrypoint: starts the inbox watcher and the review/approval web app.

Usage:
    python -m glambot.pipeline
"""
from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from .app import create_app
from .db import JobStore
from .ftp_import import FtpImportServer, load_ftp_settings
from .watcher import InboxWatcher


def _handle_sigterm(signum, frame) -> None:
    """Translate a Quit signal (Cmd+Q / Dock -> Quit when run as a packaged
    Mac app) into a normal exit, so it unwinds through main()'s
    try/finally and stops the watcher cleanly instead of a hard kill."""
    raise SystemExit(0)


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_sigterm)

    inbox_dir = Path(os.environ.get("INBOX_DIR", "inbox")).resolve()
    host = os.environ.get("HOST", "127.0.0.1")
    # BIND_HOST is what the socket actually listens on; HOST stays the address
    # shown to the user / used to build links. Set BIND_HOST=0.0.0.0 to serve
    # guests and iPads over the LAN.
    bind_host = os.environ.get("BIND_HOST", host)
    port = int(os.environ.get("PORT", "5000"))

    _warn_if_exposed_without_pin(bind_host)

    store = JobStore(inbox_dir / ".glambot" / "jobs.sqlite")

    watcher = InboxWatcher(inbox_dir, store)
    watcher.start()

    ftp_server = FtpImportServer(inbox_dir, watcher)
    if load_ftp_settings(inbox_dir).get("enabled"):
        err = ftp_server.start()
        if err:
            logging.getLogger(__name__).warning("FTP import server not started: %s", err)

    app = create_app(inbox_dir, store, watcher, ftp_server)
    try:
        app.run(host=bind_host, port=port, debug=False, use_reloader=False)
    finally:
        watcher.stop()
        ftp_server.stop()


def _warn_if_exposed_without_pin(bind_host: str) -> None:
    loopback = bind_host in {"127.0.0.1", "::1", "localhost", ""}
    if not loopback and not os.environ.get("GLAMBOT_PIN", "").strip():
        logging.getLogger(__name__).warning(
            "Glambot is binding to %s (reachable on the LAN) with no GLAMBOT_PIN set - "
            "anyone on the network can open the operator dashboard. Set GLAMBOT_PIN in .env.",
            bind_host,
        )


if __name__ == "__main__":
    main()
