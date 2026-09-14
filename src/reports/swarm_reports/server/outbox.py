"""Durable outbox for evening jobs.

The HTTP handler must never be the thing that runs the night session. It writes a job to
disk inside the same lock that created the revision and answers 200 immediately, so the
owner's phone is told "saved" even if the worker is broken, restarting, or absent because
F4 does not exist yet.

Claiming is a rename, which is atomic on POSIX: exactly one claimer moves
`pending/<day>-<rev>.json` into `claimed/`, and everyone else gets `FileNotFoundError`.
Completion is a marker keyed by `(day, revision)`, so a retry after a dispatch failure
cannot repeat an effect that already landed.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

OUTBOX_DIR = "outbox"
PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class OutboxJob:
    day: str
    revision: int
    content_hash: str
    enqueued_at: str
    attempts: int = 0
    last_error: str | None = None

    @property
    def name(self) -> str:
        return f"{self.day}-{self.revision:04d}.json"

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "enqueued_at": self.enqueued_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OutboxJob:
        return cls(
            day=str(data["day"]),
            revision=int(data["revision"]),
            content_hash=str(data["content_hash"]),
            enqueued_at=str(data.get("enqueued_at") or ""),
            attempts=int(data.get("attempts") or 0),
            last_error=data.get("last_error"),
        )


class Dispatcher(Protocol):
    def __call__(self, job: OutboxJob) -> None: ...


class LeavePendingDispatcher:
    """No night session wired yet (F4). The job stays pending and visible."""

    def __call__(self, job: OutboxJob) -> None:
        raise PendingDispatch(
            f"no evening handler configured; {job.day} revision {job.revision} left pending"
        )


class PendingDispatch(RuntimeError):
    """Not a failure to retry-forever: the work is simply not wired yet."""


class BoundedCommandDispatcher:
    """Run a configured argv with no shell, one job at a time, with a hard timeout."""

    def __init__(self, command: tuple[str, ...], *, timeout_seconds: int = 900) -> None:
        if not command:
            raise ValueError("dispatch command must be a non-empty argv list")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    def __call__(self, job: OutboxJob) -> None:
        proc = subprocess.run(
            list(self.command),
            input=json.dumps(job.to_json()),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"evening handler exited {proc.returncode}")


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


class Outbox:
    def __init__(self, state_dir: Path, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.root = state_dir / OUTBOX_DIR
        self.max_attempts = max_attempts

    def _dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def enqueue(self, job: OutboxJob) -> Path:
        path = self._dir(PENDING) / job.name
        _atomic_write_json(path, job.to_json())
        return path

    def _read_dir(self, name: str) -> list[OutboxJob]:
        out = []
        for path in sorted(self._dir(name).glob("*.json")):
            try:
                out.append(OutboxJob.from_json(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def pending(self) -> list[OutboxJob]:
        return self._read_dir(PENDING)

    def failed(self) -> list[OutboxJob]:
        """Jobs parked after `max_attempts`. They are kept for the operator, never dropped."""
        return self._read_dir(FAILED)

    def is_done(self, job: OutboxJob) -> bool:
        return (self._dir(DONE) / job.name).exists()

    def claim(self, job: OutboxJob) -> bool:
        """Atomically take ownership. False means somebody else already has it."""
        source = self._dir(PENDING) / job.name
        target = self._dir(CLAIMED) / job.name
        try:
            os.rename(source, target)
        except FileNotFoundError:
            return False
        return True

    def complete(self, job: OutboxJob) -> None:
        _atomic_write_json(
            self._dir(DONE) / job.name,
            {**job.to_json(), "completed_at": datetime.now(timezone.utc).isoformat()},
        )
        (self._dir(CLAIMED) / job.name).unlink(missing_ok=True)

    def resolve(self, day: str, revision: int) -> bool:
        """Mark `(day, revision)` done because something else already ran it.

        The CLI needs this: `report evening --input` submits *and* runs the night, so the
        job the submission enqueued describes work that has already landed. Without this
        the worker would claim it, find the session already applied, and churn.
        """
        name = OutboxJob(day=day, revision=revision, content_hash="", enqueued_at="").name
        pending = self._dir(PENDING) / name
        job = OutboxJob(day=day, revision=revision, content_hash="", enqueued_at="")
        if pending.exists():
            try:
                job = OutboxJob.from_json(json.loads(pending.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                pass
        _atomic_write_json(
            self._dir(DONE) / name,
            {**job.to_json(), "completed_at": datetime.now(timezone.utc).isoformat()},
        )
        removed = pending.exists()
        pending.unlink(missing_ok=True)
        (self._dir(CLAIMED) / name).unlink(missing_ok=True)
        return removed

    def release(self, job: OutboxJob, error: str, *, count_attempt: bool = True) -> OutboxJob:
        """Return a failed job to `pending` for retry, or park it after max attempts.

        `count_attempt=False` is for "not wired yet": the job has to stay pending
        indefinitely, so polling every 30s must not park the day's evening in `failed/`.
        """
        retried = OutboxJob(
            day=job.day,
            revision=job.revision,
            content_hash=job.content_hash,
            enqueued_at=job.enqueued_at,
            attempts=job.attempts + 1 if count_attempt else job.attempts,
            last_error=error[:500],
        )
        (self._dir(CLAIMED) / job.name).unlink(missing_ok=True)
        if count_attempt and retried.attempts >= self.max_attempts:
            _atomic_write_json(self._dir(FAILED) / job.name, retried.to_json())
        else:
            _atomic_write_json(self._dir(PENDING) / job.name, retried.to_json())
        return retried


class OutboxWorker:
    """Single background thread. Bounded by construction: one job at a time."""

    def __init__(
        self,
        outbox: Outbox,
        dispatcher: Dispatcher | None = None,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self.dispatcher = dispatcher or LeavePendingDispatcher()
        self.on_event = on_event or (lambda message: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def drain_once(self) -> int:
        """Process everything currently pending. Returns how many completed."""
        completed = 0
        for job in self.outbox.pending():
            if self.outbox.is_done(job):
                # A previous attempt already produced the effect; never repeat it.
                (self.outbox._dir(PENDING) / job.name).unlink(missing_ok=True)
                continue
            if not self.outbox.claim(job):
                continue
            try:
                self.dispatcher(job)
            except PendingDispatch as exc:
                self.outbox.release(job, str(exc), count_attempt=False)
                self.on_event(f"pending: {exc}")
            except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the worker
                self.outbox.release(job, f"{type(exc).__name__}: {exc}")
                self.on_event(f"dispatch failed for {job.day}#{job.revision}")
            else:
                self.outbox.complete(job)
                completed += 1
        return completed

    def notify(self) -> None:
        self._wake.set()

    def start(self, *, interval_seconds: float = 30.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.drain_once()
                except Exception:  # noqa: BLE001
                    self.on_event("outbox worker iteration failed")
                self._wake.wait(interval_seconds)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="evening-outbox", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
