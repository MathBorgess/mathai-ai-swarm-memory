"""Morning plan model (collection output / --plan replay input)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from swarm_reports.metrics.ids import stable_task_id

MAX_TEXT = 500
MAX_HANDOFF = 20
MAX_P0 = 3
SAFE_URL_SCHEMES = frozenset({"https", "http"})


@dataclass
class SourceRef:
    kind: str
    pointer: str
    status: str = "ok"

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "pointer": self.pointer, "status": self.status}


@dataclass
class HandoffCard:
    task_id: str
    title: str
    objective: str
    context_links: list[str] = field(default_factory=list)
    copy_prompt: str = ""
    criteria: str = ""
    time_budget_minutes: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "objective": self.objective,
            "context_links": list(self.context_links),
            "copy_prompt": self.copy_prompt,
            "criteria": self.criteria,
            "time_budget_minutes": self.time_budget_minutes,
        }


@dataclass
class ReviewDraft:
    draft_id: str
    kind: str
    reason: str
    content: str
    status: str = "pending"

    def to_json(self) -> dict[str, str]:
        return {
            "draft_id": self.draft_id,
            "kind": self.kind,
            "reason": self.reason,
            "content": self.content,
            "status": self.status,
        }


@dataclass
class LessonBlock:
    link: str | None
    topic: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {"link": self.link, "topic": self.topic}


@dataclass
class MorningPlan:
    day: date
    p0_items: list[dict[str, Any]]
    handoffs: list[HandoffCard]
    sources: list[SourceRef]
    review_drafts: list[ReviewDraft] = field(default_factory=list)
    lesson: LessonBlock | None = None
    optional_post_draft: ReviewDraft | None = None
    ledger_placeholder: str = ""
    discovery_placeholder: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "p0_items": self.p0_items,
            "handoffs": [h.to_json() for h in self.handoffs],
            "sources": [s.to_json() for s in self.sources],
            "review_drafts": [d.to_json() for d in self.review_drafts],
            "lesson": self.lesson.to_json() if self.lesson else None,
            "optional_post_draft": (
                self.optional_post_draft.to_json() if self.optional_post_draft else None
            ),
            "ledger_placeholder": self.ledger_placeholder,
            "discovery_placeholder": self.discovery_placeholder,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MorningPlan:
        day = date.fromisoformat(str(data["day"]))
        p0_raw = data.get("p0_items") or []
        if not isinstance(p0_raw, list):
            raise ValueError("p0_items must be a list")
        p0_items = [_validate_p0_item(entry, day) for entry in p0_raw]
        if len(p0_items) > MAX_P0:
            raise ValueError(f"at most {MAX_P0} P0 items")
        handoffs = [
            _handoff_from_json(entry)
            for entry in (data.get("handoffs") or [])
            if isinstance(entry, dict)
        ]
        sources = [
            SourceRef(
                kind=str(entry.get("kind") or ""),
                pointer=str(entry.get("pointer") or ""),
                status=str(entry.get("status") or "ok"),
            )
            for entry in (data.get("sources") or [])
            if isinstance(entry, dict)
        ]
        reviews = [
            _review_from_json(entry)
            for entry in (data.get("review_drafts") or [])
            if isinstance(entry, dict)
        ]
        lesson_raw = data.get("lesson")
        lesson = None
        if isinstance(lesson_raw, dict):
            link = lesson_raw.get("link")
            if link is not None:
                link = validate_safe_url(str(link))
            lesson = LessonBlock(link=link, topic=_bounded_str(lesson_raw.get("topic"), 200))
        post_raw = data.get("optional_post_draft")
        optional_post = _review_from_json(post_raw) if isinstance(post_raw, dict) else None
        return cls(
            day=day,
            p0_items=p0_items,
            handoffs=handoffs,
            sources=sources,
            review_drafts=reviews,
            lesson=lesson,
            optional_post_draft=optional_post,
            ledger_placeholder=_bounded_str(data.get("ledger_placeholder"), 2000),
            discovery_placeholder=_bounded_str(data.get("discovery_placeholder"), 2000),
        )


def load_plan_file(path: Path) -> MorningPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("plan file must be a JSON object")
    return MorningPlan.from_json(data)


def _bounded_str(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError("string field exceeds max length")
    return text


def _validate_p0_item(entry: Any, day: date) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("p0 item must be an object")
    text = _bounded_str(entry.get("text"), MAX_TEXT)
    task_id = entry.get("task_id")
    if task_id is not None:
        task_id = _bounded_str(task_id, 128)
    else:
        pointer = entry.get("source_pointer")
        if pointer:
            task_id = stable_task_id(f"{pointer}:{text}", [])
        else:
            task_id = stable_task_id(text, [])
    first_planned = entry.get("first_planned") or day.isoformat()
    return {
        "task_id": task_id,
        "text": text,
        "first_planned": str(first_planned),
        "is_p0": True,
        "source_pointer": _bounded_str(entry.get("source_pointer"), 256),
    }


def _handoff_from_json(entry: dict[str, Any]) -> HandoffCard:
    links_raw = entry.get("context_links") or []
    links = [validate_safe_url(str(x)) for x in links_raw if x][:10]
    minutes = entry.get("time_budget_minutes")
    if minutes is not None:
        minutes = int(minutes)
        if minutes < 0 or minutes > 24 * 60:
            raise ValueError("time_budget_minutes out of range")
    return HandoffCard(
        task_id=_bounded_str(entry.get("task_id"), 128),
        title=_bounded_str(entry.get("title"), 200),
        objective=_bounded_str(entry.get("objective"), MAX_TEXT),
        context_links=links,
        copy_prompt=_bounded_str(entry.get("copy_prompt"), 4000),
        criteria=_bounded_str(entry.get("criteria"), 1000),
        time_budget_minutes=minutes,
    )


def _review_from_json(entry: dict[str, Any]) -> ReviewDraft:
    status = str(entry.get("status") or "pending").lower()
    if status not in {"pending", "ready", "rejected"}:
        raise ValueError("invalid review draft status")
    return ReviewDraft(
        draft_id=_bounded_str(entry.get("draft_id"), 64),
        kind=_bounded_str(entry.get("kind"), 32),
        reason=_bounded_str(entry.get("reason"), 500),
        content=_bounded_str(entry.get("content"), 8000),
        status=status,
    )


def validate_safe_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in SAFE_URL_SCHEMES or not parsed.netloc:
        raise ValueError("url must be http(s) with host")
    if re.search(r"[\x00-\x1f]", url):
        raise ValueError("url contains control characters")
    return url.strip()

