"""Pinned Hermes wrapper: real AIAgent import, fake model transport, no paid call.

Ordinary pytest skips when the pin checkout is missing. It does not git-fetch or
pip-install into the user environment. To execute the wrapper tests: point
HERMES_ASK_SRC at a checkout of the HERMES_PIN commit, or set
HERMES_ASK_BOOTSTRAP=1 to run ask-worker/install_hermes.sh (network + pip).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ask.isolation import ISOLATED_AGENT_KWARGS, worker_root


def _pin() -> dict[str, str]:
    spec = worker_root() / "verify_hermes.py"
    import importlib.util

    loaded = importlib.util.spec_from_file_location("ask_verify_hermes", spec)
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module.load_pin()


def _hermes_src() -> Path:
    pin = _pin()
    env = os.environ.get("HERMES_ASK_SRC", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/tmp/hermes-agent-pin"))
    candidates.append(Path("/opt/hermes-agent"))
    for path in candidates:
        if (path / "run_agent.py").is_file():
            head = path / ".git"
            if head.exists():
                got = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
                if got != pin["commit"]:
                    continue
            return path
    if os.environ.get("HERMES_ASK_BOOTSTRAP", "").strip() in {"1", "true", "yes"}:
        dest = Path(env) if env else Path("/tmp/hermes-agent-pin")
        script = worker_root() / "install_hermes.sh"
        env_vars = {**os.environ, "HERMES_ASK_SRC": str(dest)}
        completed = subprocess.run(
            ["bash", str(script)], env=env_vars, capture_output=True, text=True, timeout=180
        )
        if completed.returncode != 0 or not (dest / "run_agent.py").is_file():
            pytest.skip(
                "hermes wrapper not executed: bootstrap failed "
                f"(exit {completed.returncode}: {completed.stderr[-300:]})"
            )
        return dest
    pytest.skip(
        "hermes wrapper not executed: pinned checkout missing. "
        "Set HERMES_ASK_SRC to a HERMES_PIN checkout, or HERMES_ASK_BOOTSTRAP=1 "
        "to run ask-worker/install_hermes.sh (git fetch + pip; not ordinary pytest)."
    )


def test_verify_hermes_pin_and_signatures():
    src = _hermes_src()
    pin = _pin()
    run_agent = src / "run_agent.py"
    agent_init = src / "agent" / "agent_init.py"
    assert hashlib.sha256(run_agent.read_bytes()).hexdigest() == pin["run_agent_sha256"]
    assert hashlib.sha256(agent_init.read_bytes()).hexdigest() == pin["agent_init_sha256"]
    completed = subprocess.run(
        [sys.executable, str(worker_root() / "verify_hermes.py")],
        env={**os.environ, "HERMES_ASK_SRC": str(src), "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert pin["commit"] in completed.stdout


def test_pinned_aiagent_tools_empty_and_chat_str_with_fake_transport(tmp_path, monkeypatch):
    src = _hermes_src()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "no-plugins"))
    (tmp_path / "hermes-home").mkdir()
    (tmp_path / "no-plugins").mkdir()
    (tmp_path / "hermes-home" / "config.yaml").write_text(
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
        "plugins:\n  enabled: []\nagent:\n  enabled_toolsets: []\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(src))
    from run_agent import AIAgent
    from model_tools import get_tool_definitions

    sig = inspect.signature(AIAgent.chat)
    assert sig.return_annotation is str
    tools = get_tool_definitions(enabled_toolsets=[], quiet_mode=True)
    assert tools == []
    agent = AIAgent(
        model="stub-model",
        api_key="sk-test",
        base_url="http://127.0.0.1:9/v1",
        **ISOLATED_AGENT_KWARGS,
        ephemeral_system_prompt="json only",
    )
    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert agent._memory_manager is None
    assert agent._memory_store is None
    content = json.dumps({"text": "from-fake", "cited_handles": ["h1"]})

    def fake_call(_kwargs):
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)

    agent._disable_streaming = True
    agent._interruptible_api_call = fake_call
    agent._cached_system_prompt = "test"
    agent._session_db = None
    agent.compression_enabled = False
    out = agent.chat("hello")
    assert isinstance(out, str)
    assert json.loads(out)["text"] == "from-fake"
