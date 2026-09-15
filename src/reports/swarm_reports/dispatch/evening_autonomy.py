"""Evening PR autonomy: digest cards, merge policy, optional gh merge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

from swarm_reports.dispatch.digest import (
    DECLARED_SECTION_HEADING,
    DecisionCard,
    independent_cards,
    merge_cards,
    parse_declared_cards,
    top_n_global,
    verify_locations,
    review_diff_locally,
)
from swarm_reports.dispatch.gh_adapter import GhTransport, LocalReceipt, apply_decision
from swarm_reports.dispatch.gh_cli import GhCliTransport
from swarm_reports.dispatch.merge_policy import (
    ChangedFile,
    CheckSignal,
    PullRequest,
    evaluate_merge,
)
from swarm_reports.dispatch.policy_config import DispatchPolicy
from swarm_reports.dispatch.runtime_store import load_runtime, save_runtime


_PR_URL_RE = re.compile(r"https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<num>\d+)")


@dataclass(frozen=True)
class EveningAutonomyResult:
    cards: list[DecisionCard]
    merge_attempted: bool
    merge_applied: bool
    receipts: list[LocalReceipt]
    detail: str


def parse_pr_url(url: str) -> tuple[str, int] | None:
    match = _PR_URL_RE.match(url.strip())
    if not match:
        return None
    return match.group("repo"), int(match.group("num"))


def format_pr_body_with_cards(base: str, cards: Iterable[DecisionCard]) -> str:
    lines = [base.rstrip(), "", f"## {DECLARED_SECTION_HEADING}", ""]
    for card in cards:
        lines.append(
            f"- tipo: {card.kind}, {card.location}, pergunta: {card.question}, "
            f"por que importa: {card.why}"
        )
    return "\n".join(lines).strip() + "\n"


def _files_from_gh(files_json: list) -> tuple[ChangedFile, ...]:
    out: list[ChangedFile] = []
    for entry in files_json or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("filename") or entry.get("path") or "")
        status = str(entry.get("changeType") or entry.get("status") or "modified").lower()
        if status not in ("added", "modified", "removed", "renamed", "copied", "changed"):
            status = "modified"
        prev = entry.get("previous_filename") or entry.get("previousFilename")
        out.append(
            ChangedFile(
                path=path,
                status=status,  # type: ignore[arg-type]
                previous_filename=str(prev) if prev else None,
            )
        )
    return tuple(out)


def _lint_signal(head_sha: str, lint_ok: bool | None, now: datetime) -> CheckSignal | None:
    if lint_ok is None:
        return None
    return CheckSignal(
        state="success" if lint_ok else "failure",
        head_sha=head_sha,
        observed_at=now,
        required_contexts_met=True,
        detail="vault lint",
    )


def process_evening_pr(
    *,
    policy: DispatchPolicy,
    state_dir,
    pr_url: str,
    lint_ok: bool | None,
    gh: GhCliTransport | GhTransport,
    independent_reviewer=None,
    lint_head_sha: str | None = None,
    now: datetime | None = None,
) -> EveningAutonomyResult:
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return EveningAutonomyResult([], False, False, [], "not a github pr url")
    repo, number = parsed
    repo_policy = policy.repo_policy(repo)
    if repo_policy is None:
        return EveningAutonomyResult([], False, False, [], f"repo {repo} not on allowlist")

    now = now or datetime.now(timezone.utc)
    view: dict = {}
    if hasattr(gh, "pr_view_json"):
        view = gh.pr_view_json(repo, number)  # type: ignore[attr-defined]
    head = str(view.get("headRefOid") or "")
    body = str(view.get("body") or "")
    files = _files_from_gh(view.get("files") or [])

    cards = collect_pr_digest(policy, state_dir, repo, number, gh, view=view,
                              independent_reviewer=independent_reviewer)

    pr = PullRequest(
        repo=repo,
        head_sha=head,
        base_branch=str(view.get("baseRefName") or "main"),
        files=files,
        opened_on=date.today(),
        lint=_lint_signal(head, lint_ok if lint_head_sha == head else None, now),
        checks=_checks_from_view(view, head, now),
    )
    decision = evaluate_merge(
        pr,
        repo_policy,
        tau_intent=policy.tau_intent,
        signal_max_age=timedelta(hours=policy.signal_max_age_hours),
        now=now,
        current_head_sha=head,
    )
    receipts: list[LocalReceipt] = []
    applied = apply_decision(
        number,
        pr,
        decision,
        gh,
        allow_external_comment=policy.digest.allow_external_comments,
        receipts=receipts,
    )
    return EveningAutonomyResult(
        cards=cards,
        merge_attempted=decision.action == "merge",
        merge_applied=applied,
        receipts=receipts,
        detail=decision.reason,
    )


class StaticDiffReviewer:
    """Test helper implementing DiffReviewer with a fixed card list."""

    def __init__(self, cards: Iterable[Mapping[str, str]]) -> None:
        self._cards = list(cards)

    def __call__(self, diff_text: str) -> Iterable[Mapping[str, str]]:
        return self._cards


__all__ = [
    "EveningAutonomyResult",
    "StaticDiffReviewer",
    "format_pr_body_with_cards",
    "parse_pr_url",
    "process_evening_pr",
]


def _checks_from_view(view, head, now):
    checks = view.get("requiredChecks") or []
    if not checks or any(c.get("head_sha") != head for c in checks):
        return None
    success = all(c.get("conclusion") == "success" for c in checks)
    return CheckSignal(state="success" if success else "failure", head_sha=head,
        observed_at=now, required_contexts_met=success, detail="required GitHub checks at current head")


def collect_pr_digest(policy, state_dir, repo, number, gh, *, view=None, independent_reviewer=None):
    view = view if view is not None else gh.pr_view_json(repo, number)
    body = str(view.get("body") or "")
    diff = gh.pr_diff(repo, number)
    reviewer = independent_reviewer or review_diff_locally
    independent = independent_cards(diff, reviewer) if diff else []
    cards = verify_locations(merge_cards(parse_declared_cards(body), independent,
        max_per_pr=policy.digest.max_cards_per_pr), diff)
    from dataclasses import asdict
    from swarm_reports.dispatch.statefile import file_lock
    pr_key = f"{repo}#{number}"
    with file_lock(state_dir / "dispatch-runtime.lock"):
        runtime = load_runtime(state_dir)
        runtime.digest_cards = [c for c in runtime.digest_cards if c.get("pr") != pr_key] + [
            {**asdict(c), "pr": pr_key} for c in cards]
        save_runtime(state_dir, runtime)
    return cards
