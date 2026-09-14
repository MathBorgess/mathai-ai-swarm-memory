from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from swarm_reports.dispatch.merge_policy import (
    ChangedFile,
    CheckSignal,
    MalformedPath,
    PullRequest,
    RepoPolicy,
    TauIntentState,
    classify_path,
    evaluate_merge,
    matches_any,
    normalize_path,
)

NOW = datetime(2026, 9, 14, 9, 0, 0)
TODAY = NOW.date()
HEAD = "abc123"


def _green(kind: str = "checks", head: str = HEAD, minutes_old: int = 5) -> CheckSignal:
    return CheckSignal(state="success", head_sha=head, observed_at=NOW - timedelta(minutes=minutes_old))


def _code_policy(**kw) -> RepoPolicy:
    base = dict(
        repo="MathBorgess/mathai-ai-swarm-memory",
        kind="code",
        allowed_paths=("src/reports/**",),
    )
    base.update(kw)
    return RepoPolicy(**base)


def _wiki_policy(**kw) -> RepoPolicy:
    base = dict(
        repo="MathBorgess/mathai-wiki",
        kind="wiki",
        allowed_paths=("wiki/**", "daily/**", "estudos/**", "pesquisa/**", "brand/**"),
        protected_paths=(),
        weights_paths=("brand/metrics/res-weights.yaml",),
    )
    base.update(kw)
    return RepoPolicy(**base)


def _pr(**kw) -> PullRequest:
    base = dict(
        repo="MathBorgess/mathai-ai-swarm-memory",
        head_sha=HEAD,
        base_branch="main",
        files=(ChangedFile("src/reports/swarm_reports/dispatch/quota.py", "modified"),),
        opened_on=TODAY,
        checks=_green(),
    )
    base.update(kw)
    return PullRequest(**base)


def _decide(pr: PullRequest, policy: RepoPolicy, **kw):
    kw.setdefault("current_head_sha", HEAD)
    kw.setdefault("now", NOW)
    return evaluate_merge(pr, policy, **kw)


# --- baseline code-repo behaviour -------------------------------------------


def test_merges_when_allowlisted_ci_green_head_matches():
    assert _decide(_pr(), _code_policy()).action == "merge"


@pytest.mark.parametrize("state", ["pending", "none", "failure", "error"])
def test_fails_closed_on_any_non_success_check(state):
    pr = _pr(checks=CheckSignal(state=state, head_sha=HEAD, observed_at=NOW))
    assert _decide(pr, _code_policy()).action == "block"


def test_stale_head_blocks_and_requires_recheck():
    decision = _decide(_pr(), _code_policy(), current_head_sha="def456")
    assert decision.action == "block"
    assert "head_sha" in decision.reason


def test_empty_head_sha_on_either_side_blocks():
    assert _decide(_pr(head_sha=""), _code_policy()).action == "block"
    assert _decide(_pr(), _code_policy(), current_head_sha="").action == "block"


def test_no_changed_files_blocks():
    assert _decide(_pr(files=()), _code_policy()).action == "block"


# --- finding 1a: the check signal must be evidence, not a caller's boolean ---


def test_missing_check_signal_blocks_instead_of_defaulting_to_green():
    assert _decide(_pr(checks=None), _code_policy()).action == "block"


def test_green_check_from_an_older_commit_does_not_authorize_this_head():
    pr = _pr(checks=CheckSignal(state="success", head_sha="oldsha", observed_at=NOW))
    decision = _decide(pr, _code_policy())
    assert decision.action == "block"
    assert "not the current head" in decision.reason


def test_stale_green_check_blocks():
    pr = _pr(checks=CheckSignal(state="success", head_sha=HEAD, observed_at=NOW - timedelta(days=2)))
    decision = _decide(pr, _code_policy())
    assert decision.action == "block" and "stale" in decision.reason


