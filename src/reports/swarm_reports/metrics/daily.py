"""Parse wiki daily notes (`daily/YYYY-MM-DD.md`)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from swarm_reports.metrics.frontmatter import split_frontmatter
from swarm_reports.metrics.ids import stable_task_id

CHECKLIST_LINE = re.compile(
    r"^(\s*)- \[([ xX])\]\s+(?P<body>.+?)\s*$"
)
META_COMMENT = re.compile(
    r"<!--\s*swarm:task-meta\s+"
    r"(?P<kv>(?:\w+=[^\s]+(?:\s+|$))+)\s*-->",
    re.IGNORECASE,
)
CLASS_COMMENT = re.compile(
    r"<!--\s*swarm:class\s+(?P<class>\w+)\s*-->",
    re.IGNORECASE,
)
P0_COMMENT = re.compile(r"<!--\s*swarm:p0\s*-->", re.IGNORECASE)
FROZEN_BEGIN = re.compile(
    r"<!--\s*swarm:frozen-checklist-begin(?:\s+(?P<attrs>[^>]*))?\s*-->",
    re.IGNORECASE,
)
FROZEN_END = re.compile(r"<!--\s*swarm:frozen-checklist-end\s*-->", re.IGNORECASE)
ADDED_AFTER = re.compile(r"<!--\s*swarm:added-after-freeze\s*-->", re.IGNORECASE)


def _parse_attr_blob(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in blob.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _parse_meta_comment(line: str) -> dict[str, str]:
    match = META_COMMENT.search(line)
    if not match:
        return {}
    return _parse_attr_blob(match.group("kv"))


def _parse_classification(line: str) -> str | None:
    match = CLASS_COMMENT.search(line)
    if not match:
        return None
    return match.group("class").lower()


def _is_p0_line(line: str, body: str) -> bool:
    if P0_COMMENT.search(line):
        return True
    upper = body.upper()
    if re.search(r"\bP0\b", upper):
        return True
    if re.search(r"\bP-0\b", upper):
        return True
    return False


def _strip_inline_swarm_comments(text: str) -> str:
    text = META_COMMENT.sub("", text)
    text = CLASS_COMMENT.sub("", text)
    text = P0_COMMENT.sub("", text)
    return text.strip()


def _extract_hoje_section(body: str) -> str:
    match = re.search(r"^##\s+Hoje\s*$", body, flags=re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    start = match.end()
    rest = body[start:]
    next_heading = re.search(r"^##\s+", rest, flags=re.MULTILINE)
    if next_heading:
        return rest[: next_heading.start()]
    return rest


@dataclass(frozen=True)
class ChecklistItem:
    task_id: str
    text: str
    done: bool
    frozen: bool
    added_after_freeze: bool
    is_p0: bool
    classification: str | None
    first_planned: date | None
    line_number: int


@dataclass
class DailyNote:
    day: date
    frontmatter: dict[str, Any]
    items: list[ChecklistItem] = field(default_factory=list)
    frozen_snapshot_commit: str | None = None
    frozen_snapshot_at: str | None = None
    evening_validated: bool = False
    evening_absent: bool = False


def parse_daily_markdown(text: str, day: date | None = None) -> DailyNote:
    frontmatter, body = split_frontmatter(text)
    calendar_entries: list[dict[str, Any]] = []
    external = frontmatter.get("external") or {}
    if isinstance(external, dict):
        cal = external.get("calendar")
        if isinstance(cal, list):
            calendar_entries = [e for e in cal if isinstance(e, dict)]

    if day is None:
        title_match = re.search(r"^#\s+(\d{4}-\d{2}-\d{2})\s*$", body, re.MULTILINE)
        if title_match:
            day = date.fromisoformat(title_match.group(1))
        else:
            raise ValueError("daily note day is required when title is missing")

    hoje = _extract_hoje_section(body)
    lines = hoje.splitlines()

    frozen_region = False
    snapshot_commit: str | None = None
    snapshot_at: str | None = None
    pending_meta: dict[str, str] = {}
    next_is_added = False
    items: list[ChecklistItem] = []

    for index, line in enumerate(lines, start=1):
        if FROZEN_BEGIN.search(line):
            frozen_region = True
            attrs = FROZEN_BEGIN.search(line)
            if attrs and attrs.group("attrs"):
                parsed = _parse_attr_blob(attrs.group("attrs"))
                snapshot_commit = parsed.get("commit") or snapshot_commit
                snapshot_at = parsed.get("snapshot") or snapshot_at
            continue
        if FROZEN_END.search(line):
            frozen_region = False
            continue
        if ADDED_AFTER.search(line):
            next_is_added = True
            continue

        match = CHECKLIST_LINE.match(line)
        meta = _parse_meta_comment(line)
        if meta and match is None:
            pending_meta = meta
            continue

        if not match:
            continue

        body_text = match.group("body")
        done = match.group(2).lower() == "x"
        classification = _parse_classification(line) or _parse_classification(body_text)
        is_p0 = _is_p0_line(line, body_text)

        meta = {**pending_meta, **_parse_meta_comment(line), **_parse_meta_comment(body_text)}
        pending_meta = {}

        id_source = _strip_inline_swarm_comments(body_text)
        task_id = meta.get("id") or stable_task_id(id_source, calendar_entries)
        first_planned_raw = meta.get("first_planned") or meta.get("first-planned")
        # Explicit contract: missing first_planned in meta defaults to the note calendar day.
        first_planned = date.fromisoformat(first_planned_raw) if first_planned_raw else day

        added_after = next_is_added or meta.get("added_after_freeze") == "true"
        next_is_added = False

        frozen_flag = meta.get("frozen") == "true" or frozen_region
        if meta.get("frozen") == "false":
            frozen_flag = False

        display_text = _strip_inline_swarm_comments(body_text)
        items.append(
            ChecklistItem(
                task_id=task_id,
                text=display_text,
                done=done,
                frozen=frozen_flag,
                added_after_freeze=added_after,
                is_p0=is_p0,
                classification=classification,
                first_planned=first_planned,
                line_number=index,
            )
        )

    evening_validated = bool(frontmatter.get("swarm_evening_validated"))
    evening_absent = bool(frontmatter.get("swarm_evening_absent"))

    return DailyNote(
        day=day,
        frontmatter=frontmatter,
        items=items,
        frozen_snapshot_commit=snapshot_commit,
        frozen_snapshot_at=snapshot_at,
        evening_validated=evening_validated,
        evening_absent=evening_absent,
    )
