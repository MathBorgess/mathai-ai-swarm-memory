"""Provider CLI invocation: argv-only subprocesses, evidence-based model probing.

No model id is hardcoded. A provider only gets probed when its installed CLI
actually documents a machine-readable model list; otherwise ``probe_model``
returns ``DEFAULT_MODEL`` (meaning: omit ``--model`` and let the CLI pick),
which is what skills-catalog's `handoff` skill prescribes.

Evidence for the argv below (`--help` of the installed binaries on the owner's
Mac, 2026-09-14, cross-checked with `skills-catalog/skills/handoff/references/providers.md`):

| CLI | model flag | machine-readable model list |
|---|---|---|
| `agent` (cursor, `2026.09.10-fd3934a`) | `--model <model>` | **yes** — `--list-models` |
| `claude` (`2.1.239`) | `--model <model>` (alias or full id) | **no** — no `--list-models` in `--help` |
| `codex` (`0.153.2`) | `-m, --model <MODEL>` | **no** — no models subcommand in `--help` |

So exactly one provider is probed dynamically. Inventing a probe command for
the other two would produce a failed subprocess on every run and, worse, a
first line of help text parsed as if it were a model id.

Every call goes through a ``Runner`` so tests inject a fake transport instead
of spawning a real provider CLI.
"""

from __future__ import annotations

import re
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
REDACTED = "[redacted]"

DEFAULT_MODEL = "default"  # sentinel meaning: omit --model / -m, let the CLI pick

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{1,63}$")
_HEADER_HINTS = ("available", "models:", "usage", "options", "current", "default:")


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

    def __init__(self, cwd=None):
        self.cwd = cwd

    def run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: float = 30.0) -> RunResult:
        if isinstance(argv, str):
            raise TypeError("argv must be a list, not a shell string")
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, shell=False,
                                    cwd=self.cwd, start_new_session=True)
            try:
                out, err = proc.communicate(stdin, timeout=timeout)
                return RunResult(proc.returncode, redact(out[:2_000_000]), "" if not err else "provider diagnostic omitted", False)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return RunResult(-1, "", "timeout", True)
            finally:
                # A CLI must not leave background writers after returning or timing out.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (OSError, ValueError):
            return RunResult(-1, "", "launch failed", False)


@dataclass(frozen=True)
class ProviderCLI:
    name: str
    binary: str
    probe_argv: tuple[str, ...] = ()  # empty = this CLI has no model-list surface
    model_flag: str = "--model"

    @property
    def can_probe_models(self) -> bool:
        return bool(self.probe_argv)


# Installed-CLI facts, not guesses. See the module docstring for the evidence.
CURSOR_CLI = ProviderCLI(name="cursor", binary="agent", probe_argv=("agent", "--list-models"))
CLAUDE_CLI = ProviderCLI(name="claude", binary="claude", probe_argv=())
CODEX_CLI = ProviderCLI(name="codex", binary="codex", probe_argv=(), model_flag="-m")
KNOWN_CLIS: dict[str, ProviderCLI] = {c.name: c for c in (CURSOR_CLI, CLAUDE_CLI, CODEX_CLI)}


def parse_model_ids(stdout: str) -> list[str]:
    """Model ids from a `--list-models` style output, skipping prose and headers.

    A model list is not "the first line of stdout": the first line is usually
    ``Available models:``. An id must look like an id — one token, no spaces,
    and containing a digit or a hyphen, which every real id does
    (`gpt-5`, `sonnet-4-thinking`, `claude-opus-5`) and no heading does.
    """
    ids: list[str] = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip().lstrip("-*•").strip()
        if not line or line.endswith(":") or " " in line or "\t" in raw_line.strip():
            continue
        lowered = line.lower()
        if any(hint in lowered for hint in _HEADER_HINTS):
            continue
        if not _MODEL_ID_RE.match(line):
            continue
        if not any(ch.isdigit() or ch == "-" for ch in line):
            continue
        if line not in ids:
            ids.append(line)
    return ids


def probe_model(cli: ProviderCLI, runner: Runner, *, timeout: float = 10.0, prefer: str | None = None) -> str:
    """Return a model id the CLI itself listed, or DEFAULT_MODEL.

    ``prefer`` is honoured only if the probe actually lists it, so a stale
    preference in config can never resurrect a retired model id.
    """
    if not cli.can_probe_models:
        return DEFAULT_MODEL
    result = runner.run(cli.probe_argv, stdin=None, timeout=timeout)
    if result.timed_out or result.returncode != 0 or not result.stdout.strip():
        return DEFAULT_MODEL
    ids = parse_model_ids(result.stdout)
    if not ids:
        return DEFAULT_MODEL
    if prefer and prefer in ids:
        return prefer
    return ids[0]


def model_argv(cli: ProviderCLI, model: str) -> list[str]:
    """The `--model` fragment for a launch, empty when the model is unknown."""
    if model == DEFAULT_MODEL:
        return []
    return [cli.model_flag, model]


__all__ = [
    "CLAUDE_CLI",
    "CODEX_CLI",
    "CURSOR_CLI",
    "DEFAULT_MODEL",
    "KNOWN_CLIS",
    "ProviderCLI",
    "REDACTED",
    "RunResult",
    "Runner",
    "SubprocessRunner",
    "model_argv",
    "parse_model_ids",
    "probe_model",
    "redact",
]


def provider_argv(provider, command, *, model="default", read_only=False):
    """Native CLI prefixes verified against installed --help; no shell or bypass flags."""
    cli = KNOWN_CLIS[provider]
    argv = list(command or (cli.binary,))
    if provider == "codex":
        argv += ["exec", "--json", "--ephemeral", "--sandbox", "read-only" if read_only else "workspace-write"]
    else:
        argv += ["--print", "--output-format", "json"]
        if read_only:
            argv += ["--permission-mode", "plan"] if provider == "claude" else ["--mode", "plan"]
    return argv + model_argv(cli, model)


def provider_json(stdout):
    """Read native Claude/Cursor result envelopes or Codex JSONL agent_message."""
    import json
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        texts = [e["item"]["text"] for e in events
                 if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
        if not texts:
            raise ValueError("provider returned no final agent message")
        value = texts[-1]
    if isinstance(value, dict) and value.get("is_error"):
        raise ValueError("provider returned an error")
    if isinstance(value, dict) and ("result" in value or "structured_output" in value):
        value = value.get("structured_output") or value["result"]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("```json") and value.endswith("```"):
            value = value[7:-3].strip()
        value = json.loads(value)
    if not isinstance(value, (dict, list)):
        raise ValueError("provider result must be structured JSON")
    return value
