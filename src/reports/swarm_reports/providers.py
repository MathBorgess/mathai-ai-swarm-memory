"""External planner provider subprocess (argv JSON in/out)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from swarm_reports.config import PlannerProviderConfig
from swarm_reports.plan import MorningPlan


@dataclass(frozen=True)
class ProviderResult:
    plan: MorningPlan
    stderr_redacted: str


def run_planner_provider(
    config: PlannerProviderConfig,
    *,
    day: date,
    wiki_dir: Path,
    input_payload: dict[str, Any] | None = None,
) -> ProviderResult:
    payload = {
        "day": day.isoformat(),
        "wiki_dir": str(wiki_dir),
        **(input_payload or {}),
    }
    proc = subprocess.run(
        list(config.command),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"planner provider failed ({proc.returncode}): {_redact(proc.stderr)[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("planner provider returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("planner provider JSON root must be object")
    plan = MorningPlan.from_json(data)
    if plan.day != day:
        raise RuntimeError("planner provider day mismatch")
    return ProviderResult(plan=plan, stderr_redacted=_redact(proc.stderr))


def _redact(text: str) -> str:
    import re

    return re.sub(
        r"(?i)(bearer|token|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text or "",
    )