def test_check_observed_in_the_future_blocks():
    pr = _pr(checks=CheckSignal(state="success", head_sha=HEAD, observed_at=NOW + timedelta(hours=1)))
    assert _decide(pr, _code_policy()).action == "block"


def test_green_but_required_contexts_not_all_reported_blocks():
    pr = _pr(
        checks=CheckSignal(state="success", head_sha=HEAD, observed_at=NOW, required_contexts_met=False)
    )
    assert _decide(pr, _code_policy()).action == "block"


def test_wiki_lint_must_be_a_real_current_signal_not_a_default():
    policy = _wiki_policy()
    pr = _pr(repo=policy.repo, files=(ChangedFile("estudos/x/nota.md", "added"),), lint=None, checks=None)
    assert _decide(pr, policy).action == "block"

    pr_green = _pr(
        repo=policy.repo, files=(ChangedFile("estudos/x/nota.md", "added"),), lint=_green(), checks=None
    )
    assert _decide(pr_green, policy).action == "merge"


def test_wiki_lint_red_blocks():
    policy = _wiki_policy()
    pr = _pr(
        repo=policy.repo,
        files=(ChangedFile("estudos/x/nota.md", "added"),),
        lint=CheckSignal(state="failure", head_sha=HEAD, observed_at=NOW),
        checks=None,
    )
    assert _decide(pr, policy).action == "block"


# --- finding 1b: wiki tiers, not "anything under wiki/" ---------------------


def test_compiled_wiki_t1_is_never_auto_merged_even_with_green_lint():
    policy = _wiki_policy()
    pr = _pr(repo=policy.repo, files=(ChangedFile("wiki/concepts/context-engine.md", "modified"),), lint=_green())
    decision = _decide(pr, policy)
    assert decision.action == "pr_review"
    assert "T1" in decision.reason


@pytest.mark.parametrize(
    "path",
    ["wiki/index.md", "wiki/log.md", "wiki/log-2026.md", "wiki/MOC/ai.md", "daily/2026-09-14.md"],
)
def test_navigation_t3_auto_merges_with_green_lint(path):
    policy = _wiki_policy()
    pr = _pr(repo=policy.repo, files=(ChangedFile(path, "modified"),), lint=_green())
    assert _decide(pr, policy).action == "merge"


@pytest.mark.parametrize("path", ["estudos/ce/nota.md", "pesquisa/tcc/nota.md", "brand/posts/post.md"])
def test_working_notes_t2_auto_merge_with_green_lint(path):
    policy = _wiki_policy()
    pr = _pr(repo=policy.repo, files=(ChangedFile(path, "added"),), lint=_green())
    assert _decide(pr, policy).action == "merge"


def test_a_wiki_path_in_no_known_tier_blocks_instead_of_defaulting_to_mergeable():
    policy = _wiki_policy(allowed_paths=("**",))
    pr = _pr(repo=policy.repo, files=(ChangedFile("scripts/lint.py", "modified"),), lint=_green())
    decision = _decide(pr, policy)
    assert decision.action == "block"
    assert "outside any known tier" in decision.reason


def test_new_fontes_ingest_is_t2_but_editing_an_existing_source_is_t0():
    policy = _wiki_policy()
    ingest = _pr(
        repo=policy.repo,
        files=(ChangedFile("estudos/ce/fontes/2026-09-14-artigo.md", "added"),),
        lint=_green(),
    )
    assert _decide(ingest, policy).action == "merge"

    edit = _pr(
        repo=policy.repo,
        files=(ChangedFile("estudos/ce/fontes/2026-09-14-artigo.md", "modified"),),
        lint=_green(),
    )
    assert _decide(edit, policy).action == "suggestion_only"


def test_deleting_a_file_in_the_vault_is_t0():
    policy = _wiki_policy()
    pr = _pr(repo=policy.repo, files=(ChangedFile("estudos/ce/nota.md", "removed"),), lint=_green())
    assert _decide(pr, policy).action == "suggestion_only"


