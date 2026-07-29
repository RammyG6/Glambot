#!/usr/bin/env bash
# Template launcher wrapped into Glambot.app by build_app.sh (see that file
# for the Platypus packaging step). build_app.sh substitutes __REPO_DIR__
# with the real absolute path at build time, so this file is portable across
# any Mac the repo is cloned to — do not hardcode a path here directly.
# Starts the pipeline in the foreground (so Quit can signal it) and opens
# the review page in the default browser once the server responds.
set -euo pipefail

REPO_DIR="__REPO_DIR__"
cd "$REPO_DIR"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  osascript -e 'display alert "Glambot needs setup" message ".env not found. In Terminal, run: cp .env.example .env — then fill in your settings before launching Glambot again." buttons {"OK"} default button 1' >/dev/null 2>&1 || true
  open "$REPO_DIR"
  exit 1
fi

# ffmpeg discovery/fallback (system PATH, else the bundled imageio-ffmpeg
# binary) is handled directly in Python now (glambot/processor.py) — no
# shell-level PATH/symlink setup needed here.

HOST="$(grep -E '^HOST=' .env 2>/dev/null | tail -1 | cut -d= -f2-)" || HOST=""
PORT="$(grep -E '^PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2-)" || PORT=""
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"

(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "http://$HOST:$PORT/"; then
      open "http://$HOST:$PORT/"
      break
    fi
    sleep 0.5
  done
) &

exec python -m glambot.pipeline
