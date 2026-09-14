from __future__ import annotations

from datetime import date, datetime

import pytest
from swarm_reports.dispatch.gh_adapter import (
    GhRequest,
    LocalReceipt,
    apply_decision,
    merge_argv,
    merge_graphql_variables,
    prepare_action,
)
from swarm_reports.dispatch.merge_policy import ChangedFile, MergeDecision, PullRequest

NOW = datetime(2026, 9, 14, 9, 0, 0)


class FakeTransport:
    def __init__(self):
        self.sent: list[GhRequest] = []

    def send(self, request: GhRequest) -> None:
        self.sent.append(request)


def _pr(head_sha: str = "abc") -> PullRequest:
    return PullRequest(
        repo="MathBorgess/mathai-ai-swarm-memory",
        head_sha=head_sha,
        base_branch="main",
        files=(ChangedFile("src/reports/swarm_reports/dispatch/quota.py", "modified"),),
        opened_on=date(2026, 9, 14),
    )


def test_merge_decision_sends_merge_op_via_injected_transport():
    transport = FakeTransport()
    assert apply_decision(42, _pr(), MergeDecision("merge", "ok"), transport) is True
    assert transport.sent[0].op == "merge"


# --- finding 5a: a merge is head-pinned or it is not sent --------------------


def test_merge_request_pins_the_expected_head_oid():
    action = prepare_action(42, _pr("deadbeef"), MergeDecision("merge", "ok"))
    assert action.request.expected_head_oid == "deadbeef"


def test_a_merge_request_cannot_be_built_without_a_head_oid():
    with pytest.raises(ValueError):
        GhRequest(repo="r", pr_number=1, op="merge")


def test_merge_with_no_head_sha_sends_nothing():
    action = prepare_action(42, _pr(""), MergeDecision("merge", "ok"))
    assert action.request is None
    assert "head sha" in action.skipped_reason


def test_merge_argv_carries_the_atomic_match_head_commit_guard():
    action = prepare_action(42, _pr("deadbeef"), MergeDecision("merge", "ok"))
    argv = merge_argv(action.request)
    assert argv[:3] == ["gh", "pr", "merge"]
    assert "--match-head-commit" in argv
    assert argv[argv.index("--match-head-commit") + 1] == "deadbeef"
    assert "--repo" in argv and "MathBorgess/mathai-ai-swarm-memory" in argv


def test_graphql_path_uses_expected_head_oid():
    action = prepare_action(42, _pr("deadbeef"), MergeDecision("merge", "ok"))
    assert merge_graphql_variables(action.request)["expectedHeadOid"] == "deadbeef"


def test_merge_argv_refuses_a_non_merge_request():
    action = prepare_action(42, _pr(), MergeDecision("draft", "why"))
    with pytest.raises(ValueError):
        merge_argv(action.request)


# --- finding 5b: nothing is published outward by default --------------------


def test_t0_suggestion_does_not_post_an_unsolicited_comment():
    transport = FakeTransport()
    receipts: list[LocalReceipt] = []
    sent = apply_decision(42, _pr(), MergeDecision("suggestion_only", "touches T0"), transport, receipts=receipts)
    assert sent is False
    assert transport.sent == []
    assert receipts[0].kind == "t0_suggestion"
    assert receipts[0].published is False


def test_t0_suggestion_posts_only_under_explicit_authorization():
    transport = FakeTransport()
    receipts: list[LocalReceipt] = []
    sent = apply_decision(
        42,
        _pr(),
        MergeDecision("suggestion_only", "touches T0"),
        transport,
        allow_external_comment=True,
        receipts=receipts,
    )
    assert sent is True
    assert transport.sent[0].op == "comment"
    assert receipts[0].published is True


def test_pr_review_leaves_the_pr_alone_and_records_a_receipt():
    transport = FakeTransport()
    receipts: list[LocalReceipt] = []
    sent = apply_decision(42, _pr(), MergeDecision("pr_review", "protected path"), transport, receipts=receipts)
    assert sent is False and transport.sent == []
    assert receipts[0].kind == "owner_review"


def test_draft_decision_sends_convert_to_draft_with_reason():
    transport = FakeTransport()
    apply_decision(42, _pr(), MergeDecision("draft", "protected path"), transport)
    assert transport.sent[0].op == "convert_to_draft"
    assert transport.sent[0].body == "protected path"


def test_block_decision_never_calls_transport():
    transport = FakeTransport()
    assert apply_decision(42, _pr(), MergeDecision("block", "checks red"), transport) is False
    assert transport.sent == []
