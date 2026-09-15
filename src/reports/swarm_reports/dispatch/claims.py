"""Durable dispatch claims: one launch per (date, task_id), safe to resume.

State lives outside Git under a configurable ``state_dir`` (see the design
note's "Repetição e agentes fora do fluxo": running the morning twice must
not re-dispatch). Each (date, task_id) has one claim file; ``try_claim`` is
the atomic gate against duplicate launches, including racing callers.

Two rules this module refuses to bend:

- **Elapsed time never authorizes a relaunch.** A job that has been running
  for six hours is a *slow* job, not a dead one. Resuming requires positive
  evidence that the recorded process is gone (its pid start-token no longer
  matches), or an explicit human ``allow_takeover``. Anything else raises
  ``RecoveryRequired`` — a subclass of ``DuplicateLaunch``, so a caller that
  only guards against duplicates still fails closed.
- **A completion may only close the attempt it belongs to.** ``mark_done`` /
  ``mark_failed`` take the attempt number they are closing and refuse to
  overwrite a newer one, so a straggler from attempt 1 cannot mark attempt 2
  finished.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .statefile import (
    ProbeUnavailable,
    ProcessProbe,
    StateError,
    SystemProcessProbe,
    file_lock,
    read_json,
    require,
    state_key,
    validate_id,
    validate_iso_date,
    write_json_atomic,
)

Status = Literal["pending", "running", "done", "failed"]
_STATUSES = ("pending", "running", "done", "failed")

STATE_VERSION = 1


@dataclass
class DispatchClaim:
    date: str
    task_id: str
    provider: str
    status: Status
    attempt: int
    updated_at: str
    pid: int | None = None
    process_token: str | None = None
    error: str | None = None
    version: int = STATE_VERSION


class DuplicateLaunch(Exception):
    """A live or completed claim already exists: do not launch."""


class RecoveryRequired(DuplicateLaunch):
    """A claim is 'running' but its process cannot be verified either way.

    Never resolved automatically. The operator inspects and re-runs with
    ``allow_takeover=True`` (or marks the claim failed) — a blind resume here
    is exactly how two agents end up writing the same branch.
    """


class StaleCompletion(Exception):
    """A completion arrived for an attempt that is no longer the current one."""


def _parse_claim(payload: dict) -> DispatchClaim:
    """Validate untrusted on-disk JSON before it becomes a claim object."""
    version = require(payload, "version", (int,))
    if version != STATE_VERSION:
        raise StateError(f"unsupported claim state version {version!r}")
    status = require(payload, "status", (str,))
    if status not in _STATUSES:
        raise StateError(f"unknown claim status {status!r}")
    attempt = require(payload, "attempt", (int,))
    if attempt < 1:
        raise StateError(f"attempt must be >= 1, got {attempt!r}")
    pid = payload.get("pid")
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        raise StateError(f"invalid pid {pid!r}")
    token = payload.get("process_token")
    if token is not None and not isinstance(token, str):
        raise StateError("process_token must be a string or null")
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise StateError("error must be a string or null")
    updated_at = require(payload, "updated_at", (str,))
    datetime.fromisoformat(updated_at)  # raises ValueError on garbage
    return DispatchClaim(
        date=validate_iso_date(require(payload, "date", (str,))),
        task_id=validate_id(require(payload, "task_id", (str,)), field="task_id"),
        provider=validate_id(require(payload, "provider", (str,)), field="provider"),
        status=status,  # type: ignore[arg-type]
        attempt=attempt,
        updated_at=updated_at,
        pid=pid,
        process_token=token,
        error=error,
        version=version,
    )


class ClaimStore:
    def __init__(self, state_dir: Path, *, probe: ProcessProbe | None = None) -> None:
        self._dir = Path(state_dir) / "dispatch-claims"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._probe = probe or SystemProcessProbe()

    def _path(self, date: str, task_id: str) -> Path:
        return self._dir / f"{state_key(date, task_id)}.json"

    def _lock_path(self, date: str, task_id: str) -> Path:
        return self._dir / f"{state_key(date, task_id)}.lock"

    def load(self, date: str, task_id: str) -> DispatchClaim | None:
        payload = read_json(self._path(date, task_id))
        if payload is None:
            return None
        return _parse_claim(payload)

    def _still_alive(self, claim: DispatchClaim) -> bool | None:
        """True = same process still running, False = gone, None = cannot tell."""
        if claim.pid is None or claim.process_token is None:
            return None
        try:
            token = self._probe.start_token(claim.pid)
        except ProbeUnavailable:
            return None  # cannot tell: never resolved by guessing
        if token is None:
            return False  # the probe answered "no such process"
        return token == claim.process_token

    def try_claim(
        self,
        date: str,
        task_id: str,
        provider: str,
        *,
        now: datetime,
        pid: int | None = None,
        process_token: str | None = None,
        allow_takeover: bool = False,
    ) -> DispatchClaim:
        """Atomically claim (date, task_id) for launch, or refuse.

        ``pid``/``process_token`` identify the process that will own the job
        (see ``statefile.current_process_token``). Recording them is what
        makes a later resume decision evidence-based instead of a guess.
        """
        validate_id(provider, field="provider")
        with file_lock(self._lock_path(date, task_id)):
            existing = self.load(date, task_id)
            if existing is not None:
                if existing.status == "done":
                    raise DuplicateLaunch(f"{date}/{task_id} already done")
                if existing.status == "running":
                    alive = self._still_alive(existing)
                    if alive is True:
                        raise DuplicateLaunch(
                            f"{date}/{task_id} still running as pid {existing.pid} "
                            f"(attempt {existing.attempt}); elapsed time does not authorize a relaunch"
                        )
                    if alive is None and not allow_takeover:
                        raise RecoveryRequired(
                            f"{date}/{task_id} is 'running' but its process cannot be verified "
                            f"(pid={existing.pid!r}); operator must confirm with allow_takeover=True"
                        )
                attempt = existing.attempt + 1
            else:
                attempt = 1

            claim = DispatchClaim(
                date=validate_iso_date(date),
                task_id=validate_id(task_id, field="task_id"),
                provider=provider,
                status="running",
                attempt=attempt,
                updated_at=now.isoformat(),
                pid=pid,
                process_token=process_token,
            )
            self._write(claim)
            return claim

    def _write(self, claim: DispatchClaim) -> None:
        write_json_atomic(self._path(claim.date, claim.task_id), asdict(claim))

    def _close(
        self, date: str, task_id: str, *, attempt: int, status: Status, now: datetime, error: str | None
    ) -> DispatchClaim:
        with file_lock(self._lock_path(date, task_id)):
            claim = self.load(date, task_id)
            if claim is None:
                raise LookupError(f"no claim for {date}/{task_id}")
            if attempt != claim.attempt:
                raise StaleCompletion(
                    f"{date}/{task_id}: completion for attempt {attempt}, current attempt is {claim.attempt}"
                )
            if claim.status in ("done", "failed") and claim.status != status:
                raise StaleCompletion(
                    f"{date}/{task_id}: attempt {attempt} is already {claim.status}, refusing to set {status}"
                )
            claim.status = status
            claim.error = error
            claim.updated_at = now.isoformat()
            self._write(claim)
            return claim

    def mark_done(self, date: str, task_id: str, *, attempt: int, now: datetime) -> DispatchClaim:
        return self._close(date, task_id, attempt=attempt, status="done", now=now, error=None)

    def mark_failed(self, date: str, task_id: str, error: str, *, attempt: int, now: datetime) -> DispatchClaim:
        return self._close(date, task_id, attempt=attempt, status="failed", now=now, error=str(error)[:500])

    def list_claims(self, date: str) -> list[DispatchClaim]:
        """All claims for a date, for the report's "o que fiz ontem" section."""
        validate_iso_date(date)
        claims = []
        for path in sorted(self._dir.glob(f"{date}__*.json")):
            payload = read_json(path)
            if payload is not None:
                claims.append(_parse_claim(payload))
        return claims


__all__ = [
    "ClaimStore",
    "DispatchClaim",
    "DuplicateLaunch",
    "RecoveryRequired",
    "StaleCompletion",
    "STATE_VERSION",
]


def _selfcheck() -> None:  # pragma: no cover - runnable smoke check
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = ClaimStore(Path(tmp), probe=type("P", (), {"start_token": lambda self, pid: "tok"})())
        now = datetime(2026, 9, 14, 6, 0)
        c = store.try_claim("2026-09-14", "MAT-1", "claude", now=now, pid=1, process_token="tok")
        assert c.attempt == 1
        try:
            store.try_claim("2026-09-14", "MAT-1", "claude", now=now, pid=1, process_token="tok")
            raise AssertionError("duplicate launch not blocked")
        except DuplicateLaunch:
            pass
        assert json.loads(store._path("2026-09-14", "MAT-1").read_text())["attempt"] == 1
        print("claims selfcheck ok")


if __name__ == "__main__":  # pragma: no cover
    _selfcheck()
