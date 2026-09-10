"""Uvicorn entry point: required local configuration, production adapters only."""

import os

from fastapi import FastAPI

from app.adapters.hermes import HttpHermesClient
from app.adapters.github import HttpGitHubOAuth
from app.adapters.owner import CloudflareAccessVerifier
from app.api import create_app


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
    return create_app(
        database_path=database_path,
        audience=audience,
        owner_verifier=optional_owner_verifier(),
        hermes=HttpHermesClient(hermes_url, hermes_bearer),
        github_oauth=HttpGitHubOAuth(github_client_id, github_client_secret),
        github_allowed_user_id=github_allowed_user_id,
    )


app = build_app_from_environment()
