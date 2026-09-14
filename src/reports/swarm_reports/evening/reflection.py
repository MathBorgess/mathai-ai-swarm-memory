"""Optional qualitative pass over a day that has already been measured.

The split is the design's, not a preference: **metrics are deterministic code with
tests, never a number a model produced.** So this adapter runs after every metric is
computed, receives them as input, and may only return prose and suggestions. Nothing it
returns is a path that gets opened, a command that gets run, or a number that reaches a
tile.

The contract is the same `json-stdio` seam the planner uses: request JSON on stdin,
response JSON on stdout, hard timeout, no shell.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from swarm_reports.evening.config import ReflectionConfig

MAX_TEXT = 4000
MAX_SUGGESTIONS = 5
MAX_FIELD = 500

#: Where a suggestion is allowed to land. `skill` and `weights` become draft pull
#: requests for the owner; `process` is a text note. None of them is applied here.
SUGGESTION_KINDS = frozenset({"skill", "weights", "process"})


@dataclass(frozen=True)
class Suggestion:
    kind: str
    #: A label for the owner, e.g. `skills/daily-review/SKILL.md`. Never resolved to a
    #: filesystem path and never opened by this process.
    target: str
    summary: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "target": self.target,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Reflection:
    text: str = ""
    suggestions: tuple[Suggestion, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.text and not self.suggestions


def run_reflection(config: ReflectionConfig, request: dict[str, Any]) -> Reflection:
    """Invoke the adapter. A broken adapter yields an empty reflection, never a failure:
    the day's metrics are already durable and must not be lost to a flaky subprocess."""
    try:
        proc = subprocess.run(
            list(config.command),
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Reflection()
    if proc.returncode != 0:
        return Reflection()
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return Reflection()
    try:
        return parse_reflection(data)
    except ValueError:
        return Reflection()


def parse_reflection(data: Any) -> Reflection:
    if not isinstance(data, dict):
        raise ValueError("reflection must be a JSON object")
    extra = sorted(set(data) - {"text", "suggestions"})
    if extra:
        raise ValueError(f"reflection has unknown keys: {', '.join(extra)}")

    text_raw = data.get("text") or ""
    if not isinstance(text_raw, str):
        raise ValueError("reflection.text must be a string")
    text = _clean(text_raw, MAX_TEXT, "reflection.text")

    raw = data.get("suggestions") or []
    if not isinstance(raw, list):
        raise ValueError("reflection.suggestions must be a list")
    if len(raw) > MAX_SUGGESTIONS:
        raise ValueError(f"reflection.suggestions exceeds {MAX_SUGGESTIONS} entries")

    suggestions: list[Suggestion] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("reflection.suggestions entries must be objects")
        extra = sorted(set(entry) - {"kind", "target", "summary", "detail"})
        if extra:
            raise ValueError(f"suggestion has unknown keys: {', '.join(extra)}")
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in SUGGESTION_KINDS:
            raise ValueError(f"suggestion.kind must be one of {sorted(SUGGESTION_KINDS)}")
        summary = _clean(str(entry.get("summary") or ""), MAX_FIELD, "suggestion.summary")
        if not summary:
            raise ValueError("suggestion.summary must not be blank")
        suggestions.append(
            Suggestion(
                kind=kind,
                target=_clean(str(entry.get("target") or ""), MAX_FIELD, "suggestion.target"),
                summary=summary,
                detail=_clean(str(entry.get("detail") or ""), MAX_TEXT, "suggestion.detail"),
            )
        )
    return Reflection(text=text, suggestions=tuple(suggestions))


def _clean(text: str, limit: int, label: str) -> str:
    stripped = text.strip()
    if len(stripped) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in stripped):
        raise ValueError(f"{label} contains control characters")
    return stripped
