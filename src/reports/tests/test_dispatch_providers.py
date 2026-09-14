from __future__ import annotations

import pytest
from swarm_reports.dispatch.providers import (
    DEFAULT_MODEL,
    ProviderCLI,
    RunResult,
    SubprocessRunner,
    probe_model,
    redact,
)


class FakeRunner:
    def __init__(self, result: RunResult, *, capture_argv: list | None = None):
        self._result = result
        self._capture = capture_argv

    def run(self, argv, *, stdin, timeout):
        if self._capture is not None:
            self._capture.append(list(argv))
        return self._result


def test_probe_model_returns_first_line_on_success():
    runner = FakeRunner(RunResult(0, "claude-sonnet-5\nextra\n", "", timed_out=False))
    cli = ProviderCLI("claude", "claude", ("claude", "--print-model"))
    assert probe_model(cli, runner) == "claude-sonnet-5"


def test_probe_model_falls_back_to_default_on_nonzero_exit():
    runner = FakeRunner(RunResult(1, "", "not found", timed_out=False))
    cli = ProviderCLI("codex", "codex", ("codex", "models"))
    assert probe_model(cli, runner) == DEFAULT_MODEL


def test_probe_model_falls_back_to_default_on_timeout():
    runner = FakeRunner(RunResult(-1, "", "timeout", timed_out=True))
    cli = ProviderCLI("cursor", "cursor-agent", ("cursor-agent", "models"))
    assert probe_model(cli, runner) == DEFAULT_MODEL


def test_probe_model_never_invents_a_stale_hardcoded_id():
    runner = FakeRunner(RunResult(0, "", "", timed_out=False))  # empty stdout
    cli = ProviderCLI("claude", "claude", ("claude", "--print-model"))
    assert probe_model(cli, runner) == DEFAULT_MODEL


def test_probe_argv_is_passed_through_unmodified():
    captured: list = []
    runner = FakeRunner(RunResult(0, "m1", "", timed_out=False), capture_argv=captured)
    cli = ProviderCLI("claude", "claude", ("claude", "--print-model"))
    probe_model(cli, runner)
    assert captured == [["claude", "--print-model"]]


def test_redact_strips_bearer_tokens_and_api_keys():
    text = "Authorization: Bearer abcdEFGH12345678 sk-abcdefghijklmnop1234"
    out = redact(text)
    assert "abcdEFGH12345678" not in out
    assert "sk-abcdefghijklmnop1234" not in out
    assert "[redacted]" in out


def test_subprocess_runner_rejects_shell_string_argv():
    runner = SubprocessRunner()
    with pytest.raises(TypeError):
        runner.run("echo hi", stdin=None, timeout=1.0)


def test_subprocess_runner_executes_argv_list_without_shell(tmp_path):
    runner = SubprocessRunner()
    result = runner.run(["python3", "-c", "print('hello')"], stdin=None, timeout=5.0)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_subprocess_runner_times_out_cleanly():
    runner = SubprocessRunner()
    result = runner.run(["python3", "-c", "import time; time.sleep(2)"], stdin=None, timeout=0.2)
    assert result.timed_out is True
