"""Build optiview-linux.tar.gz to copy to Ubuntu (run on Windows or Linux)."""

from __future__ import annotations

import tarfile
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SKIP_DIR = {"__pycache__", "recordings", "dist", "build", ".git", ".venv"}
SKIP_FILE = {".pyc", ".db", ".db-wal", ".db-shm", ".pid"}


def keep(path: Path) -> bool:
    rel = path.relative_to(APP)
    if any(p in SKIP_DIR for p in rel.parts):
        return False
    if path.suffix in SKIP_FILE:
        return False
    if path.name in {"master.key", "record_worker.json", "record_worker.log"}:
        return False
    return True


def main() -> None:
    name = f"optiview-linux-{datetime.now().strftime('%Y%m%d')}.tar.gz"
    out = APP / name
    with tarfile.open(out, "w:gz") as tar:
        for path in APP.rglob("*"):
            if not path.is_file() or not keep(path):
                continue
            tar.add(path, arcname=Path("optiview") / path.relative_to(APP))
    print(f"Created {out}")
    print("Copy this file to Ubuntu, then:")
    print("  tar -xzf", name)
    print("  cd optiview")
    print("  bash linux/install.sh")
    print("WSL test (no tar needed):")
    print("  wsl")
    print("  bash linux/wsl-setup.sh")
    print("  bash ~/optiview/linux/wsl-start.sh")


if __name__ == "__main__":
    main()
