"""YAML frontmatter delimiter parsing (exact `---` line boundaries)."""

from __future__ import annotations

from typing import Any

import yaml


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_idx: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_idx = index
            break
    if end_idx is None:
        return {}, text
    raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        data = {}
    return data, body
