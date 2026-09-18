"""Regenerates mac_app/glambotlogo.icns from logo/glambotlogo.png.

Run by build_app.sh before PyInstaller so the .app/window/dock icon always
matches the current source PNG, without committing a second binary copy of
the logo to git. Mirrors windows_app/make_icon.py's role for the .ico.

macOS ships everything this needs (`sips`, `iconutil`) - no extra tool
install required. Must run on macOS; `sips`/`iconutil` don't exist elsewhere.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.platform != "darwin":
    raise SystemExit("mac_app/make_icon.py must run on macOS (uses sips/iconutil).")

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "logo" / "glambotlogo.png"
_DEST = _HERE / "glambotlogo.icns"

if not _SRC.exists():
    raise SystemExit(f"Source logo not found: {_SRC}")

# iconutil wants a .iconset folder with these exact filenames/sizes present.
_SIZES = [16, 32, 128, 256, 512]

with tempfile.TemporaryDirectory() as tmp:
    iconset = Path(tmp) / "glambotlogo.iconset"
    iconset.mkdir()
    for size in _SIZES:
        subprocess.run(
            ["sips", "-z", str(size), str(size), str(_SRC),
             "--out", str(iconset / f"icon_{size}x{size}.png")],
            check=True, capture_output=True,
        )
        # @2x retina variant = the next size up, per Apple's iconset spec.
        double = size * 2
        subprocess.run(
            ["sips", "-z", str(double), str(double), str(_SRC),
             "--out", str(iconset / f"icon_{size}x{size}@2x.png")],
            check=True, capture_output=True,
        )
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(_DEST)], check=True)

print(f"Wrote {_DEST}")
