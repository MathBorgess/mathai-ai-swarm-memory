"""Runtime state for daily reports (outside Git, configurable state_dir).

Freeze semantics (F1 contract for F2–F6):
- Morning freeze writes `frozen_checklist` once per calendar day (idempotent).
- Denominator for completion uses `frozen_checklist` task ids, never lines added later.
- `evening_validated` gates completion; absent evening excludes the day from completion.
- Carryover stores `first_planned` + stable `task_id` for date-drift across days.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

STATE_VERSION = 1


@dataclass
class FrozenItem:
    task_id: str
    text: str
    first_planned: date
    is_p0: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "text": self.text,
            "first_planned": self.first_planned.isoformat(),
            "is_p0": self.is_p0,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FrozenItem:
        return cls(
            task_id=str(data["task_id"]),
            text=str(data.get("text") or ""),
            first_planned=date.fromisoformat(str(data["first_planned"])),
            is_p0=bool(data.get("is_p0")),
        )


@dataclass
class DayState:
    frozen_checklist: list[FrozenItem] = field(default_factory=list)
    frozen_at_commit: str | None = None
    frozen_at_snapshot: str | None = None
    evening_validated: bool = False
    evening_absent: bool = False
    carryover: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "frozen_checklist": [item.to_json() for item in self.frozen_checklist],
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
        return cls(
            frozen_checklist=frozen,
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
    path.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    if bucket.frozen_checklist:
        return False
    bucket.frozen_checklist = list(items)
    bucket.frozen_at_commit = commit
    bucket.frozen_at_snapshot = snapshot
    for item in items:
        bucket.carryover[item.task_id] = {
            "first_planned": item.first_planned.isoformat(),
            "text": item.text,
            "is_p0": item.is_p0,
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
