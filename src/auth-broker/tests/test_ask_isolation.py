"""Isolation is a container launch + generated profile, not a prompt instruction."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
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
from app.ask import AskBudget, AskService, AskTimeout, MemoryThreadStore
from test_ask import SENTINEL, _inference, _ingest, _isolation, _service
from test_ask_http import app_client
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


def _write_fake_docker(path: Path, log: Path, mode: str = "ok", stderr_text: str = "") -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"log = {str(log)!r}\n"
        f"mode = {mode!r}\n"
        f"stderr_text = {stderr_text!r}\n"
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
        "if mode == 'fail-stderr':\n"
        "    sys.stderr.write(stderr_text)\n"
        "    sys.stderr.flush()\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'text': 'isolated-stub', 'cited_handles': []}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _write_gated_docker(path: Path, log: Path, gate: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "from pathlib import Path\n"
        f"log = {str(log)!r}\n"
        f"gate = Path({str(gate)!r})\n"
        "gate.mkdir(parents=True, exist_ok=True)\n"
        "argv = sys.argv[1:]\n"
        "open(log, 'a', encoding='utf-8').write(json.dumps(argv) + '\\n')\n"
        "if argv and argv[0] == 'rm':\n"
        "    (gate / ('removed-' + argv[-1])).write_text('1', encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "name = argv[argv.index('--name') + 1] if '--name' in argv else 'unknown'\n"
        "query = ''\n"
        "for item in argv:\n"
        "    if 'dst=/job.json' in item:\n"
        "        for part in item.split(','):\n"
        "            if part.startswith('src='):\n"
        "                try:\n"
        "                    query = json.loads(Path(part[4:]).read_text(encoding='utf-8')).get('query') or ''\n"
        "                except Exception:\n"
        "                    query = ''\n"
        "(gate / ('started-' + name)).write_text(query, encoding='utf-8')\n"
        "if query in ('token-a', 'slow-job'):\n"
        "    time.sleep(60)\n"
        "    sys.exit(0)\n"
        "deadline = time.monotonic() + 10\n"
        "while time.monotonic() < deadline:\n"
        "    if (gate / 'release').exists():\n"
        "        print(json.dumps({'text': 'peer-ok', 'cited_handles': []}))\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.02)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _wait_started(gate: Path, count: int, timeout: float = 2.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = {p.name[len("started-") :]: p.read_text(encoding="utf-8") for p in gate.glob("started-*")}
        if len(rows) >= count:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"expected {count} started containers, got {list(gate.glob('started-*'))}")


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


def _job(query: str, timeout: int) -> dict:
    return {
        "query": query,
        "prior_user_turns": [],
        "envelope": [{"handle": "h1", "text": "visible", "source_revision": "rev-1"}],
        "budget": {"max_output_chars": 100, "timeout_seconds": timeout},
    }


def test_worker_stderr_is_absent_from_public_error_and_http_response(tmp_path):
    leak = (
        f"{SENTINEL} /root/.hermes/config.yaml OPENROUTER_API_KEY=sk-leak "
        "query=token-a path=/var/lib/auth-broker/ask-job.json"
    )
    log = tmp_path / "docker-argv.jsonl"
    docker = tmp_path / "docker"
    _write_fake_docker(docker, log, mode="fail-stderr", stderr_text=leak)
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    with pytest.raises(IsolationUnavailable) as worker_error:
        ContainerWorker(cfg).generate(_job("token-a", 2))
    worker_text = str(worker_error.value)
    assert SENTINEL not in worker_text
    assert "sk-leak" not in worker_text
    assert "token-a" not in worker_text
    assert "/root/.hermes" not in worker_text
    assert "OPENROUTER" not in worker_text
    assert "ask-job.json" not in worker_text
    store, _ = _ingest(tmp_path)
    service = AskService(
        store=store,
        worker=ContainerWorker(cfg),
        threads=MemoryThreadStore(),
        isolation=cfg,
        budget=AskBudget(timeout_seconds=2, max_concurrency=2),
    )
    with pytest.raises(IsolationUnavailable) as service_error:
        service.ask(advisor(), "token-a")
    service_text = str(service_error.value)
    assert SENTINEL not in service_text
    assert "sk-leak" not in service_text
    assert "token-a" not in service_text
    client, _ = app_client(
        tmp_path,
        store,
        lambda request: advisor(),
        worker=ContainerWorker(cfg),
        isolation=cfg,
    )
    response = client.post("/v1/context/ask", json={"query": "token-a"})
    assert response.status_code == 503
    assert SENTINEL not in response.text
    assert "sk-leak" not in response.text
    assert "/root/.hermes" not in response.text
    assert "OPENROUTER" not in response.text
    store.close()


def test_shared_worker_timeout_does_not_abort_other_entrypoint_job(tmp_path):
    log = tmp_path / "docker-argv.jsonl"
    docker = tmp_path / "docker"
    gate = tmp_path / "gate"
    gate.mkdir()
    _write_gated_docker(docker, log, gate)
    cfg = _isolation(tmp_path)
    cfg.docker_bin = docker
    worker = ContainerWorker(cfg)
    store, _ = _ingest(tmp_path)
    budget_slow = AskBudget(timeout_seconds=1, max_concurrency=2)
    budget_fast = AskBudget(timeout_seconds=5, max_concurrency=2)
    mcp = AskService(
        store=store,
        worker=worker,
        threads=MemoryThreadStore(),
        isolation=cfg,
        budget=budget_slow,
    )
    dpop = AskService(
        store=store,
        worker=worker,
        threads=MemoryThreadStore(),
        isolation=cfg,
        budget=budget_fast,
    )
    peer = advisor(principal_id="advisor-02", family_id="fam-other")
    slow_box: dict = {}
    fast_box: dict = {}

    def run_mcp():
        try:
            mcp.ask(advisor(), "token-a")
        except Exception as exc:
            slow_box["error"] = exc

    def run_dpop():
        try:
            fast_box["result"] = dpop.ask(peer, "token-c")
        except Exception as exc:
            fast_box["error"] = exc

    slow_thread = threading.Thread(target=run_mcp)
    slow_thread.start()
    started = _wait_started(gate, 1)
    fast_thread = threading.Thread(target=run_dpop)
    fast_thread.start()
    started = _wait_started(gate, 2)
    slow_name = next(name for name, query in started.items() if query == "token-a")
    fast_name = next(name for name, query in started.items() if query == "token-c")
    slow_thread.join(timeout=3)
    assert isinstance(slow_box.get("error"), AskTimeout)
    assert (gate / f"removed-{slow_name}").exists()
    assert not (gate / f"removed-{fast_name}").exists()
    assert fast_thread.is_alive()
    (gate / "release").write_text("1", encoding="utf-8")
    fast_thread.join(timeout=3)
    assert "error" not in fast_box
    assert fast_box["result"]["items"][0]["text"] == "peer-ok"
    assert (gate / f"removed-{fast_name}").exists()
    assert mcp._slots._value == budget_slow.max_concurrency
    assert dpop._slots._value == budget_fast.max_concurrency
    follow = dpop.ask(peer, "token-c")
    assert follow["items"][0]["text"] == "peer-ok"
    assert mcp._slots._value == budget_slow.max_concurrency
    assert dpop._slots._value == budget_fast.max_concurrency
    store.close()
