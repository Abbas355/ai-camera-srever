#!/bin/bash
# Install Optiview backend into ~/optiview (WSL or Ubuntu without systemd).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${OPTIVIEW_HOME:-$HOME/optiview}"

echo "Optiview — WSL / local Linux setup"
echo "Source: $ROOT"
echo "Install: $TARGET"
echo

if [[ "$ROOT" == /mnt/* ]]; then
  echo "Copying project off /mnt (faster and more reliable in WSL)…"
  mkdir -p "$TARGET"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git/' \
      --exclude '__pycache__/' \
      --exclude '*.pyc' \
      --exclude 'recordings/' \
      --exclude 'data/*.db-wal' \
      --exclude 'data/*.db-shm' \
      --exclude 'data/record_worker.pid' \
      --exclude 'data/clip_cache/' \
      --exclude '.venv/' \
      --exclude 'dist/' \
      --exclude 'build/' \
      "$ROOT/" "$TARGET/"
  else
    find "$TARGET" -mindepth 1 -maxdepth 1 ! -name recordings ! -name .venv -exec rm -rf {} + 2>/dev/null || true
    cp -a "$ROOT/." "$TARGET/"
    rm -rf "$TARGET/.git" "$TARGET/.venv" "$TARGET/dist" "$TARGET/build"
  fi
else
  TARGET="$ROOT"
  echo "Installing in place."
fi

mkdir -p "$TARGET/data" "$TARGET/recordings"

echo "Installing Python, ffmpeg, and build tools (sudo)…"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg rsync

cd "$TARGET"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
# Desktop-only packages often fail on WSL; backend does not need them.
python -m pip install \
  "pycryptodome>=3.20.0" \
  "Pillow>=10.0.0" \
  "imageio-ffmpeg>=0.5.1" \
  "numpy>=2.0.0" \
  "av>=12.0.0" \
  "pyezviz>=0.2.2" \
  "requests>=2.31.0" \
  || python -m pip install -r requirements.txt

chmod +x linux/wsl-start.sh linux/wsl-stop.sh linux/wsl-setup.sh 2>/dev/null || true

echo
echo "Setup complete: $TARGET"
echo "Start the backend:"
echo "  bash $TARGET/linux/wsl-start.sh"
echo
echo "On Windows Optiview: Remote server → 127.0.0.1  (or the IP printed at start)"
echo "Login: admin / 1290"
