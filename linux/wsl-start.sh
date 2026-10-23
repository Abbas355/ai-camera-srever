#!/bin/bash
# Start Optiview API + recorder in WSL / Linux (no systemd required).
set -euo pipefail
if [[ -n "${OPTIVIEW_HOME:-}" ]]; then
  ROOT="$OPTIVIEW_HOME"
elif [[ -x "$(dirname "$0")/../.venv/bin/python" ]]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
elif [[ -x "$HOME/optiview/.venv/bin/python" ]]; then
  ROOT="$HOME/optiview"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "No virtualenv at $ROOT/.venv — run: bash linux/wsl-setup.sh"
  exit 1
fi
mkdir -p "$ROOT/data" "$ROOT/recordings"
bash "$ROOT/linux/wsl-stop.sh" >/dev/null 2>&1 || true

nohup "$PY" "$ROOT/clip_server.py" >>"$ROOT/data/server.log" 2>&1 &
echo $! > "$ROOT/data/clip_server.pid"
nohup "$PY" "$ROOT/record_worker.py" >>"$ROOT/data/record_worker.log" 2>&1 &
echo $! > "$ROOT/data/record_worker.pid"

sleep 1
IPS="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Optiview backend is running"
echo "  API:      http://127.0.0.1:8080"
if [[ -n "$IPS" ]]; then
  echo "  LAN IP:   $IPS"
fi
echo "  Logs:     $ROOT/data/server.log"
echo
echo "Windows app → Remote server → 127.0.0.1 → Connect"
echo "Login: admin / 1290"
echo "Stop:  bash $ROOT/linux/wsl-stop.sh"
