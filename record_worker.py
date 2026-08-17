"""Detached auto-record process. Survives closing V380 Studio."""

from __future__ import annotations

from auto_record import run_worker_forever

if __name__ == "__main__":
    run_worker_forever()
