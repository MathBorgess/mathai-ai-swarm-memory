"""Isolation is a container launch + dedicated profile, not a prompt instruction."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.ask.isolation import (
    ISOLATED_AGENT_KWARGS,
    IsolationConfig,
    IsolationUnavailable,
    ContainerWorker,
    materialize_profile,
    worker_root,
)
from test_ask import SENTINEL, _ingest, _isolation, _service
from test_context_manifest import advisor


def test_owner_hermes_home_is_rejected(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    hermes = owner / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / "home").mkdir()
    monkeypatch.setenv("HOME", str(owner))
    root = worker_root()
    cfg = IsolationConfig(
        runtime_home=hermes,
        image="mathai-ask-worker:test",
        launch_script=root / "launch.sh",
        worker_script=root / "worker_main.py",
        style_path=root / "style" / "SOUL.md",
        docker_bin=tmp_path / "docker",
        network="none",
    )
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_process_hermes_home_cannot_be_reused(tmp_path, monkeypatch):
    home = tmp_path / "ask-runtime"
    home.mkdir()
    (home / "home").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = _isolation(tmp_path)
    cfg.runtime_home = home
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_host_network_is_rejected(tmp_path):
    cfg = _isolation(tmp_path)
    cfg.network = "host"
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_missing_docker_fails_closed(tmp_path):
    store, _ = _ingest(tmp_path)
    cfg = _isolation(tmp_path)
    worker = ContainerWorker(cfg)
    service, _ = _service(tmp_path, store, worker=worker, isolation=cfg)
    with pytest.raises(IsolationUnavailable):
        service.ask(advisor(), "token-a")
    store.close()


def test_launch_script_has_hardened_container_flags():
    script = (worker_root() / "launch.sh").read_text(encoding="utf-8")
    for token in (
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--network",
        "HERMES_HOME=/isolated",
        "HOME=/isolated/home",
        "HERMES_BROKER_TOKEN",
    ):
        assert token in script
    assert "--privileged" not in script
    assert "$HOME/.hermes" not in script or "must not" in script.lower() or "fail" in script.lower()
    assert "unset HERMES_BROKER_TOKEN" in script


def test_worker_script_constructs_tools_free_aiagent():
    source = (worker_root() / "worker_main.py").read_text(encoding="utf-8")
    assert "from run_agent import AIAgent" in source
    for key, value in ISOLATED_AGENT_KWARGS.items():
        if value == []:
            assert "enabled_toolsets=[]" in source.replace(" ", "")
        elif value is True:
            assert f"{key}=True" in source
        elif value is False:
            assert f"{key}=False" in source
    assert "skip_memory=True" in source
    assert "skip_context_files=True" in source
    assert "query_as_broker" not in source
    assert "HERMES_A2A_URL" not in source


def test_materialized_profile_disables_memory_and_tools(tmp_path):
    cfg = _isolation(tmp_path)
    materialize_profile(cfg)
    config = (cfg.runtime_home / "config.yaml").read_text(encoding="utf-8")
    assert "memory_enabled: false" in config
    assert "user_profile_enabled: false" in config
    assert "enabled_toolsets: []" in config
    assert "home_mode: profile" in config
    soul = (cfg.runtime_home / "SOUL.md").read_text(encoding="utf-8")
    assert "MEMORY.md" not in soul
    assert SENTINEL not in soul
    assert not (cfg.runtime_home / "memories" / "MEMORY.md").exists()
    assert not (cfg.runtime_home / "memories" / "USER.md").exists()


def test_launch_sh_fail_closed_without_runtime_home(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "ASK_IMAGE": "mathai-ask-worker:test",
        "ASK_JOB": str(tmp_path / "job.json"),
        "ASK_WORKER_SCRIPT": str(worker_root() / "worker_main.py"),
        "ASK_STYLE": str(worker_root() / "style" / "SOUL.md"),
        "ASK_DOCKER": str(tmp_path / "missing-docker"),
    }
    (tmp_path / "job.json").write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        ["bash", str(worker_root() / "launch.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "ASK_RUNTIME_HOME" in completed.stderr


def _write_fake_docker(path: Path, log: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"log = {str(log)!r}\n"
        "open(log, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'text': 'isolated-stub', 'cited_handles': []}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_container_worker_invokes_launch_with_isolation_flags(tmp_path):
    log = tmp_path / "docker-argv.json"
    docker = tmp_path / "docker"
    _write_fake_docker(docker, log)
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    materialize_profile(cfg)
    job = {
        "query": "token-a",
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 100, "timeout_seconds": 2},
    }
    result = ContainerWorker(cfg).generate(job)
    assert result["text"] == "isolated-stub"
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert argv[0] == "run"
    assert "--read-only" in argv
    assert "--privileged" not in argv
    cap = argv.index("--cap-drop")
    assert argv[cap + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert "--network" in argv
    net = argv.index("--network")
    assert argv[net + 1] == "none"
    joined = " ".join(argv)
    assert str(Path.home()) not in joined
    assert "HERMES_HOME=/isolated" in joined
    assert "HOME=/isolated/home" in joined
    assert "HERMES_BROKER_TOKEN" not in joined
    mounts = [argv[i + 1] if argv[i] == "--mount" else a for i, a in enumerate(argv)]
    assert not any(".hermes" in str(item) and "ask-runtime" not in str(item) for item in argv)


def test_worker_process_cannot_read_owner_sentinel(tmp_path):
    owner = tmp_path / "owner-home"
    sentinel = owner / ".hermes" / "memories" / "MEMORY.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("OWNER_PRIVATE_MEMORY", encoding="utf-8")
    runtime = tmp_path / "ask-runtime"
    runtime.mkdir()
    (runtime / "home").mkdir()
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            {
                "query": "token-a",
                "prior_user_turns": [],
                "envelope": [{"handle": "h1", "text": "visible excerpt", "source_revision": "rev-1"}],
                "budget": {"max_output_chars": 200, "timeout_seconds": 2},
            }
        ),
        encoding="utf-8",
    )
    env = {
        "HOME": str(runtime / "home"),
        "HERMES_HOME": str(runtime),
        "HERMES_ASK_STUB": "1",
        "ASK_OWNER_SENTINEL": str(sentinel),
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "PYTHONPATH": str(worker_root()),
    }
    completed = subprocess.run(
        [sys.executable, str(worker_root() / "worker_main.py"), str(job)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["inspection"]["owner_sentinel_readable"] is False
    assert "OWNER_PRIVATE_MEMORY" not in completed.stdout
    assert payload["inspection"]["home"].endswith("ask-runtime/home")
    assert payload["inspection"]["hermes_home"].endswith("ask-runtime")
    assert payload["inspection"]["broker_token_present"] is False
