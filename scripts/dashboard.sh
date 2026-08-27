#!/usr/bin/env bash
# Serve the local dashboard without remembering to activate the venv.
# The archiver itself keeps running as its own service; this is just the viewer.
# Open http://localhost:8080 while this is running. Ctrl-C to stop.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
    echo "[dashboard] creating virtual environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import discord, flask" 2>/dev/null; then
    echo "[dashboard] installing dependencies..."
    pip install -q -r requirements.txt
fi

exec python main.py dashboard "$@"
