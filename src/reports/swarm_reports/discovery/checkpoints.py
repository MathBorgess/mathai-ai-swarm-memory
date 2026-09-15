"""Per-source discovery checkpoints: `state_dir/discovery-state.json`, outside Git.

Restart-safe: each source keeps a `cursor` (high-water mark, only advanced on a
non-truncated pass) plus a bounded `seen_ids` ring buffer for dedup across
retries and same-timestamp ties. A source failure leaves its bucket untouched.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

STATE_VERSION = 1
MAX_SEEN_IDS = 500


@dataclass
class SourceCheckpoint:
    cursor: str | None = None
    seen_ids: list[str] = field(default_factory=list)
    paging: dict = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"cursor": self.cursor, "seen_ids": self.seen_ids, "paging": self.paging}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SourceCheckpoint:
        seen = data.get("seen_ids") or []
        return cls(
            cursor=data.get("cursor"),
            paging=dict(data.get("paging") or {}),
            seen_ids=[str(item) for item in seen if isinstance(item, str)],
        )

    def with_update(self, *, cursor: str | None, new_ids: list[str], paging: dict | None = None) -> SourceCheckpoint:
        merged = self.seen_ids + [i for i in new_ids if i not in self.seen_ids]
        bounded = merged[-MAX_SEEN_IDS:]
        return SourceCheckpoint(cursor=cursor, seen_ids=bounded, paging=paging or {})


@dataclass
class CheckpointState:
    items: dict = field(default_factory=dict)
    version: int = STATE_VERSION
    sources: dict[str, SourceCheckpoint] = field(default_factory=dict)

    def get(self, source: str) -> SourceCheckpoint:
        return self.sources.setdefault(source, SourceCheckpoint())

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "items": self.items,
            "sources": {name: cp.to_json() for name, cp in self.sources.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CheckpointState:
        sources_raw = data.get("sources") or {}
        sources = {
            str(name): SourceCheckpoint.from_json(value)
            for name, value in sources_raw.items()
            if isinstance(value, dict)
        }
        return cls(version=int(data.get("version", STATE_VERSION)), sources=sources, items=dict(data.get("items") or {}))


def default_checkpoint_path(state_dir: Path) -> Path:
    return state_dir / "discovery-state.json"


def load_checkpoints(path: Path) -> CheckpointState:
    if not path.exists():
        return CheckpointState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return CheckpointState.from_json(data)


def save_checkpoints(path: Path, state: CheckpointState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".discovery-state-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    os.chmod(path, 0o600)


@contextlib.contextmanager
def checkpoint_lock(state_dir: Path) -> Iterator[None]:
    """Exclusive file lock across processes for read-modify-write of the state file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".discovery-state.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
