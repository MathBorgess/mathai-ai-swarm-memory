from __future__ import annotations

import pytest
from swarm_reports.dispatch.providers import (
    CLAUDE_CLI,
    CODEX_CLI,
    CURSOR_CLI,
    DEFAULT_MODEL,
    ProviderCLI,
    RunResult,
    SubprocessRunner,
    model_argv,
    parse_model_ids,
    probe_model,
    redact,
)


class FakeRunner:
    def __init__(self, result: RunResult, *, capture_argv: list | None = None):
        self._result = result
        self.calls: list[list[str]] = []
        self._capture = capture_argv if capture_argv is not None else self.calls

    def run(self, argv, *, stdin, timeout):
        self._capture.append(list(argv))
        return self._result


# --- finding 4e: a model list is parsed, not "first line of stdout" ---------


def test_header_line_is_never_taken_as_a_model_id():
    """`agent --list-models` prints a heading first; it is not a model."""
    stdout = "Available models:\n  gpt-5\n  sonnet-4-thinking\n"
    assert parse_model_ids(stdout) == ["gpt-5", "sonnet-4-thinking"]
    runner = FakeRunner(RunResult(0, stdout, "", timed_out=False))
    assert probe_model(CURSOR_CLI, runner) == "gpt-5"


@pytest.mark.parametrize(
    "line",
    ["Available models:", "Usage: agent [options]", "Models", "current default", "", "   ", "-- flag"],
)
def test_prose_and_headings_are_not_model_ids(line):
    assert parse_model_ids(line) == []


def test_bulleted_and_duplicated_lists_are_normalized():
    assert parse_model_ids("- gpt-5\n* gpt-5\n• claude-opus-5\n") == ["gpt-5", "claude-opus-5"]


def test_probe_returns_the_first_listed_id():
    runner = FakeRunner(RunResult(0, "claude-sonnet-5\nextra\n", "", timed_out=False))
    assert probe_model(CURSOR_CLI, runner) == "claude-sonnet-5"


def test_a_preferred_model_is_used_only_when_the_cli_actually_lists_it():
    runner = FakeRunner(RunResult(0, "gpt-5\nsonnet-4-thinking\n", "", timed_out=False))
    assert probe_model(CURSOR_CLI, runner, prefer="sonnet-4-thinking") == "sonnet-4-thinking"
    assert probe_model(CURSOR_CLI, runner, prefer="retired-model-9") == "gpt-5"


def test_probe_model_falls_back_to_default_on_nonzero_exit():
    runner = FakeRunner(RunResult(1, "", "not found", timed_out=False))
    assert probe_model(CURSOR_CLI, runner) == DEFAULT_MODEL


def test_probe_model_falls_back_to_default_on_timeout():
    runner = FakeRunner(RunResult(-1, "", "timeout", timed_out=True))
    assert probe_model(CURSOR_CLI, runner) == DEFAULT_MODEL


def test_probe_model_never_invents_a_stale_hardcoded_id():
    runner = FakeRunner(RunResult(0, "", "", timed_out=False))
    assert probe_model(CURSOR_CLI, runner) == DEFAULT_MODEL


def test_output_with_no_parseable_id_falls_back_to_default():
    runner = FakeRunner(RunResult(0, "Available models:\nsee the docs\n", "", timed_out=False))
    assert probe_model(CURSOR_CLI, runner) == DEFAULT_MODEL


# --- only CLIs that document a model list get probed at all ------------------


def test_claude_and_codex_are_not_probed_because_their_help_lists_no_models():
    runner = FakeRunner(RunResult(0, "Usage: claude [options]", "", timed_out=False))
    assert probe_model(CLAUDE_CLI, runner) == DEFAULT_MODEL
    assert probe_model(CODEX_CLI, runner) == DEFAULT_MODEL
    assert runner.calls == []  # no subprocess attempted at all


def test_cursor_is_the_only_cli_with_a_model_probe():
    assert CURSOR_CLI.can_probe_models is True
    assert CLAUDE_CLI.can_probe_models is False
    assert CODEX_CLI.can_probe_models is False
    assert CURSOR_CLI.probe_argv == ("agent", "--list-models")


def test_default_model_means_omit_the_flag_entirely():
    assert model_argv(CURSOR_CLI, DEFAULT_MODEL) == []
    assert model_argv(CURSOR_CLI, "gpt-5") == ["--model", "gpt-5"]
    assert model_argv(CODEX_CLI, "gpt-5") == ["-m", "gpt-5"]  # codex uses -m


def test_probe_argv_is_passed_through_unmodified():
    captured: list = []
    runner = FakeRunner(RunResult(0, "m-1", "", timed_out=False), capture_argv=captured)
    probe_model(ProviderCLI("cursor", "agent", ("agent", "--list-models")), runner)
    assert captured == [["agent", "--list-models"]]


# --- transport safety --------------------------------------------------------


def test_redact_strips_bearer_tokens_api_keys_and_jwts():
    text = "Authorization: Bearer abcdEFGH12345678 sk-abcdefghijklmnop1234 eyAAAAAAAAAA.bBBBBBBBBBB.cCCCCCCCCCC"
    out = redact(text)
    assert "abcdEFGH12345678" not in out
    assert "sk-abcdefghijklmnop1234" not in out
    assert "eyAAAAAAAAAA.bBBBBBBBBBB.cCCCCCCCCCC" not in out
    assert "[redacted]" in out


def test_subprocess_runner_rejects_shell_string_argv():
    with pytest.raises(TypeError):
        SubprocessRunner().run("echo hi", stdin=None, timeout=1.0)


def test_subprocess_runner_executes_argv_list_without_shell():
    result = SubprocessRunner().run(["python3", "-c", "print('hello')"], stdin=None, timeout=5.0)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_subprocess_runner_times_out_cleanly():
    result = SubprocessRunner().run(["python3", "-c", "import time; time.sleep(2)"], stdin=None, timeout=0.2)
    assert result.timed_out is True


def test_subprocess_runner_reports_a_missing_binary_instead_of_raising():
    result = SubprocessRunner().run(["definitely-not-a-real-binary-xyz"], stdin=None, timeout=5.0)
    assert result.returncode == -1
    assert result.timed_out is False


def test_subprocess_runner_redacts_secrets_in_captured_output():
    result = SubprocessRunner().run(
        ["python3", "-c", "print('token sk-abcdefghijklmnop1234')"], stdin=None, timeout=5.0
    )
    assert "sk-abcdefghijklmnop1234" not in result.stdout
