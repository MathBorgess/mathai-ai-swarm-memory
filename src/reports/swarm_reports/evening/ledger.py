"""Durable ledger of autonomous actions and suggestions.

It lives in `state_dir`, outside Git, because it is operational state and not
knowledge: the vault records what was decided, the ledger records what the mechanism
actually did. `mathai-ai-logs` is a future repository, not this one.

Two properties the rest of F4 leans on:

- **An effect is never reported as `completed` before it landed.** Every action is
  written `pending` *before* the attempt and moved to `completed` or `failed` after it,
  so a crash in between leaves a truthful `pending`, never a lie. A pull request that
  failed to open shows up as `failed` with the transport error next to it.
- **Entry ids are deterministic** — `(day, revision, kind, target)` — so replaying the
  same revision after a crash upserts the same rows instead of duplicating the day's
  history in the next morning's report.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

LEDGER_DIR = "ledger"

#: The effect is recorded but has not landed yet. Also the honest resting state for
#: anything the owner still has to look at.
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
#: Waiting for the owner: suggestions, contests, drafts. Never an automatic effect.
STATUS_REVIEW = "pending-review"
#: The owner saw it in an evening form and moved on.
STATUS_ACKNOWLEDGED = "acknowledged"

STATUSES = frozenset(
    {STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED, STATUS_REVIEW, STATUS_ACKNOWLEDGED}
)

#: Effects. `wiki-pr` covers the daily/posts commit that the vault governance requires
#: to travel as a branch + pull request.
KIND_WIKI_COMMIT = "wiki-commit"
KIND_WIKI_PR = "wiki-pr"
KIND_POST_NOTE = "post-note"
KIND_TOMORROW_PLAN = "tomorrow-plan"
#: Owner-facing, never executed automatically.
KIND_CONTEST = "contest"
KIND_SUGGESTION = "suggestion"
KIND_REFLECTION = "reflection"

MAX_SUMMARY = 500
MAX_DETAIL = 2000


@dataclass(frozen=True)
class LedgerRecord:
    entry_id: str
    day: str
    revision: int
    kind: str
    target: str
    status: str
    summary: str
    provenance: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""
    updated_at: str = ""
    link: str | None = None
    detail: str = ""
    #: Set by the owner in a later evening form; the mechanism never writes it.
    classification: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "day": self.day,
            "revision": self.revision,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "provenance": dict(self.provenance),
            "recorded_at": self.recorded_at,
            "updated_at": self.updated_at,
            "link": self.link,
            "detail": self.detail,
            "classification": self.classification,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LedgerRecord:
        return cls(
            entry_id=str(data["entry_id"]),
            day=str(data["day"]),
            revision=int(data.get("revision") or 0),
            kind=str(data.get("kind") or ""),
            target=str(data.get("target") or ""),
            status=str(data.get("status") or STATUS_PENDING),
            summary=str(data.get("summary") or ""),
            provenance=dict(data.get("provenance") or {}),
            recorded_at=str(data.get("recorded_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            link=data.get("link"),
            detail=str(data.get("detail") or ""),
            classification=data.get("classification"),
        )

    @property
    def needs_owner(self) -> bool:
        return self.status == STATUS_REVIEW

    @property
    def landed(self) -> bool:
        return self.status == STATUS_COMPLETED


def entry_id_for(day: date | str, revision: int, kind: str, target: str) -> str:
    """Deterministic, so replaying a revision upserts instead of appending."""
    key = day if isinstance(day, str) else day.isoformat()
    digest = hashlib.sha256(f"{key}|{revision}|{kind}|{target}".encode("utf-8")).hexdigest()
    return f"{key}-{kind}-{digest[:10]}"


class LedgerStore:
    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / LEDGER_DIR

    def _path(self, day: date | str) -> Path:
        key = day if isinstance(day, str) else day.isoformat()
        return self.root / f"{key}.json"

    @contextmanager
    def _locked(self, day: date | str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._path(day).with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def entries(self, day: date | str) -> list[LedgerRecord]:
        path = self._path(day)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"corrupt ledger at {path}: {exc}") from exc
        raw = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        return [LedgerRecord.from_json(item) for item in raw if isinstance(item, dict)]

    def entries_between(self, start: date, end: date) -> list[LedgerRecord]:
        """Inclusive on both ends, oldest first."""
        out: list[LedgerRecord] = []
        current = start
        while current <= end:
            out.extend(self.entries(current))
            current += timedelta(days=1)
        return out

    def get(self, day: date | str, entry_id: str) -> LedgerRecord | None:
        for record in self.entries(day):
            if record.entry_id == entry_id:
                return record
        return None

    def record(self, entry: LedgerRecord, *, now: datetime | None = None) -> LedgerRecord:
        """Upsert by `entry_id`, preserving the original `recorded_at`."""
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._locked(entry.day):
            rows = self.entries(entry.day)
            merged = replace(
                entry,
                summary=entry.summary[:MAX_SUMMARY],
                detail=entry.detail[:MAX_DETAIL],
                recorded_at=entry.recorded_at or stamp,
                updated_at=stamp,
            )
            for index, existing in enumerate(rows):
                if existing.entry_id == entry.entry_id:
                    merged = replace(
                        merged,
                        recorded_at=existing.recorded_at or merged.recorded_at,
                        # The owner's classification is theirs; a replay never clears it.
                        classification=(
                            merged.classification
                            if merged.classification is not None
                            else existing.classification
                        ),
                    )
                    rows[index] = merged
                    break
            else:
                rows.append(merged)
            self._write(entry.day, rows)
        return merged

    def set_status(
        self,
        day: date | str,
        entry_id: str,
        status: str,
        *,
        link: str | None = None,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        if status not in STATUSES:
            raise ValueError(f"unknown ledger status '{status}'")
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._locked(day):
            rows = self.entries(day)
            for index, existing in enumerate(rows):
                if existing.entry_id != entry_id:
                    continue
                updated = replace(
                    existing,
                    status=status,
                    updated_at=stamp,
                    link=link if link is not None else existing.link,
                    detail=(detail[:MAX_DETAIL] if detail is not None else existing.detail),
                )
                rows[index] = updated
                self._write(day, rows)
                return updated
        raise KeyError(f"ledger entry '{entry_id}' not found for {day}")

    def classify(
        self,
        day: date | str,
        entry_id: str,
        classification: str | None,
        *,
        now: datetime | None = None,
    ) -> LedgerRecord | None:
        """Apply an owner classification from an evening form. Unknown ids are ignored.

        The evening payload can name an entry from any past day (the form seeds them
        from the morning report), so a miss here is normal and must not fail the night.
        """
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._locked(day):
            rows = self.entries(day)
            for index, existing in enumerate(rows):
                if existing.entry_id != entry_id:
                    continue
                updated = replace(
                    existing,
                    classification=classification,
                    updated_at=stamp,
                    status=(
                        STATUS_ACKNOWLEDGED
                        if existing.status == STATUS_REVIEW
                        else existing.status
                    ),
                )
                rows[index] = updated
                self._write(day, rows)
                return updated
        return None

    def _write(self, day: date | str, rows: list[LedgerRecord]) -> None:
        key = day if isinstance(day, str) else day.isoformat()
        payload = {
            "version": 1,
            "day": key,
            "entries": [row.to_json() for row in rows],
        }
        _atomic_write_json(self._path(key), payload)


def morning_view(
    state_dir: Path,
    day: date,
    *,
    lookback_days: int = 7,
    max_entries: int = 24,
) -> tuple[list[Any], list[Any]]:
    """What the morning report shows: yesterday's actions, and what still needs the owner.

    Returns `(ledger_entries, review_drafts)` in the plan's own types, so the renderer
    does not need to know the ledger exists. "What I did yesterday" is the previous day's
    rows; contests and suggestions stay visible until the owner answers them, however
    many days that takes, because a question that scrolls away was never asked.
    """
    from swarm_reports.plan import LedgerEntry, ReviewDraft

    store = LedgerStore(state_dir)
    yesterday = day - timedelta(days=1)
    recent = store.entries(yesterday)
    window = store.entries_between(day - timedelta(days=lookback_days), day)

    entries = [
        LedgerEntry(
            entry_id=record.entry_id,
            kind=record.kind[:32],
            summary=_with_status(record),
            link=record.link,
        )
        for record in recent
        if record.kind not in (KIND_CONTEST, KIND_SUGGESTION)
    ]

    drafts = [
        ReviewDraft(
            draft_id=record.entry_id[:64],
            kind=record.kind[:32],
            reason=record.summary[:500],
            content=record.detail[:8000],
            status="pending",
        )
        for record in window
        if record.needs_owner
    ]
    return entries[-max_entries:], drafts[-max_entries:]


def _with_status(record: LedgerRecord) -> str:
    """The status is part of the summary on purpose: the morning must be able to say
    "the pull request did not open" instead of printing the intent as if it were done."""
    if record.status == STATUS_COMPLETED:
        return record.summary
    suffix = {
        STATUS_PENDING: "pendente",
        STATUS_FAILED: "FALHOU",
        STATUS_REVIEW: "aguarda você",
        STATUS_ACKNOWLEDGED: "visto",
    }.get(record.status, record.status)
    detail = f" — {record.detail}" if record.status == STATUS_FAILED and record.detail else ""
    return f"[{suffix}] {record.summary}{detail}"


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
