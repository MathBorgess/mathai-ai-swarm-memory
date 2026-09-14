"""Durable state with cross-process file locking (read-modify-write)."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from swarm_reports.metrics.state import ReportsState, default_state_path, load_state, save_state

LOCK_SUFFIX = ".lock"
RUNNER_SUFFIX = ".morning-run"


@dataclass(frozen=True)
class MorningRunRecord:
    day: str
    run_id: str
    completed_at: str

    def to_json(self) -> dict[str, str]:
        return {"day": self.day, "run_id": self.run_id, "completed_at": self.completed_at}


class StateTransaction:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_path = default_state_path(state_dir)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + LOCK_SUFFIX)

    @contextmanager
    def locked(self) -> Iterator[ReportsState]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                state = load_state(self.state_path)
                yield state
                save_state(self.state_path, state)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def runner_claim_path(state_dir: Path, day: str) -> Path:
    return state_dir / f"{day}{RUNNER_SUFFIX}"


@contextmanager
def morning_run_claim(state_dir: Path, day: str, run_id: str) -> Iterator[bool]:
    """Return True if this run_id owns the claim; False if another completed run exists."""
    path = runner_claim_path(state_dir, day)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + LOCK_SUFFIX)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing and existing != run_id:
                    yield False
                    return
            path.write_text(run_id + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            yield True
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def record_morning_complete(state_dir: Path, day: str, run_id: str) -> MorningRunRecord:
    record = MorningRunRecord(
        day=day,
        run_id=run_id,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    meta_path = state_dir / f"{day}.morning-meta.json"
    import json

    meta_path.write_text(json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8")
    os.chmod(meta_path, 0o600)
    return record
