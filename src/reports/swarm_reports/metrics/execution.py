"""Execution metrics: completion, date drift, scope penalty."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable

from swarm_reports.metrics.daily import ChecklistItem, DailyNote


class ScopeClassification(str, Enum):
    OPPORTUNITY = "oportunidade"
    PROCRASTINATION = "procrastinacao"
    PROCRASTINATION_ALT = "procrastinação"
    DAYDREAM = "devaneio"
    UNCLASSIFIED = "unclassified"


PENALIZED = {
    ScopeClassification.PROCRASTINATION.value,
    ScopeClassification.PROCRASTINATION_ALT.value,
    ScopeClassification.DAYDREAM.value,
    "procrastination",
    "devaneio",
}


@dataclass(frozen=True)
class UnplannedCompletion:
    task_id: str
    classification: str | None
    completed_on: date


@dataclass(frozen=True)
class EveningSubmission:
    day: date
    validated_complete_ids: frozenset[str]
    unplanned_completed: tuple[UnplannedCompletion, ...] = ()
    p0_open_ids: frozenset[str] = frozenset()


def _normalize_class(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


def frozen_denominator_ids(
    note: DailyNote,
    frozen_snapshot_ids: Iterable[str] | None,
) -> list[str]:
    if frozen_snapshot_ids is not None:
        return list(frozen_snapshot_ids)
    return [item.task_id for item in note.items if item.frozen and not item.added_after_freeze]


def compute_completion(
    note: DailyNote,
    evening: EveningSubmission | None,
    *,
    frozen_snapshot_ids: Iterable[str] | None = None,
) -> float | None:
    if note.evening_absent:
        return None
    if evening is None and not note.evening_validated:
        return None

    proposed_ids = frozen_denominator_ids(note, frozen_snapshot_ids)
    if not proposed_ids:
        return None

    if evening is None:
        return None
    validated = evening.validated_complete_ids
    completed = sum(1 for task_id in proposed_ids if task_id in validated)
    return completed / len(proposed_ids)


def compute_date_drift(
    items: list[ChecklistItem],
    as_of: date,
    *,
    open_task_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    open_ids = set(open_task_ids) if open_task_ids is not None else None
    drift: dict[str, int] = {}
    for item in items:
        if open_ids is not None and item.task_id not in open_ids:
            continue
        if item.done and open_ids is None:
            continue
        if item.first_planned is None:
            continue
        days = (as_of - item.first_planned).days
        if days < 0:
            days = 0
        drift[item.task_id] = days
    return drift


def count_open_p0(items: list[ChecklistItem], *, validated_ids: set[str] | None = None) -> list[str]:
    open_p0: list[str] = []
    for item in items:
        if not item.is_p0:
            continue
        if validated_ids is not None and item.task_id in validated_ids:
            continue
        if item.done and validated_ids is None:
            continue
        open_p0.append(item.task_id)
    return open_p0[:3]


def compute_scope_penalty(
    evening: EveningSubmission,
    p0_open_at_evening: frozenset[str],
) -> int:
    if not p0_open_at_evening:
        return 0
    penalty = 0
    for entry in evening.unplanned_completed:
        classification = _normalize_class(entry.classification)
        if classification in {"oportunidade", "opportunity"}:
            continue
        if classification is None or classification in {"unclassified", ""}:
            continue
        if classification in PENALIZED:
            penalty += 1
    return penalty
