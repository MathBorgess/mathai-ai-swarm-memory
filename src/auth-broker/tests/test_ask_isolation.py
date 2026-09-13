"""Isolation is a container launch + generated profile, not a prompt instruction."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.ask.isolation import (
    ISOLATED_AGENT_KWARGS,
    IsolationConfig,
    IsolationUnavailable,
    ContainerWorker,
    WorkerTimeout,
    dockerfile_entrypoint,
    effective_container_argv,
    launch_command_after_image,
    public_style_path,
    worker_root,
    write_job_profile,
)
from test_ask import SENTINEL, _inference, _ingest, _isolation, _service
from test_context_manifest import advisor


def test_owner_hermes_inference_file_is_rejected(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    hermes = owner / ".hermes"
    hermes.mkdir(parents=True)
    secret = hermes / ".env"
    secret.write_text("OPENROUTER_API_KEY=owner", encoding="utf-8")
    monkeypatch.setenv("HOME", str(owner))
    root = worker_root()
    cfg = IsolationConfig(
        image="mathai-ask-worker:test",
        launch_script=root / "launch.sh",
        worker_script=root / "worker_main.py",
        style_path=root / "style" / "SOUL.md",
        docker_bin=tmp_path / "docker",
        inference_config=secret,
        network="none",
    )
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_owner_style_is_rejected(tmp_path):
    owner_soul = tmp_path / "SOUL.md"
    owner_soul.write_text("owner identity", encoding="utf-8")
    cfg = _isolation(tmp_path)
    cfg.style_path = owner_soul
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_host_network_is_rejected(tmp_path):
    cfg = _isolation(tmp_path)
    cfg.network = "host"
    with pytest.raises(IsolationUnavailable):
        cfg.validate()


def test_missing_network_fails_closed(tmp_path):
    cfg = _isolation(tmp_path)
    cfg.network = ""
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
        "ASK_INFERENCE",
        "ASK_PROFILE",
        "ASK_CONTAINER_NAME",
    ):
        assert token in script
    assert "--privileged" not in script
    assert "ASK_RUNTIME_HOME" not in script
    assert "unset HERMES_BROKER_TOKEN" in script
    assert "unset OPENROUTER_API_KEY" in script
    assert '"${ASK_IMAGE}" /job.json' in script
    assert "python3 /opt/ask-worker/worker_main.py /job.json" not in script


def test_dockerfile_entrypoint_plus_launch_command_is_single_python():
    entry = dockerfile_entrypoint()
    command = launch_command_after_image()
    argv = effective_container_argv(entry, command)
    assert entry == ["python3", "/opt/ask-worker/worker_main.py"]
    assert command == ["/job.json"]
    assert argv == ["python3", "/opt/ask-worker/worker_main.py", "/job.json"]
    dockerfile = (worker_root() / "Dockerfile").read_text(encoding="utf-8")
    pin = (worker_root() / "HERMES_PIN").read_text(encoding="utf-8")
    assert "de2d6a1b93508463c31434c1ae067e204af81238" in pin
    assert "install_hermes.sh" in dockerfile
    assert "verify_hermes.py" in dockerfile
    assert 'ENTRYPOINT ["python3", "/opt/ask-worker/worker_main.py"]' in dockerfile


def test_worker_script_constructs_tools_free_aiagent():
    source = (worker_root() / "worker_main.py").read_text(encoding="utf-8")
    assert "from run_agent import AIAgent" in source
    compact = source.replace(" ", "")
    assert "enabled_toolsets=[]" in compact
    for key, value in ISOLATED_AGENT_KWARGS.items():
        if value is True:
            assert f"{key}=True" in source
        elif value is False:
            assert f"{key}=False" in source
    assert "skip_memory=True" in source
    assert "skip_context_files=True" in source
    assert "assert_agent_runtime" in source
    assert "query_as_broker" not in source
    assert "HERMES_A2A_URL" not in source


def test_write_job_profile_does_not_delete_user_memories(tmp_path):
    victim = tmp_path / "existing-hermes" / "memories"
    victim.mkdir(parents=True)
    secret = victim / "MEMORY.md"
    secret.write_text("KEEP_ME", encoding="utf-8")
    dest = tmp_path / "job-profile"
    write_job_profile(dest, style_path=public_style_path())
    assert secret.read_text(encoding="utf-8") == "KEEP_ME"
    assert (dest / "config.yaml").is_file()
    assert "memory_enabled: false" in (dest / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in (dest / "config.yaml").read_text(encoding="utf-8")
    assert (dest / "bundled-plugins").is_dir()
    assert not (dest / "memories").exists()


def test_launch_sh_fail_closed_without_inference(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "ASK_IMAGE": "mathai-ask-worker:test",
        "ASK_JOB": str(tmp_path / "job.json"),
        "ASK_PROFILE": str(tmp_path / "profile"),
        "ASK_WORKER_SCRIPT": str(worker_root() / "worker_main.py"),
        "ASK_STYLE": str(worker_root() / "style" / "SOUL.md"),
        "ASK_DOCKER": str(tmp_path / "missing-docker"),
        "ASK_NETWORK": "none",
        "ASK_CONTAINER_NAME": "ask-test",
    }
    (tmp_path / "profile").mkdir()
    (tmp_path / "job.json").write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        ["bash", str(worker_root() / "launch.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "ASK_INFERENCE" in completed.stderr


def _write_fake_docker(path: Path, log: Path, mode: str = "ok") -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"log = {str(log)!r}\n"
        f"mode = {mode!r}\n"
        "open(log, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'rm':\n"
        "    sys.exit(0)\n"
        "if mode == 'sleep':\n"
        "    time.sleep(30)\n"
        "    sys.exit(0)\n"
        "if mode == 'huge':\n"
        "    sys.stdout.write('x' * 50000)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(1)\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'text': 'isolated-stub', 'cited_handles': []}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_container_worker_invokes_launch_with_isolation_flags(tmp_path):
    log = tmp_path / "docker-argv.jsonl"
    docker = tmp_path / "docker"
    _write_fake_docker(docker, log)
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    job = {
        "query": "token-a",
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 100, "timeout_seconds": 2},
    }
    result = ContainerWorker(cfg).generate(job)
    assert result["text"] == "isolated-stub"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    run = next(item for item in lines if item and item[0] == "run")
    assert "--read-only" in run
    assert "--privileged" not in run
    assert run[run.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in run
    assert run[run.index("--network") + 1] == "none"
    joined = " ".join(run)
    assert str(Path.home()) not in joined
    assert "HERMES_HOME=/isolated" in joined
    assert "/inference.json" in joined
    assert "ask-profile-" in joined
    assert ".hermes" not in joined
    image_at = run.index(cfg.image)
    assert run[image_at + 1 :] == ["/job.json"]
    reconstructed = effective_container_argv(dockerfile_entrypoint(), run[image_at + 1 :])
    assert reconstructed == ["python3", "/opt/ask-worker/worker_main.py", "/job.json"]


def test_timeout_removes_named_container(tmp_path):
    log = tmp_path / "docker-argv.jsonl"
    docker = tmp_path / "docker"
    _write_fake_docker(docker, log, mode="sleep")
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    job = {
        "query": "token-a",
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 100, "timeout_seconds": 1},
    }
    started = time.monotonic()
    with pytest.raises(WorkerTimeout):
        ContainerWorker(cfg).generate(job)
    assert time.monotonic() - started < 3
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(item and item[0] == "rm" and "-f" in item for item in lines)


def test_stdout_is_capped_during_read(tmp_path):
    log = tmp_path / "docker-argv.jsonl"
    docker = tmp_path / "docker"
    _write_fake_docker(docker, log, mode="huge")
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    job = {
        "query": "token-a",
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 32, "timeout_seconds": 2},
    }
    with pytest.raises(IsolationUnavailable):
        ContainerWorker(cfg).generate(job)


def test_worker_inspection_attempts_sentinel_read(tmp_path):
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
    inference = _inference(tmp_path, inspect=True, stub_text='{"text":"stub","cited_handles":[]}')
    env = {
        "HOME": str(runtime / "home"),
        "HERMES_HOME": str(runtime),
        "ASK_INFERENCE": str(inference),
        "ASK_OWNER_SENTINEL": str(sentinel),
        "PATH": os.environ.get("PATH", "/usr/bin"),
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
    # Host process can read the sentinel; claiming otherwise would be a fake helper.
    assert payload["inspection"]["owner_sentinel_readable"] is True
    assert payload["cited_handles"] == []
    assert payload["inspection"]["home"].endswith("ask-runtime/home")
    assert payload["inspection"]["broker_token_present"] is False


def test_worker_parse_does_not_label_all_sources_cited(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ask_worker_main", worker_root() / "worker_main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parsed = module.parse_model_output(
        '{"text":"only a","cited_handles":["h1","forged"]}',
        ["h1", "h2"],
    )
    assert parsed["cited_handles"] == ["h1"]
    assert parsed["source_handles"] == ["h1", "h2"]
    empty = module.parse_model_output("prose without json", ["h1", "h2"])
    assert empty["cited_handles"] == []
    assert empty["text"] == "prose without json"


def test_docker_smoke_stub_worker_without_provider(tmp_path):
    import shutil

    docker = shutil.which("docker")
    if not docker:
        pytest.skip("container proof missing: docker binary is not available")
    info = subprocess.run([docker, "info"], capture_output=True, text=True)
    if info.returncode != 0:
        pytest.skip("container proof missing: docker daemon is not available")
    dockerfile = tmp_path / "Dockerfile.smoke"
    dockerfile.write_text(
        "FROM python:3.12-slim\n"
        "RUN mkdir -p /opt/ask-worker/style /isolated/home /isolated/bundled-plugins\n"
        "COPY worker_main.py /opt/ask-worker/worker_main.py\n"
        "COPY style/SOUL.md /opt/ask-worker/style/SOUL.md\n"
        'ENTRYPOINT ["python3", "/opt/ask-worker/worker_main.py"]\n',
        encoding="utf-8",
    )
    build = subprocess.run(
        [docker, "build", "-t", "mathai-ask-smoke:test", "-f", str(dockerfile), str(worker_root())],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if build.returncode != 0:
        pytest.skip(f"container proof missing: docker build failed: {build.stderr[-400:]}")
    sentinel = tmp_path / "owner-home" / ".hermes" / "memories" / "MEMORY.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("OWNER_PRIVATE_MEMORY", encoding="utf-8")
    cfg = _isolation(tmp_path)
    cfg.docker_bin = Path(docker)
    cfg.image = "mathai-ask-smoke:test"
    cfg.network = "none"
    inference = _inference(
        tmp_path,
        inspect=True,
        stub_text='{"text":"smoke","cited_handles":[]}',
    )
    cfg.inference_config = inference
    # launch.sh does not forward ASK_OWNER_SENTINEL; the worker still tries the path if set.
    # The sentinel is not mounted, so a real container read fails.
    os.environ["ASK_OWNER_SENTINEL"] = str(sentinel)
    job = {
        "query": "token-a",
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 200, "timeout_seconds": 15},
    }
    try:
        result = ContainerWorker(cfg).generate(job)
    finally:
        os.environ.pop("ASK_OWNER_SENTINEL", None)
    assert result["text"] == "smoke"
    assert result["cited_handles"] == []
    inspection = result.get("inspection") or {}
    assert inspection.get("owner_sentinel_readable") is False
