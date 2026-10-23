#!/bin/bash
# Stop Optiview backend processes started by wsl-start.sh.
set -euo pipefail
if [[ -n "${OPTIVIEW_HOME:-}" ]]; then
  ROOT="$OPTIVIEW_HOME"
elif [[ -d "$(dirname "$0")/../data" ]]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
elif [[ -d "$HOME/optiview/data" ]]; then
  ROOT="$HOME/optiview"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

stop_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 0.4
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_pid "$ROOT/data/clip_server.pid"
stop_pid "$ROOT/data/record_worker.pid"
pkill -f "$ROOT/clip_server.py" 2>/dev/null || true
pkill -f "$ROOT/record_worker.py" 2>/dev/null || true
echo "Optiview backend stopped."
