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


def build_app_from_environment() -> FastAPI:
    # Resolve every required value before constructing adapters or the application.
    database_path = require_env("AUTH_BROKER_DATABASE_PATH")
    audience = require_env("AUTH_BROKER_AUDIENCE")
    issuer = require_env("AUTH_BROKER_CF_ACCESS_ISSUER")
    access_audience = require_env("AUTH_BROKER_CF_ACCESS_AUDIENCE")
    owner_email = require_env("AUTH_BROKER_OWNER_EMAIL")
    hermes_url = require_env("HERMES_A2A_URL")
    hermes_bearer = require_env("HERMES_BROKER_TOKEN")
    github_client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID")
    github_client_secret = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET")
    return create_app(
        database_path=database_path,
        audience=audience,
        owner_verifier=CloudflareAccessVerifier(issuer, access_audience, owner_email),
        hermes=HttpHermesClient(hermes_url, hermes_bearer),
        github_oauth=(HttpGitHubOAuth(github_client_id, github_client_secret)
                      if github_client_id and github_client_secret else None),
        github_redirect_uri=os.environ.get("GITHUB_OAUTH_REDIRECT_URI"),
        github_allowed_user_id=os.environ.get("GITHUB_ALLOWED_USER_ID"),
    )


app = build_app_from_environment()
