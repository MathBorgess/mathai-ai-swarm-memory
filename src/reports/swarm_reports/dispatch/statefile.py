"""Durable state primitives shared by the claim store and the quota ledger.

Three properties every state file here must have, because a dispatcher can be
killed at any moment (laptop sleeps, VPS reboots, provider CLI hangs):

1. **Locks release on crash.** `fcntl.flock` is held by the *open file
   description*; the kernel drops it when the process dies. A create/delete
   lock file does not, and a crash mid-run would wedge that key forever.
2. **Writes are atomic and private.** Write to a temp file in the same
   directory with mode 0600, fsync, then `os.replace`. A reader never sees a
   half-written JSON, and no state file is world-readable.
3. **Reads are validated.** JSON on disk is untrusted input (another tool, an
   older version, a partial restore). Types are checked before use.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ID_LEN = 200


class StateError(Exception):
    """State on disk is missing, malformed, or of an unexpected shape."""


def validate_iso_date(value: str) -> str:
    """Accept only a real calendar date in YYYY-MM-DD. No traversal, no slack."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise ValueError(f"not an ISO date (YYYY-MM-DD): {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a real calendar date: {value!r}") from exc
    return value


def validate_id(value: str, *, field: str = "id") -> str:
    """A task/account id: non-empty, bounded, printable, single line."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > MAX_ID_LEN:
        raise ValueError(f"{field} longer than {MAX_ID_LEN} chars")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{field} contains control characters")
    return value


def state_key(date_str: str, task_id: str) -> str:
    """Filesystem-safe key for (date, task_id).

    The task id is hashed rather than escaped: escaping `/` to `_` collides
    (`a/b` and `a_b` land on one file) and leaves `..` and absolute paths to
    be handled by hand. A digest has none of those failure modes, and the raw
    id is kept inside the file so nothing is lost.
    """
    validate_iso_date(date_str)
    validate_id(task_id, field="task_id")
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:32]
    return f"{date_str}__{digest}"


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``path``, released by the kernel on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(dict(payload), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    """Return a JSON object from ``path``, or None if absent. Raises on garbage."""
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise StateError(f"corrupt state file {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StateError(f"state file {path} is not a JSON object")
    return loaded


def require(payload: Mapping[str, Any], field: str, types: tuple[type, ...]) -> Any:
    if field not in payload:
        raise StateError(f"state missing required field {field!r}")
    value = payload[field]
    if not isinstance(value, types) or isinstance(value, bool) and bool not in types:
        raise StateError(f"state field {field!r} has type {type(value).__name__}")
    return value


class ProbeUnavailable(Exception):
    """The process probe could not answer. Not the same as "the process is gone"."""


class ProcessProbe(Protocol):
    def start_token(self, pid: int) -> str | None:
        """Start token of ``pid``, or None if no such process.

        Raises ``ProbeUnavailable`` when the answer is unknown. "Gone" and
        "cannot tell" must stay distinguishable: one authorizes a resume, the
        other must not.
        """


class SystemProcessProbe:
    """`ps`-based identity: a pid alone is reusable, a pid + start time is not."""

    def start_token(self, pid: int) -> str | None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ProbeUnavailable(f"not a usable pid: {pid!r}")
        try:
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5.0,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProbeUnavailable(f"ps failed: {exc}") from exc
        if proc.returncode == 1:
            return None  # ps: documented "no matching process"
        if proc.returncode != 0:
            raise ProbeUnavailable(f"ps exited {proc.returncode}")
        token = proc.stdout.strip()
        if not token:
            raise ProbeUnavailable("ps reported no start time")
        return token


def current_process_token(pid: int | None = None, probe: ProcessProbe | None = None) -> tuple[int, str | None]:
    """(pid, start token) for this process, to be recorded alongside a claim."""
    pid = os.getpid() if pid is None else pid
    probe = probe or SystemProcessProbe()
    try:
        return pid, probe.start_token(pid)
    except ProbeUnavailable:
        return pid, None
