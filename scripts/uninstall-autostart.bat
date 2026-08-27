@echo off
schtasks /delete /tn "DiscordArchiver" /f
echo Autostart removed. Run scripts\run.bat to start manually.
