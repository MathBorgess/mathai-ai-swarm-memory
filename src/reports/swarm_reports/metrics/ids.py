"""Stable task identifiers for checklist carryover."""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAT_PATTERN = re.compile(r"\bMAT-\d+\b", re.IGNORECASE)
_SWARM_META = re.compile(
    r"<!--\s*swarm:task-meta\s+(?:\w+=[^\s]+(?:\s+|$))+\s*-->",
    re.IGNORECASE,
)
_SWARM_CLASS = re.compile(r"<!--\s*swarm:class\s+\w+\s*-->", re.IGNORECASE)
_SWARM_P0 = re.compile(r"<!--\s*swarm:p0\s*-->", re.IGNORECASE)


def normalize_task_text(text: str) -> str:
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    stripped = _SWARM_META.sub("", stripped)
    stripped = _SWARM_CLASS.sub("", stripped)
    stripped = _SWARM_P0.sub("", stripped)
    stripped = re.sub(r"\bP0\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\bP-0\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\*\*", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped.strip().lower())
    return stripped


def hash_task_text(text: str) -> str:
    digest = hashlib.sha256(normalize_task_text(text).encode("utf-8")).hexdigest()[:16]
    return f"hash:{digest}"


def extract_mat_id(text: str) -> str | None:
    match = MAT_PATTERN.search(text)
    if not match:
        return None
    return match.group(0).upper()


def _calendar_title_matches(text: str, title: str) -> bool:
    if title in text:
        return True
    for part in re.split(r"[—–\-]", title):
        fragment = part.strip()
        if len(fragment) >= 8 and fragment in text:
            return True
    return False


def extract_event_id(text: str, calendar_entries: list[dict[str, Any]] | None) -> str | None:
    if not calendar_entries:
        return None
    for entry in calendar_entries:
        title = str(entry.get("title") or "")
        event_id = entry.get("event_id")
        if not title or not event_id:
            continue
        if _calendar_title_matches(text, title):
            return f"event:{event_id}"
    return None


def stable_task_id(text: str, calendar_entries: list[dict[str, Any]] | None = None) -> str:
    mat = extract_mat_id(text)
    if mat:
        return mat
    event = extract_event_id(text, calendar_entries)
    if event:
        return event
    return hash_task_text(text)
