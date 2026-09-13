"""In-process ask threads bound to principal+workspace. Durable SQLite is out of scope."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class ThreadRecord:
    thread_id: str
    principal_id: str
    workspace_id: str
    user_turns: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryThreadStore:
    def __init__(self, max_turns: int = 8, ttl_seconds: int = 3600):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._rows: dict[str, ThreadRecord] = {}
        self._lock = threading.Lock()

    def create(self, *, principal_id: str, workspace_id: str, now: datetime) -> ThreadRecord:
        record = ThreadRecord(
            thread_id=secrets.token_urlsafe(16),
            principal_id=principal_id,
            workspace_id=workspace_id,
            created_at=now,
            last_used=now,
        )
        with self._lock:
            self._rows[record.thread_id] = record
        return record

    def get(self, thread_id: str, *, now: datetime) -> ThreadRecord | None:
        with self._lock:
            record = self._rows.get(thread_id)
            if record is None:
                return None
            if now - record.last_used > timedelta(seconds=self.ttl_seconds):
                self._rows.pop(thread_id, None)
                return None
            return record

    def save(self, record: ThreadRecord) -> None:
        record.user_turns = record.user_turns[-self.max_turns :]
        with self._lock:
            self._rows[record.thread_id] = record