def test_authority_files_are_t0_at_any_nesting_level():
    policy = _wiki_policy(allowed_paths=("**",))
    for path in ("CLAUDE.md", "AGENTS.md", "estudos/ce/AGENTS.md", "wiki/principles/x.md"):
        pr = _pr(repo=policy.repo, files=(ChangedFile(path, "modified"),), lint=_green())
        assert _decide(pr, policy).action == "suggestion_only", path


def test_t0_wins_over_protected_so_the_owner_sees_it_as_a_suggestion():
    """A T0 file that is also protected must still read as 'suggestion only'."""
    policy = _wiki_policy(allowed_paths=("**",), protected_paths=("wiki/principles/**",))
    pr = _pr(repo=policy.repo, files=(ChangedFile("wiki/principles/x.md", "modified"),), lint=_green())
    decision = _decide(pr, policy)
    assert decision.action == "suggestion_only"


def test_a_mixed_pr_with_one_t0_file_is_suggestion_only_for_the_whole_pr():
    policy = _wiki_policy(allowed_paths=("**",))
    pr = _pr(
        repo=policy.repo,
        files=(ChangedFile("daily/2026-09-14.md", "modified"), ChangedFile("CLAUDE.md", "modified")),
        lint=_green(),
    )
    assert _decide(pr, policy).action == "suggestion_only"


# --- finding 1c: the allowlist applies to every path, in every repo ---------


def test_wiki_path_outside_the_allowlist_is_blocked_not_auto_merged():
    policy = _wiki_policy(allowed_paths=("daily/**",))
    pr = _pr(repo=policy.repo, files=(ChangedFile("estudos/ce/nota.md", "added"),), lint=_green())
    decision = _decide(pr, policy)
    assert decision.action == "block"
    assert "allowlist" in decision.reason


def test_allowlist_star_does_not_cross_directory_boundaries():
    """`daily/*` must not admit `daily/sub/deep.md`."""
    assert matches_any("daily/2026-09-14.md", ("daily/*",))
    assert not matches_any("daily/sub/deep.md", ("daily/*",))
    assert matches_any("daily/sub/deep.md", ("daily/**",))


def test_policy_for_another_repo_never_applies():
    pr = _pr(repo="MathBorgess/some-other-repo")
    assert _decide(pr, _code_policy()).action == "block"


def test_protected_path_leaves_the_pr_open_for_the_owner():
    policy = _code_policy(protected_paths=("src/reports/swarm_reports/dispatch/claims.py",))
    pr = _pr(files=(ChangedFile("src/reports/swarm_reports/dispatch/claims.py", "modified"),))
    decision = _decide(pr, policy)
    assert decision.action == "pr_review"
    assert "protected path" in decision.reason


def test_protected_path_blocks_the_merge_even_when_only_one_file_of_many_is_protected():
    policy = _code_policy(protected_paths=("src/reports/secrets/**",))
    pr = _pr(
        files=(
            ChangedFile("src/reports/swarm_reports/dispatch/quota.py", "modified"),
            ChangedFile("src/reports/secrets/keys.py", "modified"),
        )
    )
    assert _decide(pr, policy).action == "pr_review"


# --- finding 1d: renames touch both sides -----------------------------------


def test_rename_out_of_a_protected_directory_is_still_a_protected_write():
    policy = _code_policy(
        allowed_paths=("src/reports/**",), protected_paths=("src/reports/swarm_reports/dispatch/claims.py",)
    )
    pr = _pr(
        files=(
            ChangedFile(
                path="src/reports/swarm_reports/dispatch/claims_old.py",
                status="renamed",
                previous_filename="src/reports/swarm_reports/dispatch/claims.py",
            ),
        )
    )
    assert _decide(pr, policy).action == "pr_review"


