"""Isolated ask generator: dedicated Hermes profile in a hardened container."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ISOLATED_AGENT_KWARGS = {
    "enabled_toolsets": [],
    "skip_memory": True,
    "skip_context_files": True,
    "load_soul_identity": False,
    "quiet_mode": True,
    "max_iterations": 1,
    "save_trajectories": False,
}


class IsolationUnavailable(RuntimeError):
    status = 503


def worker_root() -> Path:
    return Path(__file__).resolve().parents[2] / "ask-worker"


@dataclass
class IsolationConfig:
    runtime_home: Path
    image: str
    launch_script: Path
    worker_script: Path
    style_path: Path
    docker_bin: Path
    network: str = "none"

    def validate(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise IsolationUnavailable("Ask isolation image is not configured")
        if self.network == "host":
            raise IsolationUnavailable("Ask worker must not use host network")
        home = Path(self.runtime_home).expanduser()
        if not home.is_dir():
            raise IsolationUnavailable("Ask runtime home is missing")
        home = home.resolve()
        owner_hermes = (Path.home() / ".hermes").resolve()
        owner_home = Path.home().resolve()
        if home == owner_home or home == owner_hermes:
            raise IsolationUnavailable("Ask runtime home must not be the owner profile")
        try:
            home.relative_to(owner_hermes)
        except ValueError:
            pass
        else:
            raise IsolationUnavailable("Ask runtime home must not be the owner profile")
        process_home = os.environ.get("HERMES_HOME", "").strip()
        if process_home:
            try:
                if Path(process_home).expanduser().resolve() == home:
                    raise IsolationUnavailable("Ask runtime home must not reuse process HERMES_HOME")
            except IsolationUnavailable:
                raise
            except OSError:
                pass
        for path, label in (
            (self.launch_script, "launch script"),
            (self.worker_script, "worker script"),
            (self.style_path, "style"),
        ):
            if not Path(path).is_file():
                raise IsolationUnavailable(f"Ask {label} is missing")


def materialize_profile(config: IsolationConfig) -> None:
    config.validate()
    home = Path(config.runtime_home)
    (home / "home").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(worker_root() / "config.yaml", home / "config.yaml")
    shutil.copyfile(config.style_path, home / "SOUL.md")
    memories = home / "memories"
    if memories.exists():
        shutil.rmtree(memories)


class ContainerWorker:
    def __init__(self, isolation: IsolationConfig):
        self.isolation = isolation

    def generate(self, payload: dict) -> dict:
        self.isolation.validate()
        docker = _docker_bin(self.isolation.docker_bin)
        materialize_profile(self.isolation)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            job_path = handle.name
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "ASK_RUNTIME_HOME": str(Path(self.isolation.runtime_home).resolve()),
            "ASK_IMAGE": self.isolation.image.strip(),
            "ASK_JOB": job_path,
            "ASK_WORKER_SCRIPT": str(Path(self.isolation.worker_script).resolve()),
            "ASK_STYLE": str(Path(self.isolation.style_path).resolve()),
            "ASK_DOCKER": str(docker),
            "ASK_NETWORK": self.isolation.network,
        }
        try:
            completed = subprocess.run(
                ["bash", str(Path(self.isolation.launch_script).resolve()), job_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=max(1, int(payload.get("budget", {}).get("timeout_seconds") or 30) + 2),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise IsolationUnavailable("Ask worker timed out") from error
        finally:
            Path(job_path).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise IsolationUnavailable(completed.stderr.strip() or "Ask worker failed closed")
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise IsolationUnavailable("Ask worker returned invalid output") from error
        if not isinstance(result, dict) or "text" not in result:
            raise IsolationUnavailable("Ask worker returned invalid output")
        return result


def _docker_bin(value: Path | str) -> Path:
    path = Path(value)
    if path.is_file() and os.access(path, os.X_OK):
        return path.resolve()
    found = shutil.which(str(value))
    if found:
        return Path(found)
    raise IsolationUnavailable("Ask container runtime is missing")
