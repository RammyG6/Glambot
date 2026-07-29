#!/usr/bin/env bash
# Builds Glambot.app, a double-click launcher for the pipeline, using
# Platypus (a free, open-source tool for wrapping a script into a proper
# macOS .app bundle with correct Dock/Quit process-lifecycle handling).
#
# One-time setup if you don't have Platypus yet:
#   brew install --cask platypus
#   /bin/sh "/Applications/Platypus.app/Contents/Resources/InstallCommandLineTool.sh" \
#     "/Applications/Platypus.app/Contents/Resources"
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Applications/Glambot.app"

if ! command -v platypus >/dev/null 2>&1; then
  echo "Platypus command-line tool not found." >&2
  echo "Install it with:" >&2
  echo "  brew install --cask platypus" >&2
  echo '  /bin/sh "/Applications/Platypus.app/Contents/Resources/InstallCommandLineTool.sh" "/Applications/Platypus.app/Contents/Resources"' >&2
  exit 1
fi

mkdir -p "$HOME/Applications"

# launch.sh is a portable template (REPO_DIR is a placeholder) so the repo
# can be cloned to any Mac and still build a correctly-pathed app — bake in
# this machine's actual path into a throwaway copy before handing it to
# Platypus, which embeds a static copy of whatever script it's given.
TMP_SCRIPT="$(mktemp -t glambot_launch).sh"
sed "s|__REPO_DIR__|$REPO_DIR|g" "$REPO_DIR/mac_app/launch.sh" > "$TMP_SCRIPT"
chmod +x "$TMP_SCRIPT"

platypus \
  -a 'Glambot' \
  -o 'None' \
  -p '/bin/bash' \
  -u 'Glambot' \
  -I 'com.g6moco.glambot' \
  -V '1.0' \
  -y \
  "$TMP_SCRIPT" \
  "$DEST"

rm -f "$TMP_SCRIPT"

echo "Built $DEST"
echo "First launch: right-click Glambot.app -> Open (it's unsigned, so Gatekeeper will warn once)."
