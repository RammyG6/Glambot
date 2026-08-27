"""Regenerates windows_app/glambotlogo.ico from logo/glambotlogo.png.

Run by build_installer.bat before PyInstaller so the .exe/window/taskbar
icon always matches the current source PNG, without committing a second
binary copy of the logo to git.
"""
from pathlib import Path

from PIL import Image

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "logo" / "glambotlogo.png"
_DEST = _HERE / "glambotlogo.ico"

if not _SRC.exists():
    raise SystemExit(f"Source logo not found: {_SRC}")

img = Image.open(_SRC).convert("RGBA")
img.save(_DEST, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print(f"Wrote {_DEST}")
