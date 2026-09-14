"""Planner source adapters (Linear, Calendar) — injectable for tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from swarm_reports.plan import MorningPlan, SourceRef


@dataclass(frozen=True)
class CandidateItem:
    task_id: str
    text: str
    source_pointer: str
    priority: int | None = None
    deadline: date | None = None


class PlannerSources(Protocol):
    def linear_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]: ...

    def calendar_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]: ...


class UnavailableSources:
    """Explicit source-unavailable markers (never invent issues/events)."""

    def linear_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="linear", pointer="", status="unavailable")

    def calendar_candidates(self, day: date) -> tuple[list[CandidateItem], SourceRef]:
        return [], SourceRef(kind="calendar", pointer="", status="unavailable")


def select_p0(candidates: list[CandidateItem], day: date, *, horizon_days: int = 7) -> list[CandidateItem]:
    horizon = day + timedelta(days=horizon_days)
    scored: list[tuple[int, CandidateItem]] = []
    for item in candidates:
        if item.deadline and item.deadline > horizon:
            continue
        priority = item.priority if item.priority is not None else 99
        scored.append((priority, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].deadline or day, pair[1].task_id))
    return [item for _, item in scored[:3]]


def build_plan_from_sources(
    day: date,
    sources: PlannerSources,
    *,
    lesson_link: str | None = None,
    lesson_topic: str | None = None,
) -> MorningPlan:
    from swarm_reports.plan import LessonBlock

    linear_items, linear_ref = sources.linear_candidates(day)
    calendar_items, calendar_ref = sources.calendar_candidates(day)
    merged = {item.task_id: item for item in linear_items + calendar_items}
    p0 = select_p0(list(merged.values()), day)
    p0_payload = [
        {
            "task_id": item.task_id,
            "text": item.text,
            "first_planned": day.isoformat(),
            "is_p0": True,
            "source_pointer": item.source_pointer,
        }
        for item in p0
    ]
    return MorningPlan(
        day=day,
        p0_items=p0_payload,
        handoffs=[],
        sources=[linear_ref, calendar_ref],
        lesson=LessonBlock(link=lesson_link, topic=lesson_topic) if lesson_link else None,
    )
