from __future__ import annotations

from datetime import date, timedelta

import pytest
from swarm_reports.dispatch.merge_policy import (
    PullRequest,
    RepoPolicy,
    classify_path,
    evaluate_merge,
)

TODAY = date(2026, 9, 14)


def _policy(**kw) -> RepoPolicy:
    base = dict(repo="MathBorgess/mathai-ai-swarm-memory", allowed_paths=("src/reports/**",))
    base.update(kw)
    return RepoPolicy(**base)


def _pr(**kw) -> PullRequest:
    base = dict(
        repo="MathBorgess/mathai-ai-swarm-memory",
        head_sha="abc123",
        base_branch="main",
        changed_paths=("src/reports/swarm_reports/dispatch/quota.py",),
        checks_status="success",
        opened_on=TODAY,
    )
    base.update(kw)
    return PullRequest(**base)


def test_merges_when_allowlisted_ci_green_head_matches():
    decision = evaluate_merge(_pr(), _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "merge"


def test_fails_closed_on_pending_checks():
    decision = evaluate_merge(_pr(checks_status="pending"), _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "block"


def test_fails_closed_on_no_checks():
    decision = evaluate_merge(_pr(checks_status="none"), _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "block"


def test_fails_closed_on_failed_checks():
    decision = evaluate_merge(_pr(checks_status="failure"), _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "block"


def test_stale_head_blocks_and_requires_recheck():
    decision = evaluate_merge(_pr(), _policy(), current_head_sha="def456", today=TODAY)
    assert decision.action == "block"
    assert "head_sha" in decision.reason


def test_protected_path_forces_draft_even_with_green_ci():
    policy = _policy(protected_paths=("src/reports/swarm_reports/dispatch/claims.py",))
    pr = _pr(changed_paths=("src/reports/swarm_reports/dispatch/claims.py",))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY)
    assert decision.action == "draft"


def test_path_traversal_is_normalized_and_blocked_not_allowlisted():
    pr = _pr(changed_paths=("src/reports/../../etc/passwd",))
    decision = evaluate_merge(pr, _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "block"


def test_rename_style_path_with_arrow_is_still_matched_by_normalization():
    # renames arrive pre-split by the caller: old path and new path both checked
    pr = _pr(changed_paths=("src/reports/swarm_reports/dispatch/old_name.py",))
    decision = evaluate_merge(pr, _policy(), current_head_sha="abc123", today=TODAY)
    assert decision.action == "merge"


def test_wiki_path_auto_merges_when_lint_green():
    policy = _policy(repo="MathBorgess/mathai-wiki", allowed_paths=("wiki/**",))
    pr = _pr(repo="MathBorgess/mathai-wiki", changed_paths=("wiki/index.md",))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY, lint_green=True)
    assert decision.action == "merge"


def test_wiki_path_drafts_when_lint_red():
    policy = _policy(repo="MathBorgess/mathai-wiki", allowed_paths=("wiki/**",))
    pr = _pr(repo="MathBorgess/mathai-wiki", changed_paths=("wiki/index.md",))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY, lint_green=False)
    assert decision.action == "draft"


def test_skills_and_res_weights_are_always_draft_only():
    policy = _policy(allowed_paths=("skills/**", "brand/metrics/**"))
    pr = _pr(changed_paths=("skills/daily-plan/SKILL.md",))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY)
    assert decision.action == "draft"


def test_t0_paths_are_suggestion_only_never_a_pr_merge():
    policy = _policy(allowed_paths=("**",))
    pr = _pr(changed_paths=("CLAUDE.md",))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY)
    assert decision.action == "suggestion_only"


def test_tau_intent_blocks_merge_inside_two_week_hold_even_with_green_ci():
    policy = _policy(is_tau_intent=True)
    pr = _pr(opened_on=TODAY - timedelta(days=3))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY)
    assert decision.action == "draft"


def test_tau_intent_still_requires_normal_gates_after_two_weeks():
    policy = _policy(is_tau_intent=True)
    pr = _pr(opened_on=TODAY - timedelta(days=15))
    decision = evaluate_merge(pr, policy, current_head_sha="abc123", today=TODAY)
    assert decision.action == "merge"


def test_classify_path_buckets():
    assert classify_path("CLAUDE.md") == "t0"
    assert classify_path("wiki/principles/foo.md") == "t0"
    assert classify_path("skills/daily-plan/SKILL.md") == "skills_weights"
    assert classify_path("wiki/index.md") == "wiki"
    assert classify_path("src/reports/swarm_reports/dispatch/quota.py") == "code"
