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
    port = int(os.environ.get("PORT", "5000"))

    store = JobStore(inbox_dir / ".glambot" / "jobs.sqlite")

    watcher = InboxWatcher(inbox_dir, store)
    watcher.start()

    app = create_app(inbox_dir, store)
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()
