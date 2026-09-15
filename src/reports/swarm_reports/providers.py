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
    from swarm_reports.dispatch.providers import SubprocessRunner, provider_argv, provider_json
    native = config.kind != "json-stdio"
    argv = provider_argv(config.kind, config.command, read_only=True) if native else list(config.command)
    instruction = (
        "Execute skills/daily-plan from the swarm reports mechanism. Read the wiki orientation, "
        "Linear and Calendar using installed read-only tools. Unavailable sources must be marked "
        "unavailable; do not invent IDs. Do not write, publish or merge. Return ONLY MorningPlan "
        "JSON with day, checklist [{task_id,text,is_p0,first_planned}], sources "
        "[{kind,pointer,status}], agenda, handoffs [{task_id,title,objective,copy_prompt}], "
        "review_drafts, ledger, lesson and confirmed_empty. Use teach-me for study and post-voice "
        "for proposed public drafts. Request: "
    )
    proc = SubprocessRunner(cwd=wiki_dir).run(
        argv, stdin=(instruction if native else "") + json.dumps(payload), timeout=config.timeout_seconds)
    if proc.timed_out or proc.returncode != 0:
        raise RuntimeError("planner provider failed or timed out; diagnostic omitted")
    try:
        data = provider_json(proc.stdout) if native else json.loads(proc.stdout)
    except (ValueError, KeyError) as exc:
        raise RuntimeError("planner provider returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("planner provider JSON root must be an object")
    plan = MorningPlan.from_json(data)
    if plan.day != day:
        raise RuntimeError("planner provider day mismatch")
    return ProviderResult(plan=plan, raw=data, stderr_redacted=_redact(proc.stderr))


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1=[redacted]", text or "")
