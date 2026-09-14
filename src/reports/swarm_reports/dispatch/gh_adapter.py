"""GitHub adapter: turns a MergeDecision into a transport call.

The transport is injected (``GhTransport``) so tests never make a real API
or ``gh`` CLI call. F5-integration wires a real transport later; this module
only prepares the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .merge_policy import MergeDecision, PullRequest


@dataclass(frozen=True)
class GhRequest:
    repo: str
    pr_number: int
    op: str  # "merge" | "convert_to_draft" | "comment"
    body: str | None = None


class GhTransport(Protocol):
    def send(self, request: GhRequest) -> None: ...


def prepare_request(pr_number: int, pr: PullRequest, decision: MergeDecision) -> GhRequest | None:
    """Map a MergeDecision to the single gh operation it authorizes, or None."""
    if decision.action == "merge":
        return GhRequest(repo=pr.repo, pr_number=pr_number, op="merge")
    if decision.action == "draft":
        return GhRequest(repo=pr.repo, pr_number=pr_number, op="convert_to_draft", body=decision.reason)
    if decision.action == "suggestion_only":
        return GhRequest(repo=pr.repo, pr_number=pr_number, op="comment", body=decision.reason)
    return None  # "block": no transport call at all


def apply_decision(pr_number: int, pr: PullRequest, decision: MergeDecision, transport: GhTransport) -> bool:
    """Return True if a request was sent."""
    request = prepare_request(pr_number, pr, decision)
    if request is None:
        return False
    transport.send(request)
    return True
