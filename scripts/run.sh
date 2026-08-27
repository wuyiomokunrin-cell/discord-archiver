#!/usr/bin/env bash
# Launcher for Linux / macOS.
# Sets up the venv, installs dependencies if missing, backfills only if there
# is no archive yet, then live-listens. Safe to run on every boot: the run
# lock refuses a second instance, and backfill is skipped once data exists.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
    echo "[run] creating virtual environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import discord, flask" 2>/dev/null; then
    echo "[run] installing dependencies..."
    pip install -q -r requirements.txt
fi

if [ ! -f data/archive.sqlite3 ]; then
    echo "[run] no archive yet - running a one-time backfill first..."
    python main.py backfill || true
fi

exec python main.py listen
