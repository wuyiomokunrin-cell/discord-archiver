#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now discord-archiver.service 2>/dev/null || true
rm -f ~/.config/systemd/user/discord-archiver.service
systemctl --user daemon-reload 2>/dev/null || true
echo "Autostart removed. Run scripts/run.sh to start manually."
