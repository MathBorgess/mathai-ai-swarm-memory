"""In-process ask threads bound to principal+workspace. Ephemeral; no database."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class ThreadBusy(RuntimeError):
    status = 429


@dataclass
class ThreadRecord:
    thread_id: str
    principal_id: str
    workspace_id: str
    user_turns: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryThreadStore:
    """Single-process ephemeral threads. Lost on restart. No SQLite.

    Bounds total rows and per-principal rows, sweeps expired entries on every
    mutation, and rejects concurrent updates to the same thread_id.
    """

    def __init__(
        self,
        max_turns: int = 8,
        ttl_seconds: int = 3600,
        max_threads: int = 64,
        max_threads_per_principal: int = 8,
    ):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.max_threads = max_threads
        self.max_threads_per_principal = max_threads_per_principal
        self._rows: dict[str, ThreadRecord] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def sweep(self, now: datetime) -> None:
        expired = [
            thread_id
            for thread_id, record in self._rows.items()
            if now - record.last_used > timedelta(seconds=self.ttl_seconds)
        ]
        for thread_id in expired:
            self._rows.pop(thread_id, None)
            self._busy.discard(thread_id)

    def create(self, *, principal_id: str, workspace_id: str, now: datetime) -> ThreadRecord:
        record = ThreadRecord(
            thread_id=secrets.token_urlsafe(16),
            principal_id=principal_id,
            workspace_id=workspace_id,
            created_at=now,
            last_used=now,
        )
        with self._lock:
            self.sweep(now)
            self._evict_for_create(principal_id)
            self._rows[record.thread_id] = record
        return record

    def get(self, thread_id: str, *, now: datetime) -> ThreadRecord | None:
        with self._lock:
            self.sweep(now)
            return self._rows.get(thread_id)

    def save(self, record: ThreadRecord) -> None:
        record.user_turns = record.user_turns[-self.max_turns :]
        with self._lock:
            self.sweep(record.last_used)
            if record.thread_id not in self._rows and len(self._rows) >= self.max_threads:
                self._evict_oldest()
            self._rows[record.thread_id] = record

    def acquire(self, thread_id: str) -> None:
        with self._lock:
            if thread_id in self._busy:
                raise ThreadBusy("Ask thread is busy")
            self._busy.add(thread_id)

    def release(self, thread_id: str) -> None:
        with self._lock:
            self._busy.discard(thread_id)

    def _evict_for_create(self, principal_id: str) -> None:
        owned = [item for item in self._rows.values() if item.principal_id == principal_id]
        while len(owned) >= self.max_threads_per_principal:
            oldest = min(owned, key=lambda item: item.last_used)
            self._rows.pop(oldest.thread_id, None)
            self._busy.discard(oldest.thread_id)
            owned = [item for item in self._rows.values() if item.principal_id == principal_id]
        while len(self._rows) >= self.max_threads:
            self._evict_oldest()

    def _evict_oldest(self) -> None:
        if not self._rows:
            return
        oldest = min(self._rows.values(), key=lambda item: item.last_used)
        self._rows.pop(oldest.thread_id, None)
        self._busy.discard(oldest.thread_id)
