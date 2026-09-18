"""Tiny stand-alone native file/folder dialog, run as a subprocess by
`glambot.nativeui` when Glambot is served from a plain browser (the dev CLI)
rather than the packaged pywebview window.

    python -m glambot._pickdialog folder [initial_dir]
    python -m glambot._pickdialog footage [initial_dir]

Prints one selected path per line to stdout; prints nothing if cancelled.
Kept in its own module (never imported, only spawned) so PyInstaller doesn't
drag tkinter into the frozen build, which uses the pywebview dialog instead.
"""
from __future__ import annotations

import sys

from .processor import VIDEO_EXTENSIONS


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else "folder"
    initial = argv[2] if len(argv) > 2 else ""

    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    if kind == "footage":
        patterns = " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))
        selection = filedialog.askopenfilenames(
            title="Choose footage to import",
            initialdir=initial or None,
            filetypes=[("Footage", patterns), ("All files", "*.*")],
        )
        paths = list(selection)
    else:
        chosen = filedialog.askdirectory(title="Choose a folder", initialdir=initial or None)
        paths = [chosen] if chosen else []

    root.destroy()
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