def test_rename_from_outside_the_allowlist_is_blocked():
    policy = _code_policy(allowed_paths=("src/reports/**",))
    pr = _pr(
        files=(
            ChangedFile(
                path="src/reports/moved.py", status="renamed", previous_filename="src/auth-broker/secret.py"
            ),
        )
    )
    decision = _decide(pr, policy)
    assert decision.action == "block" and "allowlist" in decision.reason


def test_rename_of_an_authority_file_is_still_t0():
    policy = _wiki_policy(allowed_paths=("**",))
    pr = _pr(
        repo=policy.repo,
        files=(ChangedFile(path="estudos/ce/nota.md", status="renamed", previous_filename="AGENTS.md"),),
        lint=_green(),
    )
    assert _decide(pr, policy).action == "suggestion_only"


def test_plain_rename_inside_the_allowlist_still_merges():
    pr = _pr(
        files=(
            ChangedFile(
                path="src/reports/swarm_reports/dispatch/new_name.py",
                status="renamed",
                previous_filename="src/reports/swarm_reports/dispatch/old_name.py",
            ),
        )
    )
    assert _decide(pr, _code_policy()).action == "merge"


# --- finding 1e: malformed paths are rejected, not repaired -----------------


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "src/reports/../../etc/passwd",
        "../outside.py",
        "src\\reports\\win.py",
        "C:\\Windows\\system32",
        "src/reports/./quota.py",
        "src/reports//quota.py",
        "src/reports/",
        "",
        "   ",
        "src/reports/qu\nota.py",
    ],
)
def test_malformed_paths_are_rejected_before_any_allowlist_match(bad):
    with pytest.raises(MalformedPath):
        normalize_path(bad)
    decision = _decide(_pr(files=(ChangedFile(bad, "modified"),)), _code_policy(allowed_paths=("**",)))
    assert decision.action == "block"
    assert "malformed path" in decision.reason


def test_a_traversal_that_would_normalize_into_the_allowlist_is_still_rejected():
    """`src/reports/../reports/x.py` normalizes into the allowlist. Reject it anyway."""
    decision = _decide(
        _pr(files=(ChangedFile("src/reports/../reports/x.py", "modified"),)), _code_policy()
    )
    assert decision.action == "block" and "malformed path" in decision.reason


def test_a_malformed_previous_filename_is_rejected_too():
    pr = _pr(
        files=(ChangedFile(path="src/reports/ok.py", status="renamed", previous_filename="/etc/passwd"),)
    )
    assert _decide(pr, _code_policy()).action == "block"


# --- finding 2: tau-intent counts from the manual cycle, not the PR ---------


def test_tau_intent_without_a_known_activation_date_blocks():
    policy = _code_policy(repo="MathBorgess/tau-intent", allowed_paths=("**",), is_tau_intent=True)
    pr = _pr(repo="MathBorgess/tau-intent", files=(ChangedFile("app/main.py", "modified"),))
    decision = _decide(pr, policy, tau_intent=None)
    assert decision.action == "block"
    assert "activation date unknown" in decision.reason


def test_tau_intent_hold_is_counted_from_activation_not_from_pr_age():
    """A PR opened 30 days ago does not escape a cycle activated 3 days ago."""
    policy = _code_policy(repo="MathBorgess/tau-intent", allowed_paths=("**",), is_tau_intent=True)
    pr = _pr(
        repo="MathBorgess/tau-intent",
        files=(ChangedFile("app/main.py", "modified"),),
        opened_on=TODAY - timedelta(days=30),
    )
    state = TauIntentState(activated_on=TODAY - timedelta(days=3), digest_proved=True, digest_evidence="cards")
    decision = _decide(pr, policy, tau_intent=state)
    assert decision.action == "pr_review"
    assert "day 3" in decision.reason


