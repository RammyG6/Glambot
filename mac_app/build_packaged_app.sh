#!/usr/bin/env bash
# Builds mac_app/dist/Glambot.app: a real, no-Terminal, native-window
# packaged app (PyInstaller bundles Python + deps + ffmpeg/ffprobe into a
# standalone .app), the Mac counterpart to windows_app/build_installer.bat.
#
# This is a DIFFERENT, heavier tier than mac_app/build_app.sh (which wraps
# the plain CLI pipeline with Platypus - no native window, no tray icon,
# needs a live Python/venv). Use build_app.sh for "just run it from source
# with a double-click"; use this for "hand a no-terminal .app to a new
# operator", matching Windows's Glambot.bat vs. windows_app split.
#
# One-time setup on THIS build machine only:
#   - a .venv already created by run.sh (or: python3 -m venv .venv)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  echo "Run ./run.sh once first to create the virtualenv." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$REPO_DIR/.venv/bin/activate"

echo "Installing build-time dependencies..."
pip install -q --disable-pip-version-check -r "$REPO_DIR/mac_app/packaged-requirements.txt" pyinstaller

if [ ! -f "$REPO_DIR/mac_app/vendor/ffprobe" ]; then
  echo "Looking for an installed ffprobe to vendor..."
  FFPROBE_SRC="$(command -v ffprobe || true)"
  if [ -z "$FFPROBE_SRC" ]; then
    echo
    echo "Could not find ffprobe on PATH."
    echo "Install it first: brew install ffmpeg"
    echo "(then reopen this terminal), or manually copy a static ffprobe"
    echo "binary to mac_app/vendor/ffprobe."
    exit 1
  fi
  mkdir -p "$REPO_DIR/mac_app/vendor"
  cp "$FFPROBE_SRC" "$REPO_DIR/mac_app/vendor/ffprobe"
  chmod +x "$REPO_DIR/mac_app/vendor/ffprobe"
  echo "Vendored ffprobe from $FFPROBE_SRC"
fi

echo "Regenerating the .app/window icon from logo/glambotlogo.png..."
python "$REPO_DIR/mac_app/make_icon.py"

echo "Running PyInstaller..."
pyinstaller "$REPO_DIR/mac_app/glambot.spec" \
  --distpath "$REPO_DIR/mac_app/dist" \
  --workpath "$REPO_DIR/mac_app/build" \
  --noconfirm

echo
echo "Done: mac_app/dist/Glambot.app"
echo "Unsigned build: first launch needs right-click -> Open (Gatekeeper will"
echo "warn once). To distribute without that warning, sign with an Apple"
echo "Developer ID certificate and notarize with 'xcrun notarytool' before"
echo "handing this .app to anyone else - see the plan this build came from."
