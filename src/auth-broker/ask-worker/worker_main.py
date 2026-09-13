#!/usr/bin/env python3
"""Dedicated ask worker: Hermes AIAgent with tools/memory off, isolated HERMES_HOME."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Constructor flags must stay in lockstep with app.ask.isolation.ISOLATED_AGENT_KWARGS.
# enabled_toolsets=[] is the Hermes allowlist (empty means no tools, including memory).
# skip_memory=True skips provider init; skip_context_files=True skips host AGENTS.md/SOUL cwd.


def _confined(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base:
            return True
        if base.is_file():
            continue
        try:
            resolved.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def _inspection(job_path: Path) -> dict:
    hermes_home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    home = Path(os.environ.get("HOME", "")).resolve()
    roots = [hermes_home, Path("/opt/ask-worker"), Path("/isolated"), job_path]
    sentinel = os.environ.get("ASK_OWNER_SENTINEL", "")
    readable = False
    if sentinel:
        target = Path(sentinel)
        if _confined(target, roots) and target.is_file():
            readable = True
    return {
        "owner_sentinel_readable": readable,
        "home": str(home),
        "hermes_home": str(hermes_home),
        "broker_token_present": bool(os.environ.get("HERMES_BROKER_TOKEN")),
    }


def _assert_isolated(job_path: Path) -> None:
    if os.environ.get("HERMES_BROKER_TOKEN"):
        raise SystemExit("fail closed: broker token visible")
    hermes_home = os.environ.get("HERMES_HOME", "")
    home = os.environ.get("HOME", "")
    if not hermes_home or not Path(hermes_home).is_absolute():
        raise SystemExit("fail closed: HERMES_HOME")
    expected_home = str(Path(hermes_home) / "home")
    if home != expected_home:
        raise SystemExit("fail closed: HOME must be the isolated profile home")
    if os.environ.get("HERMES_REAL_HOME"):
        raise SystemExit("fail closed: HERMES_REAL_HOME leaks owner home")
    if not job_path.is_file():
        raise SystemExit("fail closed: job missing")


def _prompt(job: dict) -> str:
    style = Path("/opt/ask-worker/style/SOUL.md")
    if not style.is_file():
        style = Path(os.environ["HERMES_HOME"]) / "SOUL.md"
    voice = style.read_text(encoding="utf-8") if style.is_file() else ""
    lines = [voice.strip(), "", "Authorized excerpts for this turn only:"]
    for item in job.get("envelope") or []:
        lines.append(f"- handle={item['handle']} revision={item.get('source_revision', '')}")
        lines.append(item.get("text", ""))
    lines.append("Return prose. Cite only the handles listed above.")
    return "\n".join(lines)


def _user_message(job: dict) -> str:
    prior = job.get("prior_user_turns") or []
    parts = []
    for turn in prior:
        if isinstance(turn, str) and turn.strip():
            parts.append(f"Previous question: {turn.strip()}")
    parts.append(job["query"])
    return "\n".join(parts)


def _generate(job: dict) -> dict:
    if os.environ.get("HERMES_ASK_STUB") == "1":
        cited = [item["handle"] for item in job.get("envelope") or [] if "handle" in item]
        return {"text": "stub", "cited_handles": cited}
    from run_agent import AIAgent

    agent = AIAgent(
        model=os.environ.get("ASK_MODEL", ""),
        enabled_toolsets=[],
        skip_memory=True,
        skip_context_files=True,
        load_soul_identity=False,
        quiet_mode=True,
        max_iterations=1,
        save_trajectories=False,
        ephemeral_system_prompt=_prompt(job),
    )
    text = agent.chat(_user_message(job))
    cited = [item["handle"] for item in job.get("envelope") or []]
    limit = int((job.get("budget") or {}).get("max_output_chars") or 8000)
    return {"text": str(text)[:limit], "cited_handles": cited}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: worker_main.py /job.json")
    job_path = Path(sys.argv[1])
    _assert_isolated(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or not job.get("envelope"):
        raise SystemExit("fail closed: empty envelope must not reach the worker")
    result = _generate(job)
    if os.environ.get("HERMES_ASK_STUB") == "1":
        result["inspection"] = _inspection(job_path)
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
