#!/usr/bin/env bash
# Start the archiver automatically on Linux, without opening a terminal.
# Installs a per-user systemd service that begins at boot (linger) and
# restarts on failure. Run once; it survives reboots and logins.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Self-heal: earlier releases shipped these without the executable bit, which
# makes systemd fail with 203/EXEC. Ensure they are runnable.
chmod +x "$DIR/scripts/run.sh" "$DIR/scripts/install-autostart.sh" \
         "$DIR/scripts/uninstall-autostart.sh" 2>/dev/null || true

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - cannot install autostart." >&2
    echo "You can still run scripts/run.sh by hand." >&2
    exit 1
fi

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/discord-archiver.service <<EOF
[Unit]
Description=Discord archiver (live capture)
After=network-online.target

[Service]
WorkingDirectory=$DIR
# Launch through bash so the service never depends on the exec bit surviving
# checkout/editor quirks (a lost +x bit shows up as systemd 203/EXEC).
ExecStart=/bin/bash $DIR/scripts/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable discord-archiver.service

# linger starts it at boot even before you log in
loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user start discord-archiver.service || \
    echo "note: could not start now (maybe no session); it will start at boot."

echo "Installed. Control with:"
echo "  systemctl --user status discord-archiver"
echo "  systemctl --user stop discord-archiver"
echo "  journalctl --user -u discord-archiver -f"
