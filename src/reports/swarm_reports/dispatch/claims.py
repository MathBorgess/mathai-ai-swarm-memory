"""Durable dispatch claims: one launch per (date, task_id), safe to resume.

State lives outside Git under a configurable ``state_dir`` (see the design
note's "Repetição e agentes fora do fluxo": running the morning twice must
not re-dispatch). Each (date, task_id) has one claim file; ``try_claim`` is
the atomic gate against duplicate launches, including racing calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

Status = Literal["pending", "running", "done", "failed"]

DEFAULT_RUNNING_TIMEOUT = timedelta(hours=2)


@dataclass
class DispatchClaim:
    date: str
    task_id: str
    provider: str
    status: Status
    attempt: int
    updated_at: str
    error: str | None = None


class DuplicateLaunch(Exception):
    """Raised by try_claim() when a live or completed claim already exists."""


class ClaimStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "dispatch-claims"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, date: str, task_id: str) -> Path:
        safe_task = task_id.replace("/", "_")
        return self._dir / f"{date}__{safe_task}.json"

    def _lock_path(self, date: str, task_id: str) -> Path:
        return self._path(date, task_id).with_suffix(".lock")

    def load(self, date: str, task_id: str) -> DispatchClaim | None:
        path = self._path(date, task_id)
        if not path.exists():
            return None
        return DispatchClaim(**json.loads(path.read_text()))

    def try_claim(
        self,
        date: str,
        task_id: str,
        provider: str,
        *,
        now: datetime,
        running_timeout: timedelta = DEFAULT_RUNNING_TIMEOUT,
    ) -> DispatchClaim:
        """Atomically claim (date, task_id) for launch, or raise DuplicateLaunch.

        A "running" claim older than ``running_timeout`` is treated as a
        failed attempt and may be resumed (attempt += 1). "done" is final.
        The exclusive-create lock file is what makes two concurrent callers
        race safely: only one ever gets past ``os.O_EXCL``.
        """
        lock_path = self._lock_path(date, task_id)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError as exc:
            raise DuplicateLaunch(f"{date}/{task_id} claim in progress") from exc

        try:
            existing = self.load(date, task_id)
            if existing is not None:
                if existing.status == "done":
                    raise DuplicateLaunch(f"{date}/{task_id} already done")
                if existing.status == "running":
                    updated_at = datetime.fromisoformat(existing.updated_at)
                    if now - updated_at < running_timeout:
                        raise DuplicateLaunch(f"{date}/{task_id} already running")
                    attempt = existing.attempt + 1
                else:  # pending or failed: resumable
                    attempt = existing.attempt + 1
            else:
                attempt = 1

            claim = DispatchClaim(
                date=date,
                task_id=task_id,
                provider=provider,
                status="running",
                attempt=attempt,
                updated_at=now.isoformat(),
            )
            self._overwrite(claim)
            return claim
        finally:
            lock_path.unlink(missing_ok=True)

    def _overwrite(self, claim: DispatchClaim) -> None:
        path = self._path(claim.date, claim.task_id)
        path.write_text(json.dumps(asdict(claim)))

    def mark_done(self, date: str, task_id: str, *, now: datetime) -> None:
        claim = self.load(date, task_id)
        if claim is None:
            raise LookupError(f"no claim for {date}/{task_id}")
        claim.status = "done"
        claim.updated_at = now.isoformat()
        self._overwrite(claim)

    def mark_failed(self, date: str, task_id: str, error: str, *, now: datetime) -> None:
        claim = self.load(date, task_id)
        if claim is None:
            raise LookupError(f"no claim for {date}/{task_id}")
        claim.status = "failed"
        claim.error = error
        claim.updated_at = now.isoformat()
        self._overwrite(claim)
