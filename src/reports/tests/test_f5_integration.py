"""F5 runtime wiring: handoff dispatch, digest persistence, evening form draft."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_reports.dispatch.evening_autonomy import (
    StaticDiffReviewer,
    format_pr_body_with_cards,
    process_evening_pr,
)
from swarm_reports.dispatch.digest import DecisionCard
from swarm_reports.dispatch.morning_dispatch import ProviderProbe, run_morning_handoff_dispatch
from swarm_reports.dispatch.policy_config import load_dispatch_policy
from swarm_reports.dispatch.providers import RunResult, Runner
from swarm_reports.dispatch.runtime_store import load_runtime
from swarm_reports.plan import HandoffCard
from swarm_reports.rendering.html import MorningViewModel, render_morning_html
from tests.conftest import DAY
from swarm_reports.evening_schema import EveningPayload, SCHEMA_VERSION
from swarm_reports.plan import MorningPlan


class _RecordingRunner(Runner):
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv, *, stdin: str | None = None, timeout: float = 30.0) -> RunResult:
        self.calls.append((list(argv), stdin))
        return RunResult(0, "{}", "", timed_out=False)


@pytest.fixture
def policy_path(tmp_path) -> Path:
    example = Path(__file__).resolve().parents[3] / "config" / "reports-policy.example.json"
    policy = json.loads(example.read_text(encoding="utf-8"))
    policy["state_dir"] = str(tmp_path / "dispatch-state")
    policy["handoff"] = {
        "default_cost_pct": 5.0,
        "command": ["/usr/bin/fake-handoff"],
    }
    policy.setdefault("routing", {})["model_probe"] = {"cursor": False, "claude": False, "codex": False}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_morning_handoff_dispatch_records_deferred_when_budget_tight(policy_path, tmp_path):
    policy = load_dispatch_policy(policy_path)
    policy = policy  # loaded
    policy_json = json.loads(policy_path.read_text())
    policy_json["handoff"]["default_cost_pct"] = 90.0
    policy_path.write_text(json.dumps(policy_json))
    policy = load_dispatch_policy(policy_path)

    handoffs = [
        HandoffCard(task_id="h1", title="first", objective="o1", copy_prompt="p1"),
        HandoffCard(task_id="h2", title="second", objective="o2", copy_prompt="p2"),
    ]
    probes = [ProviderProbe("cursor", None, datetime.now(timezone.utc))]
    runner = _RecordingRunner()
    result = run_morning_handoff_dispatch(
        policy=policy,
        state_dir=tmp_path / "state",
        day="2026-09-14",
        handoffs=handoffs,
        probes=probes,
        runner=runner,
    )
    assert result.deferred
    runtime = load_runtime(tmp_path / "state")
    assert runtime.deferred[0].task_id in {"h1", "h2"}


def test_evening_autonomy_stores_digest_cards(policy_path, tmp_path):
    policy = load_dispatch_policy(policy_path)

    body_sample = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "dispatch"
        / "sample_pr_body.md"
    ).read_text(encoding="utf-8")

    class _FakeGh:
        def pr_view_json(self, repo: str, pr_number: int) -> dict:
            return {
                "headRefOid": "abc123def456",
                "baseRefName": "main",
                "body": body_sample,
                "files": [{"path": "daily/2026-09-14.md", "changeType": "modified"}],
            }

        def pr_diff(self, repo: str, pr_number: int) -> str:
            return ""

        def send(self, request) -> None:
            raise AssertionError("merge should not run in this fixture")

    outcome = process_evening_pr(
        policy=policy,
        state_dir=tmp_path / "state",
        pr_url="https://github.com/MathBorgess/mathai-wiki/pull/42",
        lint_ok=False,
        gh=_FakeGh(),
        independent_reviewer=StaticDiffReviewer([]),
    )
    assert outcome.cards
    assert outcome.merge_attempted is False
    runtime = load_runtime(tmp_path / "state")
    assert runtime.digest_cards


def test_evening_notes_persist_on_input_event_without_blur(tmp_path):
    plan = MorningPlan(day=DAY, checklist=[{"task_id": "t1", "text": "x", "is_p0": True}])
    html = render_morning_html(
        MorningViewModel(
            day=DAY,
            owner_id="owner",
            metrics=[],
            plan=plan,
            evening_seed=EveningPayload(
                schema_version=SCHEMA_VERSION, day=DAY, owner_id="owner", checklist=[]
            ),
        )
    )
    assert '"input", persist' in html
    assert "evening-notes" in html


def test_handoff_runner_receives_json_stdin(policy_path, tmp_path):
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    raw.setdefault("routing", {})["model_probe"] = {"cursor": False, "claude": False, "codex": False}
    policy_path.write_text(json.dumps(raw), encoding="utf-8")
    policy = load_dispatch_policy(policy_path)
    runner = _RecordingRunner()
    run_morning_handoff_dispatch(
        policy=policy,
        state_dir=tmp_path / "state",
        day="2026-09-14",
        handoffs=[HandoffCard(task_id="h1", title="t", objective="o", copy_prompt="do it")],
        probes=[ProviderProbe("cursor", 50.0, datetime.now(timezone.utc))],
        runner=runner,
    )
    handoff_calls = [c for c in runner.calls if c[0] and c[0][0] == "/usr/bin/fake-handoff"]
    assert handoff_calls
    _argv, stdin = handoff_calls[0]
    assert stdin is not None
    payload = json.loads(stdin)
    assert payload["task_id"] == "h1"
    assert payload["prompt"] == "do it"
