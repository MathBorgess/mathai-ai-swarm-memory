"""Immutable, numbered evening revisions.

Two rules decide whether the night has to run again:

- **Identical resend is not a revision.** The comparison is over canonical *content*
  JSON, with transport fields such as `client_saved_at` excluded, so tapping Enviar twice
  costs nothing.
- **Comparison is against the latest revision only, never the whole history.** Editing A
  to B and back to A is a real change of mind about the day, and the night has to see it.
  Hashing against every past revision would swallow the third submission.

Durability: within one day lock the writer lays down the revision file, then the outbox
job, then the index. Every crash point converges. Lose the process after the revision
file and the index still points at the old latest, so a retry redoes the same revision
number with the same content — same file names, one job. Lose it after the outbox job and
the same thing happens, and the job file is overwritten rather than duplicated. The index
is what makes a revision visible, and it is written last.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swarm_reports.evening_schema import EveningPayload, canonical_json, parse_evening_payload

EVENING_DIR = "evening"
INDEX_NAME = "index.json"
REVISIONS_DIR = "revisions"
LOCK_NAME = ".lock"

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def content_hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Revision:
    revision: int
    day: str
    owner_id: str
    content_hash: str
    content: dict[str, Any]
    received_at: str
    transport: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "day": self.day,
            "owner_id": self.owner_id,
            "content_hash": self.content_hash,
            "content": self.content,
            "received_at": self.received_at,
            "transport": self.transport,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Revision:
        return cls(
            revision=int(data["revision"]),
            day=str(data["day"]),
            owner_id=str(data["owner_id"]),
            content_hash=str(data["content_hash"]),
            content=dict(data["content"]),
            received_at=str(data.get("received_at") or ""),
            transport=dict(data.get("transport") or {}),
        )

    def payload(self) -> EveningPayload:
        return parse_evening_payload(self.content)


@dataclass(frozen=True)
class SaveOutcome:
    revision: int
    changed: bool
    content_hash: str


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


class RevisionStore:
    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / EVENING_DIR

    def day_dir(self, day: date | str) -> Path:
        key = day if isinstance(day, str) else day.isoformat()
        return self.root / key

    @contextmanager
    def day_lock(self, day: date | str) -> Iterator[None]:
        """Serialize revisions for one day across both threads and processes."""
        directory = self.day_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / LOCK_NAME
        with _thread_lock(str(lock_path)):
            with open(lock_path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _index_path(self, day: date | str) -> Path:
        return self.day_dir(day) / INDEX_NAME

    def _revision_path(self, day: date | str, revision: int) -> Path:
        return self.day_dir(day) / REVISIONS_DIR / f"{revision:04d}.json"

    def read_index(self, day: date | str) -> dict[str, Any]:
        path = self._index_path(day)
        if not path.exists():
            return {"latest": 0, "revisions": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"corrupt revision index at {path}")
        return data

    def latest(self, day: date | str) -> Revision | None:
        index = self.read_index(day)
        latest = int(index.get("latest") or 0)
        if latest <= 0:
            return None
        return self.read_revision(day, latest)

    def read_revision(self, day: date | str, revision: int) -> Revision:
        path = self._revision_path(day, revision)
        return Revision.from_json(json.loads(path.read_text(encoding="utf-8")))

    def history(self, day: date | str) -> list[Revision]:
        index = self.read_index(day)
        return [
            self.read_revision(day, int(entry["revision"]))
            for entry in index.get("revisions") or []
        ]

    def save(self, payload: EveningPayload, *, now: datetime | None = None) -> SaveOutcome:
        """Append a revision if the content changed. Caller must hold `day_lock`."""
        day_key = payload.day.isoformat()
        content = payload.content_json()
        digest = content_hash(content)
        index = self.read_index(day_key)
        latest_number = int(index.get("latest") or 0)

        if latest_number > 0:
            current = self.read_revision(day_key, latest_number)
            if current.content_hash == digest:
                return SaveOutcome(revision=latest_number, changed=False, content_hash=digest)

        revision = latest_number + 1
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        record = Revision(
            revision=revision,
            day=day_key,
            owner_id=payload.owner_id,
            content_hash=digest,
            content=content,
            received_at=stamp,
            transport={"client_saved_at": payload.client_saved_at},
        )
        _atomic_write_json(self._revision_path(day_key, revision), record.to_json())
        return SaveOutcome(revision=revision, changed=True, content_hash=digest)

    def commit_index(self, day: date | str, revision: int, digest: str, stamp: str) -> None:
        """Make a revision visible. Written last, on purpose."""
        day_key = day if isinstance(day, str) else day.isoformat()
        index = self.read_index(day_key)
        entries = [
            entry for entry in (index.get("revisions") or []) if int(entry["revision"]) != revision
        ]
        entries.append({"revision": revision, "content_hash": digest, "received_at": stamp})
        entries.sort(key=lambda entry: int(entry["revision"]))
        _atomic_write_json(
            self._index_path(day_key),
            {"latest": max(int(e["revision"]) for e in entries), "revisions": entries},
        )
