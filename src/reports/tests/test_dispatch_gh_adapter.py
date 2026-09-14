from __future__ import annotations

from datetime import date

from swarm_reports.dispatch.gh_adapter import GhRequest, apply_decision
from swarm_reports.dispatch.merge_policy import MergeDecision, PullRequest


class FakeTransport:
    def __init__(self):
        self.sent: list[GhRequest] = []

    def send(self, request: GhRequest) -> None:
        self.sent.append(request)


def _pr() -> PullRequest:
    return PullRequest(
        repo="MathBorgess/mathai-ai-swarm-memory",
        head_sha="abc",
        base_branch="main",
        changed_paths=("src/reports/swarm_reports/dispatch/quota.py",),
        checks_status="success",
        opened_on=date(2026, 9, 14),
    )


def test_merge_decision_sends_merge_op_via_injected_transport():
    transport = FakeTransport()
    sent = apply_decision(42, _pr(), MergeDecision("merge", "ok"), transport)
    assert sent is True
    assert transport.sent == [GhRequest(repo=_pr().repo, pr_number=42, op="merge")]


def test_draft_decision_sends_convert_to_draft_with_reason():
    transport = FakeTransport()
    apply_decision(42, _pr(), MergeDecision("draft", "protected path"), transport)
    assert transport.sent[0].op == "convert_to_draft"
    assert transport.sent[0].body == "protected path"


def test_block_decision_never_calls_transport():
    transport = FakeTransport()
    sent = apply_decision(42, _pr(), MergeDecision("block", "checks red"), transport)
    assert sent is False
    assert transport.sent == []


def test_suggestion_only_posts_a_comment_not_a_merge():
    transport = FakeTransport()
    apply_decision(42, _pr(), MergeDecision("suggestion_only", "touches T0"), transport)
    assert transport.sent[0].op == "comment"
