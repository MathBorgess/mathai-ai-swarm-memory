"""Durable state with cross-process file locking (read-modify-write).

Morning idempotency lives here as an explicit phase machine rather than a claim file.
Writing "this run owns the day" *before* doing the work means the first crash wedges
the day forever: nothing ever writes the completion, and every later run reads the
stale claim and refuses. Instead:

- `day_run_lock` is a lease held for the whole run, which is what actually prevents a
  concurrent double launch of the planner.
- `MorningProgress` checkpoints each phase after its effect is durable, so a retry
  resumes instead of relaunching an uncertain provider.
- `completed` is only written once the HTML has been atomically replaced on disk.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swarm_reports.metrics.state import ReportsState, default_state_path, load_state, save_state

LOCK_SUFFIX = ".lock"
MORNING_DIR = "morning"

PHASE_START = "start"
PHASE_PLANNED = "planned"
PHASE_FROZEN = "frozen"
PHASE_COMPLETED = "completed"


class MorningRunBusy(RuntimeError):
    """Another morning run holds the day lease."""


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Replace `path` in one step; a half-written report must never be served."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


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


def morning_dir(state_dir: Path) -> Path:
    return state_dir / MORNING_DIR


def day_lock_path(state_dir: Path, day: str) -> Path:
    return morning_dir(state_dir) / f"{day}.run.lock"


def progress_path(state_dir: Path, day: str) -> Path:
    return morning_dir(state_dir) / f"{day}.progress.json"


@contextmanager
def day_run_lock(state_dir: Path, day: str, *, blocking: bool = True) -> Iterator[None]:
    """Exclusive lease for one calendar day's morning run."""
    path = day_lock_path(state_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            raise MorningRunBusy(f"another morning run holds {day}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class MorningProgress:
    day: str
    phase: str = PHASE_START
    run_id: str | None = None
    attempts: int = 0
    #: Raw planner output, cached before any validation so a retry never has to
    #: relaunch a provider whose side effects are unknown.
    provider_raw: dict[str, Any] | None = None
    plan_json: dict[str, Any] | None = None
    freeze_branch: str | None = None
    freeze_commit: str | None = None
    freeze_snapshot: str | None = None
    html_path: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    history: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "phase": self.phase,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "provider_raw": self.provider_raw,
            "plan_json": self.plan_json,
            "freeze_branch": self.freeze_branch,
            "freeze_commit": self.freeze_commit,
            "freeze_snapshot": self.freeze_snapshot,
            "html_path": self.html_path,
            "completed_at": self.completed_at,
            "last_error": self.last_error,
            "history": list(self.history),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MorningProgress:
        return cls(
            day=str(data.get("day") or ""),
            phase=str(data.get("phase") or PHASE_START),
            run_id=data.get("run_id"),
            attempts=int(data.get("attempts") or 0),
            provider_raw=data.get("provider_raw"),
            plan_json=data.get("plan_json"),
            freeze_branch=data.get("freeze_branch"),
            freeze_commit=data.get("freeze_commit"),
            freeze_snapshot=data.get("freeze_snapshot"),
            html_path=data.get("html_path"),
            completed_at=data.get("completed_at"),
            last_error=data.get("last_error"),
            history=[str(x) for x in (data.get("history") or [])],
        )

    def enter(self, phase: str) -> None:
        self.phase = phase
        stamp = datetime.now(timezone.utc).isoformat()
        self.history.append(f"{phase}@{stamp}")
        del self.history[:-40]


def load_progress(state_dir: Path, day: date | str) -> MorningProgress:
    key = day if isinstance(day, str) else day.isoformat()
    path = progress_path(state_dir, key)
    if not path.exists():
        return MorningProgress(day=key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt morning progress at {path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"corrupt morning progress at {path}")
    progress = MorningProgress.from_json(data)
    progress.day = key
    return progress


def save_progress(state_dir: Path, progress: MorningProgress) -> None:
    atomic_write_text(
        progress_path(state_dir, progress.day),
        json.dumps(progress.to_json(), indent=2, sort_keys=True) + "\n",
    )


@dataclass(frozen=True)
class MorningRunRecord:
    day: str
    run_id: str
    completed_at: str

    def to_json(self) -> dict[str, str]:
        return {"day": self.day, "run_id": self.run_id, "completed_at": self.completed_at}


def record_morning_complete(state_dir: Path, day: str, run_id: str) -> MorningRunRecord:
    record = MorningRunRecord(
        day=day,
        run_id=run_id,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    atomic_write_text(
        morning_dir(state_dir) / f"{day}.morning-meta.json",
        json.dumps(record.to_json(), indent=2) + "\n",
    )
    return record
