"""Provider CLI invocation: argv-only subprocesses, dynamic model probing.

No model id is hardcoded here — ``probe_model`` asks the CLI itself (or the
caller supplies a probe command) and falls back to "default" (meaning: omit
``--model``) if the probe fails, per skills-catalog/skills/handoff routing.md.

Every call goes through a ``Runner`` so tests inject a fake transport instead
of spawning a real provider CLI.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
]
REDACTED = "[redacted]"


def redact(text: str) -> str:
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, stdin: str | None, timeout: float) -> RunResult: ...


class SubprocessRunner:
    """Real transport. argv only, never shell=True, no CLI output on unhandled error paths."""

    def run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 30.0) -> RunResult:
        if isinstance(argv, str):
            raise TypeError("argv must be a list, not a shell string")
        try:
            proc = subprocess.run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            return RunResult(proc.returncode, redact(proc.stdout), redact(proc.stderr), timed_out=False)
        except subprocess.TimeoutExpired:
            return RunResult(returncode=-1, stdout="", stderr="timeout", timed_out=True)


@dataclass(frozen=True)
class ProviderCLI:
    name: str
    binary: str
    probe_argv: tuple[str, ...]  # e.g. ("claude", "--version") or a models-list command


DEFAULT_MODEL = "default"  # sentinel meaning: omit --model / -m, let the CLI pick


def probe_model(cli: ProviderCLI, runner: Runner, *, timeout: float = 10.0) -> str:
    """Return a model id parsed from the probe, or DEFAULT_MODEL if the probe fails."""
    result = runner.run(cli.probe_argv, stdin=None, timeout=timeout)
    if result.timed_out or result.returncode != 0 or not result.stdout.strip():
        return DEFAULT_MODEL
    first_line = result.stdout.strip().splitlines()[0].strip()
    return first_line or DEFAULT_MODEL
