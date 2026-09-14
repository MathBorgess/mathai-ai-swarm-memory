"""Merge autonomy, per the design note's autonomy table and the vault's tiers.

Design note (`2026-09-13-daily-reports-ciclo-self-improvement.md`, "Autonomia"):

| Alvo                                    | O que acontece sem o dono                  |
|-----------------------------------------|--------------------------------------------|
| Wiki T2/T3 (notas, métricas, navegação) | PR com auto-merge se o lint passar         |
| Skills do mecanismo e pesos do RES      | PR draft                                   |
| `CLAUDE.md`, `AGENTS.md`, `wiki/principles/` | Só sugestão em texto                  |
| Repos de código na allowlist            | Merge com CI verde, exceto caminhos protegidos, que viram PR sem merge |
| `tau-intent`                            | Só PR nas duas primeiras semanas; merge só depois de o digest provar que funciona |

"Wiki T2/T3" is not "anything under `wiki/`". The vault's own tier table
(`mathai-wiki/CLAUDE.md`, § Tiers) is the authority, and it cuts across
directories, file status and repo:

- **T0** — `wiki/principles/`, `CLAUDE.md`, `AGENTS.md`, *removing a file*,
  and *any touch to an existing file under a* `fontes/` *directory*
  (ingest of a **new** source file is T2; editing one is never automatic).
- **T1** — compiled wiki: `wiki/concepts/`, `wiki/entities/`,
  `wiki/case-studies/`. The owner reads the claims. Not auto-mergeable.
- **T2** — working notes: `estudos/`, `pesquisa/`, `brand/`, new `fontes/`
  ingest. **T3** — navigation: `wiki/index.md`, `wiki/log*.md`,
  `wiki/MOC/`, `daily/`.

Everything fails closed. A signal that is missing, stale, or attached to a
different commit blocks the merge; it never defaults to "allow".
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Sequence

Action = Literal["merge", "draft", "pr_review", "block", "suggestion_only"]
"""``pr_review`` = leave the PR open for the owner; it is not a draft conversion.
The design note says protected paths "viram PR sem merge", which is a different
outcome from the explicit *draft* the table reserves for skills and RES weights.
"""

CheckState = Literal["success", "failure", "pending", "none", "error"]
FileStatus = Literal["added", "modified", "removed", "renamed", "copied", "changed"]
RepoKind = Literal["wiki", "code"]
Tier = Literal["t0", "t1", "t2", "t3", "skills_weights", "code", "unknown"]

TAU_INTENT_MERGE_HOLD = timedelta(days=14)
DEFAULT_SIGNAL_MAX_AGE = timedelta(hours=6)

_AUTHORITY_BASENAMES = ("CLAUDE.md", "AGENTS.md")
_WIKI_T0_PREFIXES = ("wiki/principles/",)
_WIKI_T1_PREFIXES = ("wiki/concepts/", "wiki/entities/", "wiki/case-studies/")
_WIKI_T3_PREFIXES = ("wiki/MOC/", "daily/")
_WIKI_T3_FILES = ("wiki/index.md", "wiki/log.md")
_WIKI_T3_PATTERNS = (re.compile(r"^wiki/log-\d{4}\.md$"),)
_WIKI_T2_PREFIXES = ("estudos/", "pesquisa/", "brand/")
_DEFAULT_SKILLS_PREFIXES = ("skills/**",)

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\- /]*$")


class MalformedPath(ValueError):
    """A changed path is not a plain repo-relative POSIX path."""


@dataclass(frozen=True)
class ChangedFile:
    """One entry of the PR's file list, as GitHub reports it.

    ``previous_filename`` is populated for renames. Both sides are classified
    and both must clear the allowlist: renaming a protected file out of its
    directory is a write to the protected file.
    """

    path: str
    status: FileStatus = "modified"
    previous_filename: str | None = None


@dataclass(frozen=True)
class CheckSignal:
    """A CI or lint verdict, bound to the commit it was produced for.

    A bare boolean supplied by the planner is not evidence: it carries no
    commit and no timestamp, so a green from three commits ago reads exactly
    like a green from this one.
    """

    state: CheckState
    head_sha: str
    observed_at: datetime
    required_contexts_met: bool = True
    detail: str = ""


@dataclass(frozen=True)
class RepoPolicy:
    repo: str
    kind: RepoKind
    allowed_paths: tuple[str, ...]  # repo-relative globs; applies to every path
    protected_paths: tuple[str, ...] = ()
    is_tau_intent: bool = False
    skills_paths: tuple[str, ...] = _DEFAULT_SKILLS_PREFIXES
    weights_paths: tuple[str, ...] = ()  # RES weights file(s) in the vault


@dataclass(frozen=True)
class PullRequest:
    repo: str
    head_sha: str
    base_branch: str
    files: tuple[ChangedFile, ...]
    opened_on: date
    checks: CheckSignal | None = None
    lint: CheckSignal | None = None


@dataclass(frozen=True)
class TauIntentState:
    """The manual-cycle facts the tau-intent rule actually depends on.

    The two-week hold is counted from the day the owner activated the manual
    cycle — not from when a PR happened to be opened, which an agent can set
    to any date by opening the PR later.
    """

    activated_on: date | None = None
    digest_proved: bool = False
    digest_evidence: str = ""


@dataclass(frozen=True)
class MergeDecision:
    action: Action
    reason: str
    tiers: tuple[Tier, ...] = ()


def normalize_path(path: str) -> str:
    """Return a repo-relative POSIX path, or raise MalformedPath.

    Rejects rather than repairs. Silently rewriting ``..\\..\\etc`` into
    something that matches an allowlist entry is how a traversal becomes a
    merge; and on POSIX a backslash is a legal filename character, so
    "fixing" it to ``/`` invents a path the repo does not contain.
    """
    if not isinstance(path, str) or not path.strip():
        raise MalformedPath("empty path")
    if "\x00" in path or any(ord(ch) < 0x20 for ch in path):
        raise MalformedPath(f"control character in path: {path!r}")
    if path.startswith("/"):
        raise MalformedPath(f"absolute path: {path!r}")
    if re.match(r"^[A-Za-z]:[\\/]", path):
        raise MalformedPath(f"drive-letter path: {path!r}")
    if "\\" in path:
        raise MalformedPath(f"backslash in path: {path!r}")
    if not _SAFE_PATH_RE.match(path):
        raise MalformedPath(f"unsupported characters in path: {path!r}")
    if posixpath.normpath(path) != path.rstrip("/") or path.endswith("/"):
        raise MalformedPath(f"non-canonical path: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise MalformedPath(f"path escapes or is non-canonical: {path!r}")
    return path


def _match(path: str, pattern: str) -> bool:
    """Glob match where ``*`` stops at ``/`` and ``**`` crosses directories.

    ``fnmatch`` treats ``*`` as matching ``/`` too, so an allowlist entry of
    ``daily/*`` would also admit ``daily/anything/deep.md``.
    """
    regex = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            regex.append(r"(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            regex.append(r".*")
            i += 2
        elif ch == "*":
            regex.append(r"[^/]*")
            i += 1
        elif ch == "?":
            regex.append(r"[^/]")
            i += 1
        else:
            regex.append(re.escape(ch))
            i += 1
    regex.append("$")
    return re.match("".join(regex), path) is not None


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_match(path, pattern) for pattern in patterns)


def _under(path: str, prefixes: Sequence[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _is_authority_file(path: str) -> bool:
    """`CLAUDE.md`/`AGENTS.md` are T0 at any nesting level, not only at the root."""
    return posixpath.basename(path) in _AUTHORITY_BASENAMES


def _in_fontes(path: str) -> bool:
    return "/fontes/" in f"/{path}"


def classify_path(path: str, *, kind: RepoKind, status: FileStatus = "modified", policy: RepoPolicy | None = None) -> Tier:
    """Tier for one path. Unknown territory returns ``unknown`` (fails closed)."""
    path = normalize_path(path)

    if _is_authority_file(path):
        return "t0"
    if policy is not None and matches_any(path, tuple(policy.weights_paths) + tuple(policy.skills_paths)):
        return "skills_weights"

    if kind == "code":
        return "code"

    # --- wiki repo: mathai-wiki/CLAUDE.md § Tiers ---
    if status == "removed":
        return "t0"  # "remoção de arquivo" is T0 regardless of where it lives
    if _under(path, _WIKI_T0_PREFIXES):
        return "t0"
    if _in_fontes(path):
        # ingest of a new source is T2; any edit to an existing source is T0
        return "t2" if status in ("added", "copied") else "t0"
    if _under(path, _WIKI_T1_PREFIXES):
        return "t1"
    if path in _WIKI_T3_FILES or _under(path, _WIKI_T3_PREFIXES):
        return "t3"
    if any(pattern.match(path) for pattern in _WIKI_T3_PATTERNS):
        return "t3"
    if _under(path, _WIKI_T2_PREFIXES):
        return "t2"
    if path.startswith("wiki/"):
        return "t1"  # compiled area not otherwise named: owner reads it
    return "unknown"


def _effective_paths(file: ChangedFile) -> list[tuple[str, FileStatus]]:
    """Both sides of a rename; a rename is a write to the old path too."""
    pairs: list[tuple[str, FileStatus]] = [(normalize_path(file.path), file.status)]
    if file.previous_filename:
        pairs.append((normalize_path(file.previous_filename), "removed"))
    return pairs


def _signal_ok(
    signal: CheckSignal | None,
    *,
    current_head_sha: str,
    now: datetime,
    max_age: timedelta,
    label: str,
) -> str | None:
    """Return a blocking reason, or None if the signal is green and current."""
    if signal is None:
        return f"{label} signal missing"
    if not signal.head_sha or signal.head_sha != current_head_sha:
        return f"{label} was produced for {signal.head_sha or '<empty>'}, not the current head"
    if signal.state != "success":
        return f"{label} not green (state={signal.state})"
    if not signal.required_contexts_met:
        return f"{label} green but required contexts are not all reported"
    age = now - signal.observed_at
    if age < timedelta(0):
        return f"{label} timestamp is in the future"
    if age > max_age:
        return f"{label} is stale ({age} old, max {max_age})"
    return None


def evaluate_merge(
    pr: PullRequest,
    policy: RepoPolicy,
    *,
    current_head_sha: str,
    now: datetime,
    tau_intent: TauIntentState | None = None,
    signal_max_age: timedelta = DEFAULT_SIGNAL_MAX_AGE,
) -> MergeDecision:
    """Fail-closed merge decision. Never returns "merge" on ambiguous input."""
    today = now.date()

    if pr.repo != policy.repo:
        return MergeDecision("block", f"repo {pr.repo!r} not covered by this policy")
    if not pr.head_sha or not current_head_sha:
        return MergeDecision("block", "head sha unknown on one side; refusing to merge blind")
    if pr.head_sha != current_head_sha:
        return MergeDecision("block", "head_sha stale, recheck required before merge")
    if not pr.files:
        return MergeDecision("block", "no changed files reported")

    try:
        pairs: list[tuple[str, FileStatus]] = []
        for file in pr.files:
            pairs.extend(_effective_paths(file))
    except MalformedPath as exc:
        return MergeDecision("block", f"malformed path rejected: {exc}")

    tiers = tuple(
        classify_path(path, kind=policy.kind, status=status, policy=policy) for path, status in pairs
    )
    paths = [path for path, _ in pairs]

    # T0 first: a suggestion-only target stays suggestion-only even when it is
    # also protected or outside the allowlist. Reporting it as "protected" would
    # hide that the owner must read the text line by line.
    if "t0" in tiers:
        return MergeDecision(
            "suggestion_only",
            "touches T0 (authority file, wiki/principles, deletion, or an existing fontes/ file)",
            tiers,
        )

    if "unknown" in tiers:
        unknown = [p for p, t in zip(paths, tiers) if t == "unknown"]
        return MergeDecision("block", f"path outside any known tier: {unknown[0]}", tiers)

    # The allowlist applies to every path, in every repo, before any autonomy.
    outside = [p for p in paths if not matches_any(p, policy.allowed_paths)]
    if outside:
        return MergeDecision("block", f"changed path outside repo allowlist: {outside[0]}", tiers)

    protected = [p for p in paths if matches_any(p, policy.protected_paths)]
    if protected:
        return MergeDecision("pr_review", f"protected path, PR stays open for the owner: {protected[0]}", tiers)

    if policy.is_tau_intent:
        state = tau_intent or TauIntentState()
        if state.activated_on is None:
            return MergeDecision("block", "tau-intent: manual-cycle activation date unknown", tiers)
        if today - state.activated_on < TAU_INTENT_MERGE_HOLD:
            days = (today - state.activated_on).days
            return MergeDecision(
                "pr_review", f"tau-intent: PR only, day {days} of the {TAU_INTENT_MERGE_HOLD.days}-day manual cycle", tiers
            )
        if not state.digest_proved or not state.digest_evidence.strip():
            return MergeDecision(
                "pr_review",
                "tau-intent: hold elapsed, but merge still waits on digest evidence that it works",
                tiers,
            )

    if "skills_weights" in tiers:
        return MergeDecision("draft", "skills or RES weights: PR draft only, never auto-merge", tiers)

    if "t1" in tiers:
        return MergeDecision("pr_review", "compiled wiki (T1): the owner reads the claims", tiers)

    if policy.kind == "wiki":
        reason = _signal_ok(pr.lint, current_head_sha=current_head_sha, now=now, max_age=signal_max_age, label="lint")
        if reason:
            return MergeDecision("block", f"wiki T2/T3: {reason}", tiers)
        return MergeDecision("merge", "wiki T2/T3, lint green on this head", tiers)

    reason = _signal_ok(pr.checks, current_head_sha=current_head_sha, now=now, max_age=signal_max_age, label="checks")
    if reason:
        return MergeDecision("block", reason, tiers)
    return MergeDecision("merge", "allowlisted path, CI green on this head", tiers)


__all__ = [
    "Action",
    "ChangedFile",
    "CheckSignal",
    "MalformedPath",
    "MergeDecision",
    "PullRequest",
    "RepoPolicy",
    "TauIntentState",
    "classify_path",
    "evaluate_merge",
    "matches_any",
    "normalize_path",
]
