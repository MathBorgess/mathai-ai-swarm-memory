"""Parse post notes (`brand/posts/*.md`) YAML frontmatter."""

from __future__ import annotations

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


@dataclass
class PostNote:
    guided: bool | None
    url: str | None
    platform: str
    checkpoints: list[PostCheckpoint] = field(default_factory=list)
    metrics: PostMetrics | None = None
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def _parse_metrics_blob(blob: Any) -> PostMetrics | None:
    if not isinstance(blob, dict):
        return None
    return PostMetrics.from_mapping(blob)


def parse_post_markdown(text: str) -> PostNote:
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

    return PostNote(
        guided=guided,
        url=url,
        platform=platform,
        checkpoints=checkpoints,
        metrics=top_metrics,
        raw_frontmatter=frontmatter,
    )
