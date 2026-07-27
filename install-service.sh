#!/bin/bash
# Install (or refresh) the systemd unit that starts the camera at boot.
#
#   sudo ./install-service.sh
#
# Paths and username are detected from where this script lives and who ran
# it, so nothing has to be edited by hand.
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo:  sudo ./install-service.sh"
    exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-root}"
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
PICS="$HOME_DIR/piCameraPics"
UNIT=/etc/systemd/system/wigglecam.service

echo "repo:   $REPO"
echo "user:   $RUN_USER"
echo "photos: $PICS"
echo

if [ ! -f "$REPO/wigglecam.py" ]; then
    echo "!! wigglecam.py not found in $REPO — run this from the repo folder"
    exit 1
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Wigglegram camera
After=multi-user.target

[Service]
Type=simple
User=root
WorkingDirectory=$REPO
# Without this Python block-buffers stdout into the journal and the log
# looks empty for minutes at a time.
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $REPO/wigglecam.py --pics-dir $PICS

# Deliberately NO TTYPath/StandardInput=tty: claiming /dev/tty1 fights
# getty for the console and the resulting hangup kills us on sight. SDL
# opens the framebuffer by itself, exactly as it does over SSH.

# Appliance behaviour: come back from crashes, and from ESC.
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wigglecam
systemctl restart wigglecam
echo "installed — waiting a few seconds, then reporting status"
sleep 5
systemctl status wigglecam --no-pager -l || true
echo
echo "Live output:      journalctl -u wigglecam -f"
echo "Stop for testing: sudo systemctl stop wigglecam"
