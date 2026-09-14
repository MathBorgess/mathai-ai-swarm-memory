"""Merge autonomy, per the design note's autonomy table.

| Alvo                                   | Autonomia                          |
|-----------------------------------------|-------------------------------------|
| wiki T2/T3 (notas, métricas, navegação) | auto-merge se lint verde           |
| skills do mecanismo e pesos do RES      | PR draft, nunca merge automático   |
| CLAUDE.md, AGENTS.md, wiki/principles/  | só sugestão em texto (T0)          |
| repos de código na allowlist            | merge com CI verde, exceto paths protegidos |
| tau-intent                              | só PR nas duas primeiras semanas   |

Fails closed: any missing signal (no checks, no green, pending) blocks the
merge rather than defaulting to allow.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from datetime import date, timedelta
from fnmatch import fnmatch
from typing import Literal

Action = Literal["merge", "draft", "block", "suggestion_only"]
ChecksStatus = Literal["success", "failure", "pending", "none"]

TAU_INTENT_MERGE_HOLD = timedelta(days=14)

_T0_PATH_PREFIXES = ("CLAUDE.md", "AGENTS.md", "wiki/principles/")
_SKILLS_PATH_PREFIXES = ("skills/", "brand/metrics/")
_WIKI_T2_T3_PREFIX = "wiki/"


@dataclass(frozen=True)
class RepoPolicy:
    repo: str
    allowed_paths: tuple[str, ...]  # glob patterns, relative to repo root
    protected_paths: tuple[str, ...] = ()
    is_tau_intent: bool = False


@dataclass(frozen=True)
class PullRequest:
    repo: str
    head_sha: str
    base_branch: str
    changed_paths: tuple[str, ...]
    checks_status: ChecksStatus
    opened_on: date


@dataclass(frozen=True)
class MergeDecision:
    action: Action
    reason: str


def _normalize(path: str) -> str:
    """Collapse '..'/'.' segments so a crafted path cannot escape the allowlist."""
    normalized = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if normalized.startswith(".."):
        raise ValueError(f"path escapes repo root: {path!r}")
    return normalized


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def classify_path(path: str) -> Literal["t0", "skills_weights", "wiki", "code"]:
    normalized = _normalize(path)
    if any(normalized == p or normalized.startswith(p) for p in _T0_PATH_PREFIXES):
        return "t0"
    if any(normalized.startswith(p) for p in _SKILLS_PATH_PREFIXES):
        return "skills_weights"
    if normalized.startswith(_WIKI_T2_T3_PREFIX):
        return "wiki"
    return "code"


def evaluate_merge(
    pr: PullRequest,
    policy: RepoPolicy,
    *,
    current_head_sha: str,
    today: date,
    lint_green: bool = True,
) -> MergeDecision:
    """Fail-closed merge decision. Never returns "merge" on ambiguous input."""
    if pr.repo != policy.repo:
        return MergeDecision("block", f"repo {pr.repo!r} not covered by this policy")

    if pr.head_sha != current_head_sha:
        return MergeDecision("block", "head_sha stale, recheck required before merge")

    try:
        normalized_changed = tuple(_normalize(p) for p in pr.changed_paths)
    except ValueError as exc:
        return MergeDecision("block", str(exc))

    if not normalized_changed:
        return MergeDecision("block", "no changed paths reported")

    for path in normalized_changed:
        if _matches_any(path, policy.protected_paths):
            return MergeDecision("draft", f"protected path: {path}")

    categories = {classify_path(p) for p in normalized_changed}

    if "t0" in categories:
        return MergeDecision("suggestion_only", "touches T0 (CLAUDE.md/AGENTS.md/wiki/principles)")

    if "skills_weights" in categories:
        return MergeDecision("draft", "skills or RES weights: PR draft only, never auto-merge")

    if policy.is_tau_intent and today - pr.opened_on < TAU_INTENT_MERGE_HOLD:
        return MergeDecision("draft", "tau-intent: PR only for the first 2 weeks, even with green CI")

    if categories == {"wiki"}:
        if not lint_green:
            return MergeDecision("draft", "wiki lint not green")
        return MergeDecision("merge", "wiki T2/T3, lint green")

    if not all(_matches_any(p, policy.allowed_paths) for p in normalized_changed):
        return MergeDecision("block", "changed path outside repo allowlist")

    if pr.checks_status != "success":
        return MergeDecision("block", f"checks not green (status={pr.checks_status})")

    return MergeDecision("merge", "allowlisted path, CI green, head matches")
