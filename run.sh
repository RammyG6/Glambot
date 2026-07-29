#!/usr/bin/env bash
# Starts the Glambot footage pipeline: inbox watcher + local review/approve app.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Note: system ffmpeg not found on PATH — Glambot will automatically fall back to the bundled ffmpeg from the imageio-ffmpeg package."
fi

if [ ! -f .env ]; then
  echo ".env not found — copy .env.example to .env and fill in your credentials first." >&2
  exit 1
fi

python -m glambot.pipeline
