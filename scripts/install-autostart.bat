@echo off
rem Start the archiver automatically on Windows at logon, no terminal typing.
rem Creates a Task Scheduler task that runs the launcher when you sign in.
schtasks /create /tn "DiscordArchiver" /tr "\"%~dp0run.bat\"" /sc onlogon /f
if errorlevel 1 (
    echo Failed to create the task. Try running this file as Administrator.
    exit /b 1
)
echo Installed. The archiver will start each time you sign in.
echo To remove it, run scripts\uninstall-autostart.bat
