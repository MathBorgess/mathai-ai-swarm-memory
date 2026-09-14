"""Planner provider subprocess — a JSON contract, not a provider integration.

What this actually supports is one thing: an adapter you write, which reads the request
JSON on stdin and writes a `MorningPlan` JSON on stdout. That is the only `kind` this
module accepts, and configuring anything else fails loudly.

It is **not** an integration with `claude`, `codex` or `cursor-agent`. Those CLIs emit
prose on stdout, so pointing `planner_provider.command` at one of them produces
"planner provider returned invalid JSON". Parsing real provider output, routing between
them and honouring quota is F5's job (`skills/handoff` routing table). Until then the
supported morning paths are `--plan` with an adapter-produced file, or explicit
`source-unavailable` markers.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from swarm_reports.config import PlannerProviderConfig
from swarm_reports.plan import MorningPlan

_SECRET = re.compile(
    r"(?i)\b(bearer|token|api[_-]?key|secret|password|authorization)\b\s*[:=]?\s*\S+"
)


@dataclass(frozen=True)
class ProviderResult:
    plan: MorningPlan
    #: The parsed-but-unvalidated payload, checkpointed before any wiki effect.
    raw: dict[str, Any]
    stderr_redacted: str


def run_planner_provider(
    config: PlannerProviderConfig,
    *,
    day: date,
    wiki_dir: Path,
    input_payload: dict[str, Any] | None = None,
) -> ProviderResult:
    payload = {
        "kind": config.kind,
        "day": day.isoformat(),
        "wiki_dir": str(wiki_dir),
        **(input_payload or {}),
    }
    try:
        proc = subprocess.run(
            list(config.command),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"planner provider timed out after {config.timeout_seconds}s"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"planner provider failed ({proc.returncode}): {_redact(proc.stderr)[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "planner provider returned invalid JSON. This seam accepts only a "
            "JSON-on-stdout adapter; wrapping a text-emitting provider CLI is F5 work."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("planner provider JSON root must be an object")
    plan = MorningPlan.from_json(data)
    if plan.day != day:
        raise RuntimeError("planner provider day mismatch")
    return ProviderResult(plan=plan, raw=data, stderr_redacted=_redact(proc.stderr))


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1=[redacted]", text or "")
