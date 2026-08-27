@echo off
rem Serve the local dashboard on Windows without remembering the venv.
rem The archiver keeps running via its own task; this is just the viewer.
rem Open http://localhost:8080 while this is running. Close the window to stop.
cd /d "%~dp0.."

if not exist ".venv\Scripts\activate.bat" (
    echo [dashboard] creating virtual environment...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

python -c "import discord, flask" 2>nul
if errorlevel 1 (
    echo [dashboard] installing dependencies...
    pip install -q -r requirements.txt
)

python main.py dashboard %*
