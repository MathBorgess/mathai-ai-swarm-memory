"""Isolated ask generator: per-job Hermes profile in a hardened container."""

from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import signal
import subprocess
import tempfile
import time
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
    "skip_background_review": True,
}

_PUBLIC_STYLE = "style/SOUL.md"


class IsolationUnavailable(RuntimeError):
    status = 503


class WorkerTimeout(RuntimeError):
    status = 504


def worker_root() -> Path:
    return Path(__file__).resolve().parents[2] / "ask-worker"


def public_style_path() -> Path:
    return worker_root() / "style" / "SOUL.md"


@dataclass
class IsolationConfig:
    image: str
    launch_script: Path
    worker_script: Path
    style_path: Path
    docker_bin: Path
    inference_config: Path
    network: str

    def validate(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise IsolationUnavailable("Ask isolation image is not configured")
        if not isinstance(self.network, str) or not self.network.strip():
            raise IsolationUnavailable("Ask worker network is not configured")
        if self.network == "host":
            raise IsolationUnavailable("Ask worker must not use host network")
        inference = Path(self.inference_config).expanduser()
        if not inference.is_file():
            raise IsolationUnavailable("Ask inference config is missing")
        inference = inference.resolve()
        _reject_owner_secret(inference, "inference config")
        style = Path(self.style_path).expanduser()
        if not style.is_file():
            raise IsolationUnavailable("Ask style is missing")
        style = style.resolve()
        if style != public_style_path().resolve():
            raise IsolationUnavailable("Ask style must be the public worker SOUL.md")
        for path, label in (
            (self.launch_script, "launch script"),
            (self.worker_script, "worker script"),
        ):
            if not Path(path).is_file():
                raise IsolationUnavailable(f"Ask {label} is missing")


def write_job_profile(directory: Path, *, style_path: Path) -> None:
    """Write a clean generated profile. Never reads or deletes an existing user home."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(worker_root() / "config.yaml", directory / "config.yaml")
    shutil.copyfile(style_path, directory / "SOUL.md")
    (directory / "bundled-plugins").mkdir(exist_ok=True)
    (directory / "home").mkdir(exist_ok=True)


def dockerfile_entrypoint(text: str | None = None) -> list[str]:
    source = text if text is not None else (worker_root() / "Dockerfile").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("ENTRYPOINT "):
            raw = stripped[len("ENTRYPOINT ") :].strip()
            value = json.loads(raw)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise IsolationUnavailable("Ask Dockerfile ENTRYPOINT is invalid")
            return value
    raise IsolationUnavailable("Ask Dockerfile ENTRYPOINT is missing")


def launch_command_after_image() -> list[str]:
    return ["/job.json"]


def effective_container_argv(entrypoint: list[str], command: list[str]) -> list[str]:
    """Docker exec-form ENTRYPOINT + command (what the container actually runs)."""
    return list(entrypoint) + list(command)


class ContainerWorker:
    def __init__(self, isolation: IsolationConfig):
        self.isolation = isolation
        self._container_name: str | None = None
        self._proc: subprocess.Popen | None = None
        self._docker: Path | None = None

    def abort(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
        name = self._container_name
        docker = self._docker
        if name and docker is not None:
            _remove_container(docker, name)

    def generate(self, payload: dict) -> dict:
        self.isolation.validate()
        docker = _docker_bin(self.isolation.docker_bin)
        self._docker = docker
        timeout = max(1, int((payload.get("budget") or {}).get("timeout_seconds") or 30))
        max_chars = max(1, int((payload.get("budget") or {}).get("max_output_chars") or 8000))
        max_bytes = max_chars + 8192
        name = f"ask-{secrets.token_hex(8)}"
        self._container_name = name
        profile = Path(tempfile.mkdtemp(prefix="ask-profile-"))
        job_path = None
        try:
            write_job_profile(profile, style_path=self.isolation.style_path)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(payload, handle)
                job_path = handle.name
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ASK_IMAGE": self.isolation.image.strip(),
                "ASK_JOB": job_path,
                "ASK_PROFILE": str(profile),
                "ASK_WORKER_SCRIPT": str(Path(self.isolation.worker_script).resolve()),
                "ASK_STYLE": str(Path(self.isolation.style_path).resolve()),
                "ASK_INFERENCE": str(Path(self.isolation.inference_config).resolve()),
                "ASK_DOCKER": str(docker),
                "ASK_NETWORK": self.isolation.network,
                "ASK_CONTAINER_NAME": name,
                "ASK_OWNER_SENTINEL": os.environ.get("ASK_OWNER_SENTINEL", ""),
            }
            argv = ["bash", str(Path(self.isolation.launch_script).resolve()), job_path]
            try:
                proc = subprocess.Popen(
                    argv,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as error:
                raise IsolationUnavailable("Ask worker failed closed") from error
            self._proc = proc
            stdout, stderr, timed_out, capped = _read_capped(proc, timeout=timeout, max_bytes=max_bytes)
            if timed_out or proc.poll() is None:
                self.abort()
                raise WorkerTimeout("Ask worker timed out")
            if capped:
                self.abort()
                raise IsolationUnavailable("Ask worker output exceeded budget")
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                raise IsolationUnavailable(err or "Ask worker failed closed")
            try:
                result = json.loads(stdout.decode("utf-8"))
            except ValueError as error:
                raise IsolationUnavailable("Ask worker returned invalid output") from error
            if not isinstance(result, dict) or "text" not in result:
                raise IsolationUnavailable("Ask worker returned invalid output")
            if isinstance(result.get("text"), str) and len(result["text"]) > max_chars:
                result["text"] = result["text"][:max_chars]
            return result
        finally:
            self._proc = None
            if job_path:
                Path(job_path).unlink(missing_ok=True)
            shutil.rmtree(profile, ignore_errors=True)
            if self._container_name:
                _remove_container(docker, self._container_name)
            self._container_name = None


def _reject_owner_secret(path: Path, label: str) -> None:
    owner_hermes = (Path.home() / ".hermes").resolve()
    owner_env = (Path.home() / ".env").resolve()
    try:
        if path == owner_env or path == owner_hermes:
            raise IsolationUnavailable(f"Ask {label} must not be an owner credential/config")
        path.relative_to(owner_hermes)
    except IsolationUnavailable:
        raise
    except ValueError:
        return
    except OSError:
        return
    else:
        raise IsolationUnavailable(f"Ask {label} must not be an owner credential/config")


def _read_capped(proc: subprocess.Popen, *, timeout: float, max_bytes: int) -> tuple[bytes, bytes, bool, bool]:
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    out = proc.stdout
    err = proc.stderr
    streams = [item for item in (out, err) if item is not None]
    capped = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bytes(stdout), bytes(stderr), True, capped
        if proc.poll() is not None and not streams:
            break
        ready, _, _ = select.select(streams, [], [], min(0.2, max(0.01, remaining)))
        if not ready:
            if proc.poll() is not None:
                # Drain leftovers without blocking past the deadline.
                for stream in list(streams):
                    chunk = stream.read() if stream else b""
                    if not chunk:
                        streams.remove(stream)
                        continue
                    if stream is out:
                        room = max_bytes - len(stdout)
                        if room <= 0:
                            capped = True
                            _kill_process_group(proc)
                            return bytes(stdout), bytes(stderr), False, True
                        stdout.extend(chunk[:room])
                        if len(chunk) > room:
                            capped = True
                            _kill_process_group(proc)
                            return bytes(stdout), bytes(stderr), False, True
                    else:
                        stderr.extend(chunk[:65536])
                break
            continue
        for stream in ready:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                streams.remove(stream)
                continue
            if stream is out:
                room = max_bytes - len(stdout)
                if room <= 0:
                    capped = True
                    _kill_process_group(proc)
                    return bytes(stdout), bytes(stderr), False, True
                stdout.extend(chunk[:room])
                if len(chunk) > room:
                    capped = True
                    _kill_process_group(proc)
                    return bytes(stdout), bytes(stderr), False, True
            else:
                stderr.extend(chunk[:65536])
    return bytes(stdout), bytes(stderr), False, capped


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def _remove_container(docker: Path, name: str) -> None:
    try:
        subprocess.run(
            [str(docker), "rm", "-f", name],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _docker_bin(value: Path | str) -> Path:
    path = Path(value)
    if path.is_file() and os.access(path, os.X_OK):
        return path.resolve()
    found = shutil.which(str(value))
    if found:
        return Path(found)
    raise IsolationUnavailable("Ask container runtime is missing")
