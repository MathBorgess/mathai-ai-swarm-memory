"""Uvicorn entry point: required local configuration, production adapters only."""

import os
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI

from app.adapters.hermes import HttpHermesClient
from app.adapters.github import HttpGitHubOAuth
from app.adapters.owner import CloudflareAccessVerifier
from app.api import create_app
from app.context import ContextStore
from app.context import build_router as build_context_router
from app.proposals import HttpProposalGitHub, ProposalRepository, ProposalStore
from app.proposals import build_router as build_proposal_router


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
        context_router_factory=optional_context_factory(),
        proposal_router_factory=optional_proposal_factory(),
    )


def optional_context_factory():
    """S3 query/resolve, installed only when the operator points at an indexed store.

    The manifest is ingested out of band by the operator; nothing here indexes
    a vault. Without the path the routes stay 503 and capabilities omit them.
    """
    path = os.environ.get("AUTH_BROKER_CONTEXT_SQLITE", "").strip()
    if not path:
        return None
    store = ContextStore(path)
    return lambda *, authorize: build_context_router(authorize=authorize, store=store)


def optional_proposal_factory():
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
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Proposal configuration is incomplete: {', '.join(missing)}")
    owner, _, name = values["AUTH_BROKER_PROPOSAL_REPO"].partition("/")
    if not owner or not name:
        raise RuntimeError("AUTH_BROKER_PROPOSAL_REPO must be owner/name")
    repository = ProposalRepository(owner=owner, name=name, base_ref=values["AUTH_BROKER_PROPOSAL_BASE_REF"])
    store = ProposalStore(values["AUTH_BROKER_PROPOSAL_SQLITE"])
    github = HttpProposalGitHub(repository, values["AUTH_BROKER_PROPOSAL_GITHUB_TOKEN"])
    return lambda *, authorize: build_proposal_router(
        authorize=authorize, store=store, github=github, repository=repository,
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
