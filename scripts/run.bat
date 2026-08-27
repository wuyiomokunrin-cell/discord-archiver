@echo off
rem Launcher for Windows.
rem Sets up the venv, installs dependencies if missing, backfills only if there
rem is no archive yet, then live-listens. Safe to run on every boot.
cd /d "%~dp0.."

if not exist ".venv\Scripts\activate.bat" (
    echo [run] creating virtual environment...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

python -c "import discord, flask" 2>nul
if errorlevel 1 (
    echo [run] installing dependencies...
    pip install -q -r requirements.txt
)

if not exist "data\archive.sqlite3" (
    echo [run] no archive yet - running a one-time backfill first...
    python main.py backfill
)

python main.py listen
