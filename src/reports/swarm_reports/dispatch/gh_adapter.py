"""GitHub adapter: turns a MergeDecision into one authorized transport call.

The transport is injected (``GhTransport``) so tests never make a real API or
``gh`` CLI call. F5-integration wires a real transport later; this module only
prepares the request and the exact argv it would run.

Two boundaries this module enforces:

- **A merge is always head-pinned.** The request carries
  ``expected_head_oid`` and the argv carries ``--match-head-commit``, so a
  commit that lands between the policy check and the API call aborts the
  merge instead of being merged unreviewed. Without it, "checks were green"
  is a statement about a commit that may no longer be the head.
- **Nothing is written to an external surface by default.** A
  ``suggestion_only`` outcome produces a *local receipt* for the ledger, not
  a PR comment. Posting a comment is an outward-facing action on a surface
  the owner shares with other people; it needs explicit authorization
  (``allow_external_comment=True``), which the caller only sets when the
  owner asked for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .merge_policy import MergeDecision, PullRequest

GhOp = Literal["merge", "convert_to_draft", "comment"]


@dataclass(frozen=True)
class GhRequest:
    repo: str
    pr_number: int
    op: GhOp
    expected_head_oid: str | None = None
    body: str | None = None

    def __post_init__(self) -> None:
        if self.op == "merge" and not self.expected_head_oid:
            raise ValueError("a merge request must pin expected_head_oid")


@dataclass(frozen=True)
class LocalReceipt:
    """A suggestion recorded locally for the ledger; nothing leaves the box."""

    repo: str
    pr_number: int
    kind: str
    text: str
    published: bool = False


@dataclass(frozen=True)
class PreparedAction:
    request: GhRequest | None = None
    receipt: LocalReceipt | None = None
    skipped_reason: str = ""


class GhTransport(Protocol):
    def send(self, request: GhRequest) -> None: ...


def merge_argv(request: GhRequest, *, merge_method: str = "--squash") -> list[str]:
    """The exact `gh` invocation a real transport should run for a merge.

    `--match-head-commit` is `gh`'s atomic guard: the API refuses the merge if
    the head moved. The GraphQL equivalent is `mergePullRequest(expectedHeadOid:)`.
    """
    if request.op != "merge":
        raise ValueError("merge_argv is only for merge requests")
    return [
        "gh",
        "pr",
        "merge",
        str(request.pr_number),
        "--repo",
        request.repo,
        merge_method,
        "--match-head-commit",
        str(request.expected_head_oid),
    ]


def merge_graphql_variables(request: GhRequest) -> dict[str, Any]:
    """Variables for `mergePullRequest`, the API path with the same guard."""
    if request.op != "merge":
        raise ValueError("merge_graphql_variables is only for merge requests")
    return {"expectedHeadOid": request.expected_head_oid, "pullRequestId": f"{request.repo}#{request.pr_number}"}


def prepare_action(
    pr_number: int,
    pr: PullRequest,
    decision: MergeDecision,
    *,
    allow_external_comment: bool = False,
) -> PreparedAction:
    """Map a MergeDecision to the single operation it authorizes."""
    if decision.action == "merge":
        if not pr.head_sha:
            return PreparedAction(skipped_reason="no head sha to pin the merge to")
        return PreparedAction(
            request=GhRequest(repo=pr.repo, pr_number=pr_number, op="merge", expected_head_oid=pr.head_sha)
        )

    if decision.action == "draft":
        return PreparedAction(
            request=GhRequest(repo=pr.repo, pr_number=pr_number, op="convert_to_draft", body=decision.reason)
        )

    if decision.action == "suggestion_only":
        receipt = LocalReceipt(
            repo=pr.repo, pr_number=pr_number, kind="t0_suggestion", text=decision.reason, published=False
        )
        if allow_external_comment:
            return PreparedAction(
                request=GhRequest(repo=pr.repo, pr_number=pr_number, op="comment", body=decision.reason),
                receipt=LocalReceipt(
                    repo=pr.repo, pr_number=pr_number, kind="t0_suggestion", text=decision.reason, published=True
                ),
            )
        return PreparedAction(receipt=receipt, skipped_reason="T0 suggestion kept local; no unsolicited PR comment")

    if decision.action == "pr_review":
        return PreparedAction(
            receipt=LocalReceipt(repo=pr.repo, pr_number=pr_number, kind="owner_review", text=decision.reason),
            skipped_reason="PR left open for the owner; no API write",
        )

    return PreparedAction(skipped_reason=f"blocked: {decision.reason}")


def apply_decision(
    pr_number: int,
    pr: PullRequest,
    decision: MergeDecision,
    transport: GhTransport,
    *,
    allow_external_comment: bool = False,
    receipts: list[LocalReceipt] | None = None,
) -> bool:
    """Send the authorized request, if any. Returns True if a call was made."""
    action = prepare_action(pr_number, pr, decision, allow_external_comment=allow_external_comment)
    if action.receipt is not None and receipts is not None:
        receipts.append(action.receipt)
    if action.request is None:
        return False
    transport.send(action.request)
    return True


__all__ = [
    "GhOp",
    "GhRequest",
    "GhTransport",
    "LocalReceipt",
    "PreparedAction",
    "apply_decision",
    "merge_argv",
    "merge_graphql_variables",
    "prepare_action",
]