def test_tau_intent_after_the_hold_still_requires_digest_evidence():
    policy = _code_policy(repo="MathBorgess/tau-intent", allowed_paths=("**",), is_tau_intent=True)
    pr = _pr(repo="MathBorgess/tau-intent", files=(ChangedFile("app/main.py", "modified"),))
    state = TauIntentState(activated_on=TODAY - timedelta(days=20), digest_proved=False)
    decision = _decide(pr, policy, tau_intent=state)
    assert decision.action == "pr_review"
    assert "digest evidence" in decision.reason


def test_tau_intent_digest_evidence_must_be_more_than_a_true_flag():
    policy = _code_policy(repo="MathBorgess/tau-intent", allowed_paths=("**",), is_tau_intent=True)
    pr = _pr(repo="MathBorgess/tau-intent", files=(ChangedFile("app/main.py", "modified"),))
    state = TauIntentState(activated_on=TODAY - timedelta(days=20), digest_proved=True, digest_evidence="  ")
    assert _decide(pr, policy, tau_intent=state).action == "pr_review"


def test_tau_intent_merges_only_after_hold_plus_digest_plus_normal_gates():
    policy = _code_policy(repo="MathBorgess/tau-intent", allowed_paths=("app/**",), is_tau_intent=True)
    pr = _pr(repo="MathBorgess/tau-intent", files=(ChangedFile("app/main.py", "modified"),))
    state = TauIntentState(
        activated_on=TODAY - timedelta(days=20), digest_proved=True, digest_evidence="digest cards 2026-09-10..14"
    )
    assert _decide(pr, policy, tau_intent=state).action == "merge"

    outside = _pr(repo="MathBorgess/tau-intent", files=(ChangedFile("infra/deploy.sh", "modified"),))
    assert _decide(outside, policy, tau_intent=state).action == "block"


# --- skills and RES weights --------------------------------------------------


def test_skills_are_always_draft_only():
    policy = _code_policy(allowed_paths=("skills/**",))
    pr = _pr(files=(ChangedFile("skills/daily-plan/SKILL.md", "modified"),))
    assert _decide(pr, policy).action == "draft"


def test_res_weights_file_in_the_vault_is_draft_not_a_t2_auto_merge():
    policy = _wiki_policy(weights_paths=("brand/metrics/res-weights.yaml",))
    pr = _pr(repo=policy.repo, files=(ChangedFile("brand/metrics/res-weights.yaml", "modified"),), lint=_green())
    assert _decide(pr, policy).action == "draft"


def test_skills_draft_still_loses_to_a_t0_file_in_the_same_pr():
    policy = _code_policy(allowed_paths=("**",))
    pr = _pr(files=(ChangedFile("skills/daily-plan/SKILL.md", "modified"), ChangedFile("CLAUDE.md", "modified")))
    assert _decide(pr, policy).action == "suggestion_only"


# --- classification unit checks ---------------------------------------------


def test_classify_path_buckets_in_the_wiki_repo():
    kind = "wiki"
    assert classify_path("CLAUDE.md", kind=kind) == "t0"
    assert classify_path("wiki/principles/foo.md", kind=kind) == "t0"
    assert classify_path("estudos/ce/fontes/a.md", kind=kind, status="modified") == "t0"
    assert classify_path("estudos/ce/fontes/a.md", kind=kind, status="added") == "t2"
    assert classify_path("wiki/concepts/foo.md", kind=kind) == "t1"
    assert classify_path("wiki/index.md", kind=kind) == "t3"
    assert classify_path("daily/2026-09-14.md", kind=kind) == "t3"
    assert classify_path("estudos/ce/nota.md", kind=kind) == "t2"
    assert classify_path("Makefile", kind=kind) == "unknown"


def test_classify_path_buckets_in_a_code_repo():
    policy = _code_policy()
    assert classify_path("src/reports/x.py", kind="code", policy=policy) == "code"
    assert classify_path("skills/daily-plan/SKILL.md", kind="code", policy=policy) == "skills_weights"
    assert classify_path("AGENTS.md", kind="code", policy=policy) == "t0"
