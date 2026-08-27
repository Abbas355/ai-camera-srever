#!/bin/bash
set -e
cd "$(dirname "$0")/.."
PREFIX=/opt/v380-studio
echo "Installing V380 Rec to $PREFIX"
sudo mkdir -p "$PREFIX"
sudo cp -a . "$PREFIX"
sudo apt-get update
sudo apt-get install -y python3 python3-pip ffmpeg
sudo python3 -m pip install -r "$PREFIX/requirements.txt"
sudo sed -i "s#WorkingDirectory=.*#WorkingDirectory=$PREFIX#" "$PREFIX/linux/v380-record.service"
sudo sed -i "s#WorkingDirectory=.*#WorkingDirectory=$PREFIX#" "$PREFIX/linux/v380-clips.service"
sudo sed -i "s#/opt/v380-studio#$PREFIX#g" "$PREFIX/linux/v380-record.service"
sudo sed -i "s#/opt/v380-studio#$PREFIX#g" "$PREFIX/linux/v380-clips.service"
sudo cp "$PREFIX/linux/v380-record.service" /etc/systemd/system/
sudo cp "$PREFIX/linux/v380-clips.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now v380-record
sudo systemctl enable --now v380-clips
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 8080/tcp || true
fi
echo
echo "Record service: on"
echo "Recordings list port: 8080"
hostname -I
echo "On Windows Playback, enter one of those IPs."
