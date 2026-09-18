"""macOS entry point for the packaged app (built by mac_app/glambot.spec).

Thin per-OS wrapper - all the actual launcher logic (native window, tray
icon, data-dir resolution, server startup/shutdown) lives in the shared
`launcher_core` module at the repo root, so Windows and macOS builds run
identical app behavior. See launcher_core.py's docstring for details.

Not used by mac_app/launch.sh (the lightweight Platypus/venv build) - that
keeps running the plain CLI entry point (glambot/pipeline.py) unchanged.
This launcher is only for the PyInstaller-packaged, no-terminal .app tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Dev-mode convenience: `python mac_app/glambot_launcher.py` from a
    # checkout needs the repo root on sys.path to find `launcher_core` (a
    # frozen build gets this from PyInstaller's Analysis instead).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher_core import main

if __name__ == "__main__":
    main()
