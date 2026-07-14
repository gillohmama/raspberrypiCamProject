#!/bin/bash
# Fresh-install setup for the wigglegram camera.
# Raspberry Pi OS BULLSEYE ONLY — the Arducam multi-camera adapter does not
# work on newer releases; do not upgrade the OS.
#
# Run as the normal user:  ./setup.sh

echo "== wigglegram camera setup (Bullseye) =="

sudo apt-get update

# One package per line on purpose: a single unknown package name aborts the
# whole apt-get install invocation, and names shift between OS releases.
for pkg in \
    python3-picamera2 \
    python3-pygame \
    python3-numpy \
    python3-pil \
    python3-smbus \
    python3-rpi.gpio \
    raspi-gpio \
    i2c-tools
do
    echo "-- installing $pkg"
    sudo apt-get install -y "$pkg" || echo "!! $pkg failed to install — check manually"
done

# The only pip package. NEVER pip-install numpy or picamera2 on this box —
# it breaks the apt libcamera stack. (Bullseye pip has no
# --break-system-packages flag and doesn't need one.)
sudo pip3 install smbus2

mkdir -p "$HOME/piCameraPics"

echo
echo "== /boot/config.txt checks (edit manually if any FAIL, then reboot) =="
for want in "camera_auto_detect=0" "dtoverlay=imx219" "dtparam=i2c_arm=on"; do
    if grep -q "^${want}" /boot/config.txt; then
        echo "OK    $want"
    else
        echo "FAIL  missing: $want"
    fi
done

echo
echo "Optional: install pisugar-server for cleaner button handling; without"
echo "it the app polls the PiSugar over I2C (or use SPACE on a keyboard)."
echo
echo "Done. Run with:"
echo "  sudo python3 wigglecam.py 3"
