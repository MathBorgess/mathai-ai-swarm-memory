"""Uvicorn entry point: required local configuration, production adapters only."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException

from app.ask import (
    AskBusy,
    AskService,
    AskTimeout,
    ContainerWorker,
    IsolationConfig,
    IsolationUnavailable,
    MemoryThreadStore,
    ThreadBusy,
    worker_root,
)
from app.ask import build_router as build_ask_router
from app.adapters.hermes import HttpHermesClient
from app.adapters.github import HttpGitHubOAuth
from app.adapters.owner import CloudflareAccessVerifier
from app.api import create_app
from app.context import ContextStore
from app.context import build_router as build_context_router
from app.context.query import AuthError, resolve_handles, search, validate_principal
from app.mcp_oauth.github import HttpGitHubAuthorizationCode
from app.proposals import HttpProposalGitHub, ProposalRepository, ProposalStore, submit_proposal
from app.proposals import build_router as build_proposal_router
from app.proposals.schema import ProposalPayloadError, ProposalTooLarge, parse_proposal


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Required environment variable is missing or empty: {name}")
    return value


def optional_owner_verifier() -> CloudflareAccessVerifier | None:
    names = ("AUTH_BROKER_CF_ACCESS_ISSUER", "AUTH_BROKER_CF_ACCESS_AUDIENCE", "AUTH_BROKER_OWNER_EMAIL")
    values = {name: os.environ.get(name, "").strip() for name in names}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Cloudflare Access pairing configuration is incomplete: {', '.join(missing)}")
    return CloudflareAccessVerifier(
        values["AUTH_BROKER_CF_ACCESS_ISSUER"],
        values["AUTH_BROKER_CF_ACCESS_AUDIENCE"],
        values["AUTH_BROKER_OWNER_EMAIL"],
    )


def build_app_from_environment() -> FastAPI:
    # Resolve every required value before constructing adapters or the application.
    database_path = require_env("AUTH_BROKER_DATABASE_PATH")
    audience = require_env("AUTH_BROKER_AUDIENCE")
    hermes_url = require_env("HERMES_A2A_URL")
    hermes_bearer = require_env("HERMES_BROKER_TOKEN")
    github_client_id = require_env("GITHUB_OAUTH_CLIENT_ID")
    github_client_secret = require_env("GITHUB_OAUTH_CLIENT_SECRET")
    github_allowed_user_id = require_env("GITHUB_ALLOWED_USER_ID")
    workspace_id = os.environ.get("AUTH_BROKER_WORKSPACE_ID", "").strip() or None
    public_url = os.environ.get("AUTH_BROKER_PUBLIC_URL", "").strip() or "https://a2a.mathai.com.br"
    token_signing_key = _optional_signing_key()
    lifetime_raw = os.environ.get("AUTH_BROKER_ACCESS_TOKEN_LIFETIME_SECONDS", "").strip()
    access_token_lifetime = timedelta(minutes=5)
    if lifetime_raw:
        seconds = int(lifetime_raw)
        if not 0 < seconds <= 3600:
            raise RuntimeError("AUTH_BROKER_ACCESS_TOKEN_LIFETIME_SECONDS must be between 1 and 3600")
        access_token_lifetime = timedelta(seconds=seconds)
    context_factory, mcp_query, mcp_resolve, context_store = optional_context()
    proposal_factory, mcp_propose = optional_proposals()
    ask_factory, mcp_ask = optional_ask(context_store)
    return create_app(
        database_path=database_path,
        audience=audience,
        owner_verifier=optional_owner_verifier(),
        hermes=HttpHermesClient(hermes_url, hermes_bearer),
        github_oauth=HttpGitHubOAuth(github_client_id, github_client_secret),
        github_allowed_user_id=github_allowed_user_id,
        token_signing_key=token_signing_key,
        workspace_id=workspace_id,
        public_url=public_url,
        access_token_lifetime=access_token_lifetime,
        context_router_factory=context_factory,
        proposal_router_factory=proposal_factory,
        ask_router_factory=ask_factory,
        mcp_github=optional_mcp_github(github_client_id, github_client_secret, public_url, token_signing_key, workspace_id),
        mcp_query=mcp_query,
        mcp_resolve=mcp_resolve,
        mcp_propose=mcp_propose,
        mcp_ask=mcp_ask,
    )


def optional_context():
    """S3 query/resolve, installed only when the operator points at an indexed store.

    The manifest is ingested out of band by the operator; nothing here indexes
    a vault. Without the path the routes stay 503 and capabilities omit them.
    """
    path = os.environ.get("AUTH_BROKER_CONTEXT_SQLITE", "").strip()
    if not path:
        return None, None, None, None
    store = ContextStore(path)
    factory = lambda *, authorize: build_context_router(authorize=authorize, store=store)
    return factory, _mcp_query(store), _mcp_resolve(store), store


def optional_proposals():
    """S4 propose, installed only with a dedicated GitHub write credential.

    That credential is separate from the Device Flow app: a broker access token
    never reaches the writer. Missing any part is a configuration error, not a
    permissive fallback.
    """
    names = (
        "AUTH_BROKER_PROPOSAL_SQLITE",
        "AUTH_BROKER_PROPOSAL_REPO",
        "AUTH_BROKER_PROPOSAL_BASE_REF",
        "AUTH_BROKER_PROPOSAL_GITHUB_TOKEN",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    if not any(values.values()):
        return None, None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Proposal configuration is incomplete: {', '.join(missing)}")
    owner, _, name = values["AUTH_BROKER_PROPOSAL_REPO"].partition("/")
    if not owner or not name:
        raise RuntimeError("AUTH_BROKER_PROPOSAL_REPO must be owner/name")
    repository = ProposalRepository(owner=owner, name=name, base_ref=values["AUTH_BROKER_PROPOSAL_BASE_REF"])
    store = ProposalStore(values["AUTH_BROKER_PROPOSAL_SQLITE"])
    github = HttpProposalGitHub(repository, values["AUTH_BROKER_PROPOSAL_GITHUB_TOKEN"])
    factory = lambda *, authorize: build_proposal_router(
        authorize=authorize, store=store, github=github, repository=repository,
    )
    return factory, _mcp_propose(store, github)


def optional_ask(context_store):
    """Isolated Hermes ask. Installed only with image, network, inference file, and context."""
    names = (
        "AUTH_BROKER_ASK_INFERENCE_CONFIG",
        "AUTH_BROKER_ASK_IMAGE",
        "AUTH_BROKER_ASK_NETWORK",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    if not any(values.values()):
        return None, None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Ask configuration is incomplete: {', '.join(missing)}")
    if context_store is None:
        raise RuntimeError("Ask requires AUTH_BROKER_CONTEXT_SQLITE")
    docker = Path(os.environ.get("AUTH_BROKER_ASK_DOCKER", "/usr/bin/docker") or "/usr/bin/docker")
    isolation = IsolationConfig(
        image=values["AUTH_BROKER_ASK_IMAGE"],
        launch_script=worker_root() / "launch.sh",
        worker_script=worker_root() / "worker_main.py",
        style_path=worker_root() / "style" / "SOUL.md",
        docker_bin=docker,
        inference_config=Path(values["AUTH_BROKER_ASK_INFERENCE_CONFIG"]),
        network=values["AUTH_BROKER_ASK_NETWORK"],
    )
    try:
        isolation.validate()
    except IsolationUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    worker = ContainerWorker(isolation)
    threads = MemoryThreadStore()
    service = AskService(store=context_store, worker=worker, threads=threads, isolation=isolation)
    factory = lambda *, authorize: build_ask_router(
        authorize=authorize, store=context_store, worker=worker, isolation=isolation, threads=threads,
    )
    return factory, _mcp_ask(service)


def _mcp_query(store):
    def query(principal, query, limit=10):
        try:
            return search(store, validate_principal(principal), query, limit)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
    return query


def _mcp_resolve(store):
    def resolve(principal, handles):
        try:
            return resolve_handles(store, validate_principal(principal), handles)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
    return resolve


def _mcp_propose(store, github):
    def propose(principal, namespace, title, body_markdown, sources, *, idempotency_key):
        try:
            payload = parse_proposal({
                "namespace": namespace,
                "title": title,
                "body_markdown": body_markdown,
                "sources": sources,
            })
        except ProposalTooLarge:
            raise HTTPException(413, "Proposal markdown too large") from None
        except ProposalPayloadError:
            raise HTTPException(400, "Invalid proposal") from None
        return submit_proposal(
            store=store, github=github, principal=principal, payload=payload,
            idempotency_key=idempotency_key, now=datetime.now(timezone.utc),
        )
    return propose


def _mcp_ask(service: AskService):
    def ask(principal, query, thread_id=None):
        try:
            return service.ask(principal, query, thread_id)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
        except (AskBusy, ThreadBusy) as error:
            raise HTTPException(getattr(error, "status", 429), "Ask concurrency limit") from None
        except AskTimeout as error:
            raise HTTPException(error.status, "Ask worker timed out") from None
        except IsolationUnavailable:
            raise HTTPException(503, "Ask generator unavailable") from None
    return ask


def optional_mcp_github(client_id: str, client_secret: str, public_url: str, signing_key: str | None, workspace_id: str | None):
    """Authorization-code GitHub adapter for remote MCP OAuth.

    Mounted only when swarm issuance is already configured (workspace + signing
    key). Uses the same GitHub OAuth App as Device Flow; the web callback is
    {public_url}/mcp/oauth/callback.
    """
    if not signing_key or not workspace_id:
        return None
    issuer = public_url.rstrip("/")
    return HttpGitHubAuthorizationCode(
        client_id, client_secret, callback_url=f"{issuer}/mcp/oauth/callback",
    )


def _optional_signing_key() -> str | None:
    path = os.environ.get("AUTH_BROKER_JWT_SIGNING_KEY_PATH", "").strip()
    inline = os.environ.get("AUTH_BROKER_JWT_SIGNING_KEY", "").strip()
    if path and inline:
        raise RuntimeError("Set only one of AUTH_BROKER_JWT_SIGNING_KEY or AUTH_BROKER_JWT_SIGNING_KEY_PATH")
    if path:
        pem = Path(path).read_text()
        if not pem.strip():
            raise RuntimeError("AUTH_BROKER_JWT_SIGNING_KEY_PATH is empty")
        return pem
    return inline or None


app = build_app_from_environment()
