#!/usr/bin/env bash
# =============================================================
#  Wigglegram Camera – Raspberry Pi 4 setup script
#  Run once after a fresh Raspberry Pi OS install.
#  Usage: bash setup.sh
# =============================================================

set -uo pipefail   # note: -e removed so one bad package won't abort everything

echo "=== Updating system packages ==="
sudo apt-get update -y
sudo apt-get upgrade -y

echo "=== Installing system dependencies ==="
sudo apt-get install -y \
    python3-pip \
    python3-dev \
    python3-pygame \
    python3-numpy \
    python3-pil \
    python3-smbus \
    python3-picamera2 \
    i2c-tools \
    libopenblas-dev \
    libopenjp2-7 \
    rpicam-apps

echo "=== Installing Python packages ==="
pip3 install --break-system-packages \
    smbus2 \
    Pillow \
    numpy \
    pygame \
    picamera2

echo ""
echo "=== /boot/firmware/config.txt changes needed ==="
echo ""
echo "  NOTE: On Raspberry Pi OS Bookworm the file is:"
echo "    /boot/firmware/config.txt"
echo "  On older Bullseye it is:"
echo "    /boot/config.txt"
echo ""
echo "  Open the file with:  sudo nano /boot/firmware/config.txt"
echo ""
echo "  Find the line:  camera_auto_detect=1"
echo "  Change it to:   camera_auto_detect=0"
echo ""
echo "  Then add these lines at the very bottom:"
echo ""
echo "    # Enable I2C"
echo "    dtparam=i2c_arm=on"
echo ""
echo "    # Disable camera auto-detect (required for Arducam adapter)"
echo "    camera_auto_detect=0"
echo ""
echo "    # Arducam Multi Camera Adapter V2.2 - IMX219 (Camera Module 2 / NoIR)"
echo "    dtoverlay=imx219"
echo ""
echo "  Save with Ctrl+X -> Y -> Enter, then reboot."
echo ""
echo "=== After reboot: verify I2C devices ==="
echo "Run: i2cdetect -y 1"
echo "You should see:"
echo "  0x57 = PiSugar 3 Plus"
echo "  0x70 = Arducam Multi Camera Adapter"
echo ""
echo "=== Select camera and verify camera is detected ==="
echo "Run these after reboot:"
echo "  sudo i2cset -y 1 0x70 0x01"
echo "  sudo modprobe -r imx219"
echo "  sudo modprobe imx219"
echo "  rpicam-still --list-cameras"
echo ""
echo "=== PiSugar 3 Plus daemon (optional but recommended) ==="
echo "Install pisugar-server for reliable button detection:"
echo "  curl http://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash"
echo "  sudo systemctl enable pisugar-server"
echo "  sudo systemctl start  pisugar-server"
echo ""
echo "=== Running the app ==="
echo "The app reloads the camera driver at startup so it MUST be run with sudo:"
echo "  sudo python3 ~/wigglegram.py"
echo ""
echo "=== Creating save directory ==="
mkdir -p ~/piCameraPics
echo "Save dir: ~/piCameraPics"
echo ""
echo "=== Done! ==="
