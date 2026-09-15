"""Morning plan model (collection output / --plan replay input).

The plan carries the **full** `## Hoje` checklist. `p0_items` is a derived subset
(at most `MAX_P0`), never the whole checklist: freezing only the P0 subset would
silently drop the ordinary items the owner planned for the day.
"""

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
MAX_CHECKLIST = 64
MAX_AGENDA = 40
MAX_SOURCES = 16
MAX_REVIEWS = 32
SAFE_URL_SCHEMES = frozenset({"https", "http"})

#: `ok` means the adapter really reached the source and the result is trustworthy,
#: including a trustworthy *empty* result. Anything else must not freeze an empty day.
SOURCE_STATUSES = frozenset({"ok", "unavailable", "error"})


@dataclass
class SourceRef:
    kind: str
    pointer: str
    status: str = "ok"

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"source status must be one of {sorted(SOURCE_STATUSES)}")

    @property
    def confirms_empty(self) -> bool:
        """Only a successful read can confirm that the source genuinely had nothing."""
        return self.status == "ok"

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "pointer": self.pointer, "status": self.status}


@dataclass
class AgendaEntry:
    """Calendar row for the Hoje tab. Refreshed on every rerun, never frozen."""

    title: str
    when: str = ""
    source_pointer: str = ""

    def to_json(self) -> dict[str, str]:
        return {"title": self.title, "when": self.when, "source_pointer": self.source_pointer}


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
class LedgerEntry:
    """Autonomous action already taken; classified in the evening form."""

    entry_id: str
    kind: str
    summary: str
    link: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "summary": self.summary,
            "link": self.link,
        }


@dataclass
class MorningPlan:
    day: date
    checklist: list[dict[str, Any]] = field(default_factory=list)
    handoffs: list[HandoffCard] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    agenda: list[AgendaEntry] = field(default_factory=list)
    review_drafts: list[ReviewDraft] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    lesson: LessonBlock | None = None
    optional_post_draft: ReviewDraft | None = None
    discovery_placeholder: str = ""
    #: Set only when every source returned `ok` and genuinely had nothing to plan.
    confirmed_empty: bool = False

    @property
    def p0_items(self) -> list[dict[str, Any]]:
        return [item for item in self.checklist if item.get("is_p0")]

    @property
    def source_failures(self) -> list[SourceRef]:
        return [ref for ref in self.sources if not ref.confirms_empty]

    def freeze_blocker(self) -> str | None:
        """Why this plan must not be frozen, or None when freezing is safe.

        Freezing is irreversible for the day, so an empty checklist is only
        acceptable when the emptiness itself was confirmed.
        """
        if self.checklist:
            return None
        if self.confirmed_empty and not self.source_failures:
            return None
        failed = ", ".join(sorted(f"{ref.kind}:{ref.status}" for ref in self.source_failures))
        if failed:
            return f"empty checklist with unreadable sources ({failed})"
        return "empty checklist that was not explicitly confirmed empty"

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "checklist": self.checklist,
            "handoffs": [h.to_json() for h in self.handoffs],
            "sources": [s.to_json() for s in self.sources],
            "agenda": [a.to_json() for a in self.agenda],
            "review_drafts": [d.to_json() for d in self.review_drafts],
            "ledger": [entry.to_json() for entry in self.ledger],
            "lesson": self.lesson.to_json() if self.lesson else None,
            "optional_post_draft": (
                self.optional_post_draft.to_json() if self.optional_post_draft else None
            ),
            "discovery_placeholder": self.discovery_placeholder,
            "confirmed_empty": self.confirmed_empty,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MorningPlan:
        if not isinstance(data, dict):
            raise ValueError("plan must be a JSON object")
        day = date.fromisoformat(_require_str(data, "day"))

        checklist = _validate_checklist(data.get("checklist"), data.get("p0_items"), day)
        handoffs = _validate_list(
            data.get("handoffs"), MAX_HANDOFF, "handoffs", lambda e: _handoff_from_json(e)
        )
        sources = _validate_list(
            data.get("sources"), MAX_SOURCES, "sources", _source_from_json
        )
        agenda = _validate_list(data.get("agenda"), MAX_AGENDA, "agenda", _agenda_from_json)
        reviews = _validate_list(
            data.get("review_drafts"), MAX_REVIEWS, "review_drafts", _review_from_json
        )
        ledger = _validate_list(data.get("ledger"), MAX_REVIEWS, "ledger", _ledger_from_json)

        lesson_raw = data.get("lesson")
        lesson = None
        if lesson_raw is not None:
            if not isinstance(lesson_raw, dict):
                raise ValueError("lesson must be an object")
            link = lesson_raw.get("link")
            if link is not None:
                link = validate_lesson_link(str(link))
            lesson = LessonBlock(link=link, topic=_bounded_str(lesson_raw.get("topic"), 200))

        post_raw = data.get("optional_post_draft")
        if post_raw is not None and not isinstance(post_raw, dict):
            raise ValueError("optional_post_draft must be an object")
        optional_post = _review_from_json(post_raw) if isinstance(post_raw, dict) else None

        confirmed_empty = data.get("confirmed_empty", False)
        if not isinstance(confirmed_empty, bool):
            raise ValueError("confirmed_empty must be a boolean")

        return cls(
            day=day,
            checklist=checklist,
            handoffs=handoffs,
            sources=sources,
            agenda=agenda,
            review_drafts=reviews,
            ledger=ledger,
            lesson=lesson,
            optional_post_draft=optional_post,
            discovery_placeholder=_bounded_str(data.get("discovery_placeholder"), 2000),
            confirmed_empty=confirmed_empty,
        )


