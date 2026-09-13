#!/usr/bin/env python3
"""Verify the pinned Hermes AIAgent install. No paid model call."""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
from pathlib import Path


def _pin_file() -> Path:
    return Path(__file__).resolve().parent / "HERMES_PIN"


def load_pin(path: Path | None = None) -> dict[str, str]:
    pin: dict[str, str] = {}
    for line in (path or _pin_file()).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            pin[key.strip()] = value.strip()
    for required in ("commit", "repo", "run_agent_sha256", "agent_init_sha256"):
        if not pin.get(required):
            raise SystemExit(f"HERMES_PIN missing {required}")
    return pin


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def hermes_src() -> Path:
    env = os.environ.get("HERMES_ASK_SRC", "").strip()
    if env:
        return Path(env)
    return Path("/opt/hermes-agent")


def main() -> None:
    pin = load_pin()
    src = hermes_src()
    run_agent = src / "run_agent.py"
    agent_init = src / "agent" / "agent_init.py"
    if not run_agent.is_file() or not agent_init.is_file():
        raise SystemExit(f"Hermes sources missing under {src}")
    got_run = sha256_file(run_agent)
    got_init = sha256_file(agent_init)
    if got_run != pin["run_agent_sha256"]:
        raise SystemExit(f"run_agent.py sha256 {got_run} != pin")
    if got_init != pin["agent_init_sha256"]:
        raise SystemExit(f"agent/agent_init.py sha256 {got_init} != pin")
    git_dir = src / ".git"
    if git_dir.exists():
        import subprocess

        head = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
        if head != pin["commit"]:
            raise SystemExit(f"git HEAD {head} != pin {pin['commit']}")
    sys.path.insert(0, str(src))
    from run_agent import AIAgent

    init_sig = inspect.signature(AIAgent.__init__)
    for name in ("enabled_toolsets", "skip_memory", "skip_context_files", "api_key", "base_url", "model"):
        if name not in init_sig.parameters:
            raise SystemExit(f"AIAgent.__init__ missing {name}")
    chat_sig = inspect.signature(AIAgent.chat)
    if chat_sig.return_annotation is not str:
        raise SystemExit(f"AIAgent.chat return annotation is {chat_sig.return_annotation!r}, not str")
    print(f"hermes pin {pin['commit']} signatures ok")


if __name__ == "__main__":
    main()
