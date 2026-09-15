"""Runtime state for daily reports (outside Git, configurable state_dir).

Freeze semantics (F1 contract for F2–F6):
- Morning freeze writes `frozen_checklist` once per calendar day (idempotent).
- Denominator for completion uses `frozen_checklist` task ids, never lines added later.
- `evening_validated` gates completion; absent evening excludes the day from completion.
- Carryover stores `first_planned` + stable `task_id` for date-drift across days.

Concurrency: F2 owns cross-process file locking. Callers that read-modify-write
`reports-state.json` must hold that lock; F1 only guarantees atomic replace on save.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

STATE_VERSION = 1
MAX_P0_PER_DAY = 3


@dataclass
class FrozenItem:
    task_id: str
    text: str
    first_planned: date
    is_p0: bool = False
    frozen: bool = True

    def to_json(self) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "text": self.text,
            "first_planned": self.first_planned.isoformat(),
            "is_p0": self.is_p0,
        }
        if self.frozen is not True:
            payload["frozen"] = self.frozen
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FrozenItem:
        return cls(
            task_id=str(data["task_id"]),
            text=str(data.get("text") or ""),
            first_planned=date.fromisoformat(str(data["first_planned"])),
            is_p0=bool(data.get("is_p0")),
            frozen=bool(data.get("frozen", True)),
        )


@dataclass
class DayState:
    frozen_checklist: list[FrozenItem] = field(default_factory=list)
    morning_freeze_applied: bool = False
    frozen_at_commit: str | None = None
    frozen_at_snapshot: str | None = None
    evening_validated: bool = False
    evening_absent: bool = False
    carryover: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "frozen_checklist": [item.to_json() for item in self.frozen_checklist],
            "morning_freeze_applied": self.morning_freeze_applied,
            "frozen_at_commit": self.frozen_at_commit,
            "frozen_at_snapshot": self.frozen_at_snapshot,
            "evening_validated": self.evening_validated,
            "evening_absent": self.evening_absent,
            "carryover": self.carryover,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DayState:
        frozen_raw = data.get("frozen_checklist") or []
        frozen = [
            FrozenItem.from_json(entry)
            for entry in frozen_raw
            if isinstance(entry, dict)
        ]
        morning_freeze_applied = bool(data.get("morning_freeze_applied"))
        if not morning_freeze_applied and frozen:
            morning_freeze_applied = True
        return cls(
            frozen_checklist=frozen,
            morning_freeze_applied=morning_freeze_applied,
            frozen_at_commit=data.get("frozen_at_commit"),
            frozen_at_snapshot=data.get("frozen_at_snapshot"),
            evening_validated=bool(data.get("evening_validated")),
            evening_absent=bool(data.get("evening_absent")),
            carryover=dict(data.get("carryover") or {}),
        )


@dataclass
class ReportsState:
    version: int = STATE_VERSION
    days: dict[str, DayState] = field(default_factory=dict)

    def day_key(self, day: date) -> str:
        return day.isoformat()

    def get_day(self, day: date) -> DayState:
        return self.days.setdefault(self.day_key(day), DayState())

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "days": {key: value.to_json() for key, value in self.days.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReportsState:
        days_raw = data.get("days") or {}
        days = {
            str(key): DayState.from_json(value)
            for key, value in days_raw.items()
            if isinstance(value, dict)
        }
        return cls(version=int(data.get("version", STATE_VERSION)), days=days)


def default_state_path(state_dir: Path) -> Path:
    return state_dir / "reports-state.json"


def load_state(path: Path) -> ReportsState:
    if not path.exists():
        return ReportsState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReportsState.from_json(data)


def save_state(path: Path, state: ReportsState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".reports-state-", dir=path.parent)
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


def _validate_freeze_items(items: list[FrozenItem]) -> None:
    seen: set[str] = set()
    p0_count = 0
    for item in items:
        if item.task_id in seen:
            raise ValueError(f"duplicate frozen task_id '{item.task_id}'")
        seen.add(item.task_id)
        if item.is_p0:
            p0_count += 1
    if p0_count > MAX_P0_PER_DAY:
        raise ValueError(f"at most {MAX_P0_PER_DAY} P0 items allowed in morning freeze")


def apply_morning_freeze(
    state: ReportsState,
    day: date,
    items: list[FrozenItem],
    *,
    commit: str | None = None,
    snapshot: str | None = None,
) -> bool:
    """Record frozen checklist for `day`. Returns False if already frozen (no-op)."""
    bucket = state.get_day(day)
    if bucket.morning_freeze_applied:
        return False
    _validate_freeze_items(items)
    bucket.frozen_checklist = [
        FrozenItem(
            task_id=item.task_id,
            text=item.text,
            first_planned=item.first_planned,
            is_p0=item.is_p0,
            frozen=item.frozen,
        )
        for item in items
    ]
    bucket.morning_freeze_applied = True
    bucket.frozen_at_commit = commit
    bucket.frozen_at_snapshot = snapshot
    for item in bucket.frozen_checklist:
        bucket.carryover[item.task_id] = {
            "first_planned": item.first_planned.isoformat(),
            "text": item.text,
            "is_p0": item.is_p0,
            "frozen": item.frozen,
        }
    return True


def mark_evening_validated(state: ReportsState, day: date, *, absent: bool = False) -> None:
    bucket = state.get_day(day)
    if absent:
        bucket.evening_absent = True
        bucket.evening_validated = False
        return
    bucket.evening_validated = True
    bucket.evening_absent = False