def load_plan_file(path: Path) -> MorningPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("plan file must be a JSON object")
    return MorningPlan.from_json(data)


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _bounded_str(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("string field has wrong type")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError("string field exceeds max length")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise ValueError("string field contains control characters")
    return text


def _validate_list(raw: Any, limit: int, label: str, build):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds {limit} entries")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{label} entries must be objects")
        out.append(build(entry))
    return out


def _validate_checklist(
    checklist_raw: Any,
    legacy_p0_raw: Any,
    day: date,
) -> list[dict[str, Any]]:
    """Build the full checklist, merging the legacy `p0_items`-only plan shape."""
    entries: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    def add(entry: Any, force_p0: bool) -> None:
        item = _validate_checklist_item(entry, day, force_p0=force_p0)
        existing = by_id.get(item["task_id"])
        if existing is not None:
            if item["is_p0"]:
                existing["is_p0"] = True
            return
        by_id[item["task_id"]] = item
        entries.append(item)

    if checklist_raw is not None:
        if not isinstance(checklist_raw, list):
            raise ValueError("checklist must be a list")
        for entry in checklist_raw:
            add(entry, force_p0=False)
    if legacy_p0_raw is not None:
        if not isinstance(legacy_p0_raw, list):
            raise ValueError("p0_items must be a list")
        for entry in legacy_p0_raw:
            add(entry, force_p0=True)

    if len(entries) > MAX_CHECKLIST:
        raise ValueError(f"checklist exceeds {MAX_CHECKLIST} entries")
    p0_count = sum(1 for item in entries if item["is_p0"])
    if p0_count > MAX_P0:
        raise ValueError(f"at most {MAX_P0} P0 items")
    return entries


def _validate_checklist_item(entry: Any, day: date, *, force_p0: bool) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("checklist item must be an object")
    text = _bounded_str(entry.get("text"), MAX_TEXT)
    if not text:
        raise ValueError("checklist item text is required")
    task_id = entry.get("task_id")
    if task_id is not None:
        task_id = _bounded_str(task_id, 128)
        if not task_id:
            raise ValueError("checklist item task_id must not be blank")
    else:
        pointer = _bounded_str(entry.get("source_pointer"), 256)
        task_id = stable_task_id(f"{pointer}:{text}" if pointer else text, [])

    is_p0 = entry.get("is_p0", False)
    if not isinstance(is_p0, bool):
        raise ValueError("is_p0 must be a boolean")
    is_p0 = bool(is_p0) or force_p0

    first_planned_raw = entry.get("first_planned")
    if first_planned_raw is None:
        first_planned = day
    else:
        if not isinstance(first_planned_raw, str):
            raise ValueError("first_planned must be a YYYY-MM-DD string")
        first_planned = date.fromisoformat(first_planned_raw)
        if first_planned > day:
            raise ValueError("first_planned must not be in the future of the plan day")
    return {
        "task_id": task_id,
        "text": text,
        "first_planned": first_planned.isoformat(),
        "is_p0": is_p0,
        "source_pointer": _bounded_str(entry.get("source_pointer"), 256),
    }


def _source_from_json(entry: dict[str, Any]) -> SourceRef:
    return SourceRef(
        kind=_bounded_str(entry.get("kind"), 32),
        pointer=_bounded_str(entry.get("pointer"), 256),
        status=_bounded_str(entry.get("status"), 16) or "ok",
    )


def _agenda_from_json(entry: dict[str, Any]) -> AgendaEntry:
    return AgendaEntry(
        title=_bounded_str(entry.get("title"), 200),
        when=_bounded_str(entry.get("when"), 64),
        source_pointer=_bounded_str(entry.get("source_pointer"), 256),
    )


def _handoff_from_json(entry: dict[str, Any]) -> HandoffCard:
    links_raw = entry.get("context_links") or []
    if not isinstance(links_raw, list):
        raise ValueError("context_links must be a list")
    if len(links_raw) > 10:
        raise ValueError("context_links exceeds 10 entries")
    links = [validate_safe_url(str(x)) for x in links_raw if x]
    minutes = entry.get("time_budget_minutes")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ValueError("time_budget_minutes must be an integer")
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


def _ledger_from_json(entry: dict[str, Any]) -> LedgerEntry:
    link = entry.get("link")
    if link is not None:
        link = validate_safe_url(str(link))
    return LedgerEntry(
        entry_id=_bounded_str(entry.get("entry_id"), 128),
        kind=_bounded_str(entry.get("kind"), 32),
        summary=_bounded_str(entry.get("summary"), MAX_TEXT),
        link=link,
    )


#: Internal lesson links are same-origin paths under this prefix. Anything else would
#: let a planner adapter point the Lição tab at an attacker-chosen destination.
INTERNAL_LESSON_PREFIX = "/lessons/"
_INTERNAL_PATH = re.compile(r"^/lessons/[A-Za-z0-9._/-]{1,180}$")


def validate_internal_path(path: str) -> str:
    text = path.strip()
    if not _INTERNAL_PATH.match(text) or ".." in text or "//" in text:
        raise ValueError(f"internal lesson link must match {INTERNAL_LESSON_PREFIX}<safe-path>")
    return text


def validate_lesson_link(link: str) -> str:
    text = link.strip()
    if text.startswith("/"):
        return validate_internal_path(text)
    return validate_safe_url(text)


def validate_safe_url(url: str) -> str:
    text = url.strip()
    if re.search(r"[\x00-\x1f\x7f]", text):
        raise ValueError("url contains control characters")
    if len(text) > 2000:
        raise ValueError("url too long")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES or not parsed.netloc:
        raise ValueError("url must be http(s) with host")
    return text
