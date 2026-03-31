#!/usr/bin/env bash
# =============================================================
#  Wigglegram Camera – Raspberry Pi 5 setup script
#  Run once after a fresh Raspberry Pi OS install.
#  Usage: bash setup.sh
# =============================================================

set -euo pipefail

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
    libatlas-base-dev \
    libopenjp2-7

echo "=== Installing Python packages ==="
pip3 install --break-system-packages \
    smbus2 \
    Pillow \
    numpy \
    pygame \
    picamera2

echo ""
echo "=== /boot/firmware/config.txt changes needed ==="
echo "Add these lines to /boot/firmware/config.txt if not already present:"
echo ""
echo "  # Enable I2C"
echo "  dtparam=i2c_arm=on"
echo ""
echo "  # Arducam Multi Camera Adapter (comment out whichever camera"
echo "  # type does NOT match your modules)"
echo "  #dtoverlay=imx219          # for IMX219 / Camera Module 2"
echo "  #dtoverlay=imx477          # for IMX477 / HQ Camera"
echo "  #dtoverlay=ov5647          # for OV5647 / Camera Module 1"
echo ""
echo "  # Disable camera auto-detect so we can control it manually"
echo "  camera_auto_detect=0"
echo ""
echo "Then reboot."
echo ""
echo "=== Checking I2C devices ==="
echo "Run 'i2cdetect -y 1' after reboot to confirm:"
echo "  0x70 = Arducam adapter"
echo "  0x57 = PiSugar 3 Plus"
echo ""
echo "=== PiSugar 3 Plus daemon (optional but recommended) ==="
echo "Install pisugar-server for reliable button detection:"
echo "  curl http://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash"
echo "  sudo systemctl enable pisugar-server"
echo "  sudo systemctl start  pisugar-server"
echo ""
echo "=== Creating save directory ==="
mkdir -p ~/piCameraPics
echo "Save dir: ~/piCameraPics"
echo ""
echo "=== Done! ==="
echo "Run the camera with: python3 wigglegram.py"
