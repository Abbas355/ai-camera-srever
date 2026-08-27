"""Project paths. Keep data/ and recordings/ at the repo root."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REC_DIR = ROOT / "recordings"
AUDIO_DIR = ROOT / "audio"
WORKER_SCRIPT = ROOT / "record_worker.py"
