"""Parse post notes (`brand/posts/*.md`) YAML frontmatter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from swarm_reports.metrics.frontmatter import split_frontmatter
from swarm_reports.metrics.res import PostMetrics


@dataclass
class PostCheckpoint:
    label: str
    due: date | None = None
    metrics: PostMetrics | None = None


FILENAME_DATE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")


@dataclass
class PostNote:
    guided: bool | None
    url: str | None
    platform: str
    checkpoints: list[PostCheckpoint] = field(default_factory=list)
    metrics: PostMetrics | None = None
    posted_on: date | None = None
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def post_date_from_filename(name: str) -> date | None:
    """`brand/posts/` files are named `YYYY-MM-DD-slug.md`."""
    match = FILENAME_DATE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError:
        return None


def _parse_metrics_blob(blob: Any) -> PostMetrics | None:
    if not isinstance(blob, dict):
        return None
    return PostMetrics.from_mapping(blob)


def parse_post_markdown(text: str, *, filename: str | None = None) -> PostNote:
    frontmatter, _body = split_frontmatter(text)
    guided_raw = frontmatter.get("guided")
    guided: bool | None
    if guided_raw is None:
        guided = None
    else:
        guided = bool(guided_raw)

    platform = str(frontmatter.get("platform") or "linkedin").lower()
    url = frontmatter.get("url")
    if url is not None:
        url = str(url)

    checkpoints: list[PostCheckpoint] = []
    raw_checkpoints = frontmatter.get("checkpoints")
    if isinstance(raw_checkpoints, list):
        for entry in raw_checkpoints:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("at") or entry.get("label") or "checkpoint")
            due_raw = entry.get("due")
            due = date.fromisoformat(str(due_raw)) if due_raw else None
            metrics = _parse_metrics_blob(entry.get("metrics"))
            checkpoints.append(PostCheckpoint(label=label, due=due, metrics=metrics))

    top_metrics = _parse_metrics_blob(frontmatter.get("metrics"))

    posted_raw = frontmatter.get("posted_on") or frontmatter.get("date")
    posted_on: date | None = None
    if posted_raw is not None:
        posted_on = posted_raw if isinstance(posted_raw, date) else date.fromisoformat(str(posted_raw))
    elif filename:
        posted_on = post_date_from_filename(filename)

    return PostNote(
        guided=guided,
        url=url,
        platform=platform,
        checkpoints=checkpoints,
        metrics=top_metrics,
        posted_on=posted_on,
        raw_frontmatter=frontmatter,
    )
