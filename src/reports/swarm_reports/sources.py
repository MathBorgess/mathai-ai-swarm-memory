"""Planner source adapters (Linear, Calendar) — injectable for tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from swarm_reports.plan import MAX_P0, AgendaEntry, LessonBlock, MorningPlan, SourceRef

#: Linear priority numbers that the design counts as P0-eligible.
URGENT = 1
HIGH = 2
P0_PRIORITY_LABELS = frozenset({"urgent", "urgente", "high", "alta"})


@dataclass(frozen=True)
class CandidateItem:
    task_id: str
    text: str
    source_pointer: str
    priority: int | None = None
    priority_label: str | None = None
    deadline: date | None = None

    @property
    def priority_rank(self) -> int:
        if self.priority is not None:
            return self.priority
        label = (self.priority_label or "").strip().lower()
        if label in {"urgent", "urgente"}:
            return URGENT
        if label in {"high", "alta"}:
            return HIGH
        return 99

    def is_high_priority(self) -> bool:
        if self.priority is not None and self.priority in (URGENT, HIGH):
            return True
        return (self.priority_label or "").strip().lower() in P0_PRIORITY_LABELS


class PlannerSources(Protocol):
    def linear_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]: ...

    def calendar_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]: ...


class UnavailableSources:
    """Explicit source-unavailable markers (never invent issues/events)."""

    def linear_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="linear", pointer="", status="unavailable")

    def calendar_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="calendar", pointer="", status="unavailable")


class EmptyConfirmedSources:
    """Both sources answered and had nothing: an empty day the owner can freeze."""

    def linear_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="linear", pointer="confirmed-empty", status="ok")

    def calendar_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="calendar", pointer="confirmed-empty", status="ok")


def is_p0_eligible(item: CandidateItem, day: date, *, horizon_days: int = 7) -> bool:
    """Urgent/High priority **or** a calendar deadline inside the horizon.

    The design uses OR, not AND: an Urgent issue with no date is P0, and so is a
    hard deadline in three days that carries no Linear priority.
    """
    if item.is_high_priority():
        return True
    if item.deadline is not None:
        return day <= item.deadline <= day + timedelta(days=horizon_days)
    return False


def select_p0(
    candidates: list[CandidateItem],
    day: date,
    *,
    horizon_days: int = 7,
) -> list[CandidateItem]:
    eligible = [c for c in candidates if is_p0_eligible(c, day, horizon_days=horizon_days)]
    eligible.sort(
        key=lambda item: (
            item.priority_rank,
            item.deadline or date.max,
            item.task_id,
        )
    )
    return eligible[:MAX_P0]


def build_plan_from_sources(
    day: date,
    sources: PlannerSources,
    *,
    lesson_link: str | None = None,
    lesson_topic: str | None = None,
    agenda: list[AgendaEntry] | None = None,
) -> MorningPlan:
    linear_items, linear_ref = sources.linear_candidates(day)
    calendar_items, calendar_ref = sources.calendar_candidates(day)

    merged: dict[str, CandidateItem] = {}
    for item in list(linear_items) + list(calendar_items):
        merged.setdefault(item.task_id, item)
    ordered = list(merged.values())
    p0_ids = {item.task_id for item in select_p0(ordered, day, horizon_days=7)}

    checklist = [
        {
            "task_id": item.task_id,
            "text": item.text,
            "first_planned": day.isoformat(),
            "is_p0": item.task_id in p0_ids,
            "source_pointer": item.source_pointer,
        }
        for item in ordered
    ]
    refs = [linear_ref, calendar_ref]
    return MorningPlan(
        day=day,
        checklist=checklist,
        handoffs=[],
        sources=refs,
        agenda=list(agenda or []),
        lesson=LessonBlock(link=lesson_link, topic=lesson_topic) if lesson_link else None,
        confirmed_empty=not checklist and all(ref.confirms_empty for ref in refs),
    )
