#!/bin/bash
# Ubuntu server install (systemd). For WSL use linux/wsl-setup.sh instead.
set -euo pipefail
cd "$(dirname "$0")/.."
PREFIX=/opt/optiview
echo "Installing Optiview to $PREFIX"
sudo mkdir -p "$PREFIX"
sudo cp -a . "$PREFIX"
sudo mkdir -p "$PREFIX/data" "$PREFIX/recordings"
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg
sudo python3 -m venv "$PREFIX/.venv"
sudo "$PREFIX/.venv/bin/pip" install -U pip
sudo "$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt" || \
  sudo "$PREFIX/.venv/bin/pip" install \
    pycryptodome Pillow imageio-ffmpeg numpy av pyezviz requests
# Point units at this install path (venv python + working dir)
for unit in optiview-record.service optiview-api.service; do
  sudo sed -i "s#WorkingDirectory=.*#WorkingDirectory=$PREFIX#" "$PREFIX/linux/$unit"
  sudo sed -i "s#/opt/optiview#$PREFIX#g" "$PREFIX/linux/$unit"
  sudo cp "$PREFIX/linux/$unit" /etc/systemd/system/
done
# Avoid double-start from old unit names
sudo systemctl disable --now v380-record v380-clips 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now optiview-record
sudo systemctl enable --now optiview-api
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 8080/tcp || true
fi
echo
echo "Optiview is on"
echo "API port: 8080"
hostname -I
echo "On Windows: Remote server → that IP → login admin / 1290"
