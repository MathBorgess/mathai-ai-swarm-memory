"""Router factory for POST /v1/context/propose. Auth mapping is injected by A."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from app.proposals.github import ProposalGitHubError, ProposalGitHubTimeout, ProposalRepository
from app.proposals.schema import (
    ProposalPayloadError,
    ProposalTooLarge,
    parse_object,
    parse_proposal,
    render_note,
)
from app.proposals.store import IdempotencyConflict, ProposalRecord, ProposalStore

AUTH_FIELDS = ("principal_id", "workspace_id", "scopes", "classifications", "family_id", "expires_at")
MAX_IDEMPOTENCY_KEY = 128


async def _body(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Expected application/json")
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > 16384:
            raise HTTPException(413, "Request body too large")
    return bytes(result)


def _auth_mapping(value: object) -> dict:
    if not isinstance(value, dict) or any(key not in value for key in AUTH_FIELDS):
        raise HTTPException(401, "Missing session")
    principal_id, workspace_id = value["principal_id"], value["workspace_id"]
    scopes, classifications = value["scopes"], value["classifications"]
    family_id, expires_at = value["family_id"], value["expires_at"]
    if not isinstance(principal_id, str) or not principal_id.strip():
        raise HTTPException(401, "Missing session")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise HTTPException(401, "Missing session")
    if not isinstance(family_id, str) or not family_id.strip():
        raise HTTPException(401, "Missing session")
    if not isinstance(scopes, tuple) or not all(isinstance(item, str) for item in scopes):
        raise HTTPException(401, "Missing session")
    if not isinstance(classifications, tuple) or not all(isinstance(item, str) for item in classifications):
        raise HTTPException(401, "Missing session")
    if not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
        raise HTTPException(401, "Missing session")
    return {
        "principal_id": principal_id,
        "workspace_id": workspace_id,
        "scopes": scopes,
        "classifications": classifications,
        "family_id": family_id,
        "expires_at": expires_at,
    }


def _idempotency_key(request: Request) -> str:
    return valid_idempotency_key(request.headers.get("idempotency-key", ""))


def valid_idempotency_key(key: str) -> str:
    if not key or len(key) > MAX_IDEMPOTENCY_KEY or "\n" in key or "\r" in key:
        raise HTTPException(400, "Invalid Idempotency-Key")
    return key


def _review_card(record: ProposalRecord) -> str:
    return "\n".join((
        "T2 proposal. Human confirmation pending.",
        "Does not promote to wiki T1/T0, fontes/, or main.",
        "Do not merge automatically.",
        f"proposal_id: {record.proposal_id}",
        f"principal_id: {record.principal_id}",
        f"workspace_id: {record.workspace_id}",
        f"path: {record.path}",
    ))


def _created(record: ProposalRecord) -> dict:
    if not record.pr_url or not record.branch:
        raise HTTPException(503, "Proposal publishing is not configured")
    return {
        "status": "created",
        "proposal_id": record.proposal_id,
        "branch": record.branch,
        "pr_url": record.pr_url,
    }


def publish(store: ProposalStore, github, record: ProposalRecord, now: datetime) -> ProposalRecord:
    current = store.get(record.proposal_id) or record
    if current.pr_url:
        return current
    if current.branch_sha is None:
        sha = github.ensure_branch(branch=current.branch)
        current = store.mark_branch(current.proposal_id, sha, now)
    if current.commit_sha is None:
        sha = github.ensure_file(
            branch=current.branch,
            path=current.path,
            content=current.note,
            message=f"T2 proposal {current.proposal_id}",
        )
        current = store.mark_commit(current.proposal_id, sha, now)
    if current.pr_url is None:
        number, url = github.ensure_draft_pr(
            branch=current.branch,
            title=current.title,
            body=_review_card(current),
        )
        current = store.mark_pr(current.proposal_id, number, url, now)
    return current


def submit_proposal(
    *,
    store: ProposalStore,
    github,
    principal: dict,
    payload,
    idempotency_key: str,
    now: datetime,
) -> dict:
    """Create or replay a T2 proposal using the trusted principal mapping.

    Caller JSON cannot choose principal, workspace, path, or branch.
    """
    auth = _auth_mapping(principal)
    if now >= auth["expires_at"]:
        raise HTTPException(401, "Missing session")
    key = valid_idempotency_key(idempotency_key)
    if payload.propose_scope() not in auth["scopes"]:
        raise HTTPException(403, "Proposal is outside the granted scope")
    if github is None:
        raise HTTPException(503, "Proposal publishing is not configured")
    proposal_id = secrets.token_urlsafe(16)
    note = render_note(
        proposal_id=proposal_id,
        principal_id=auth["principal_id"],
        workspace_id=auth["workspace_id"],
        created_at=now,
        payload=payload,
    )
    try:
        claimed = store.claim(
            workspace_id=auth["workspace_id"],
            principal_id=auth["principal_id"],
            idempotency_key=key,
            payload_hash=payload.payload_hash(),
            proposal_id=proposal_id,
            namespace=payload.namespace,
            title=payload.title,
            note=note,
            now=now,
        )
    except IdempotencyConflict:
        raise HTTPException(409, "Idempotency key reused with a different payload") from None
    if claimed.pr_url:
        return _created(claimed)
    try:
        published = publish(store, github, claimed, now)
    except ProposalGitHubTimeout:
        raise HTTPException(503, "GitHub proposal adapter unavailable") from None
    except ProposalGitHubError:
        raise HTTPException(503, "GitHub proposal adapter unavailable") from None
    return _created(published)


def build_router(*, authorize, store: ProposalStore, github, repository: ProposalRepository,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> APIRouter:
    if repository.allowed_prefix != "pesquisa/tcc/inbox":
        raise ValueError("Proposal prefix is closed")
    adapter_repo = getattr(github, "repository", None) if github is not None else None
    if adapter_repo is not None and adapter_repo != repository:
        raise ValueError("GitHub adapter repository must match")
    router = APIRouter()

    @router.post("/v1/context/propose")
    def propose(request: Request, body: bytes = Depends(_body)):
        try:
            mapping = authorize(request)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Missing session") from None
        try:
            payload = parse_proposal(parse_object(body))
        except ProposalTooLarge:
            raise HTTPException(413, "Proposal markdown too large") from None
        except ProposalPayloadError:
            raise HTTPException(400, "Invalid proposal") from None
        return submit_proposal(
            store=store, github=github, principal=mapping, payload=payload,
            idempotency_key=_idempotency_key(request), now=clock(),
        )

    return router
