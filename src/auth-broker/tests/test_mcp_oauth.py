"""Remote MCP OAuth provider: metadata, DCR, code+PKCE, two identities, grants."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.adapters.sqlite import SqlitePairingStore
from app.mcp_oauth import build_router
from app.mcp_oauth.github import GitHubIdentityError, HttpGitHubAuthorizationCode
from swarm_helpers import es256_material

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
PUBLIC = "https://a2a.mathai.com.br"
RESOURCE = "https://a2a.mathai.com.br/mcp"
READ = "ctx:read:pesquisa.tcc"
PROPOSE = "ctx:propose:pesquisa.tcc"
REDIRECT = "http://127.0.0.1:54321/callback"


class FakeGitHub:
    def __init__(self, subject="1001"):
        self.subject = subject
        self.urls = []
        fail = False
        self.fail = fail

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        url = (
            "https://github.com/login/oauth/authorize"
            f"?client_id=test&redirect_uri={redirect_uri}&state={state}&scope=read:user"
        )
        self.urls.append(url)
        return url

    def exchange_code(self, *, code: str, redirect_uri: str) -> str:
        if self.fail:
            raise GitHubIdentityError("GitHub identity validation failed")
        return self.subject


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def csrf_token(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html
    return match.group(1)


def make_env(tmp_path, *, github=None, clock=None, registration_limit=32):
    clock = clock or [NOW]
    path = tmp_path / "broker.sqlite3"
    _, pem, jwk, _ = es256_material()
    github = github or FakeGitHub()
    provider = build_router(
        database_path=path,
        github=github,
        signing_key=pem,
        workspace_id="personal",
        clock=lambda: clock[0],
        public_url=PUBLIC,
        registration_limit=registration_limit,
    )
    app = FastAPI()
    app.include_router(provider.router)

    @app.get("/__probe")
    def probe(request: Request):
        return provider.authorize(request)

    return SimpleNamespace(
        path=path,
        pem=pem,
        jwk=jwk,
        github=github,
        clock=clock,
        provider=provider,
        client=TestClient(app),
    )


def add_principal(env, *, principal_id, subject, role="advisor", scopes=()):
    store = SqlitePairingStore(env.path)
    try:
        if store.get_principal(principal_id) is None:
            store.add_principal(
                principal_id, github_subject=subject, jwk=env.jwk, role=role, now=env.clock[0],
            )
        for scope in scopes:
            store.set_grant(principal_id, scope, env.clock[0], env.clock[0] + timedelta(days=30))
    finally:
        store.close()


def register_client(env, *, redirect_uri=REDIRECT, name="Cursor"):
    response = env.client.post(
        "/mcp/oauth/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": name,
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body
    return body


def complete_code_flow(
    env,
    *,
    client_id,
    redirect_uri=REDIRECT,
    resource=RESOURCE,
    state="client-state",
    scope=f"{READ} {PROPOSE}",
    challenge=None,
    verifier=None,
    decision="allow",
):
    if challenge is None or verifier is None:
        verifier, challenge = pkce_pair()
    authorize = env.client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
            "state": state,
            "scope": scope,
        },
        follow_redirects=False,
    )
    if authorize.status_code != 200:
        return authorize, verifier
    csrf = csrf_token(authorize.text)
    assert redirect_uri in authorize.text
    for item in scope.split():
        assert item in authorize.text
    consent = env.client.post(
        "/mcp/oauth/consent",
        data={"csrf": csrf, "decision": decision},
        follow_redirects=False,
    )
    if decision != "allow":
        return consent, verifier
    assert consent.status_code == 302, consent.text
    github_url = consent.headers["location"]
    assert github_url.startswith("https://github.com/login/oauth/authorize")
    github_state = parse_qs(urlparse(github_url).query)["state"][0]
    callback = env.client.get(
        "/mcp/oauth/callback",
        params={"code": "ghs-test", "state": github_state},
        follow_redirects=False,
    )
    return callback, verifier


def exchange(env, *, client_id, code, verifier, redirect_uri=REDIRECT, resource=RESOURCE):
    return env.client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
            "resource": resource,
        },
    )


def tokens_for(env, *, principal_id, subject, scopes, redirect_uri=REDIRECT, name="Cursor"):
    add_principal(env, principal_id=principal_id, subject=subject, scopes=scopes)
    env.github.subject = subject
    registered = register_client(env, redirect_uri=redirect_uri, name=name)
    callback, verifier = complete_code_flow(
        env,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        scope=" ".join(scopes),
    )
    assert callback.status_code == 302, getattr(callback, "text", callback)
    location = urlparse(callback.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == redirect_uri or callback.headers["location"].startswith(redirect_uri)
    code = parse_qs(location.query)["code"][0]
    token = exchange(
        env,
        client_id=registered["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["token_type"] == "Bearer"
    return registered, body


def test_rfc9728_and_rfc8414_metadata(tmp_path):
    env = make_env(tmp_path)
    resource = env.client.get("/.well-known/oauth-protected-resource")
    nested = env.client.get("/.well-known/oauth-protected-resource/mcp")
    server = env.client.get("/.well-known/oauth-authorization-server")
    assert resource.status_code == nested.status_code == server.status_code == 200
    for document in (resource.json(), nested.json()):
        assert document["resource"] == RESOURCE
        assert document["authorization_servers"] == [PUBLIC]
        assert document["bearer_methods_supported"] == ["header"]
        assert READ in document["scopes_supported"]
        assert PROPOSE in document["scopes_supported"]
    meta = server.json()
    assert meta["issuer"] == PUBLIC
    assert meta["authorization_endpoint"] == f"{PUBLIC}/mcp/oauth/authorize"
    assert meta["token_endpoint"] == f"{PUBLIC}/mcp/oauth/token"
    assert meta["registration_endpoint"] == f"{PUBLIC}/mcp/oauth/register"
    assert meta["response_types_supported"] == ["code"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert "authorization_code" in meta["grant_types_supported"]
    assert "refresh_token" in meta["grant_types_supported"]


def test_dcr_public_client_and_strict_redirects(tmp_path):
    env = make_env(tmp_path)
    ok = env.client.post(
        "/mcp/oauth/register",
        json={
            "redirect_uris": ["http://127.0.0.1:9/cb", "http://[::1]/cb", "https://app.example/cb"],
            "client_name": "Cursor",
            "token_endpoint_auth_method": "none",
        },
    )
    assert ok.status_code == 201
    assert "client_secret" not in ok.json()
    bad = env.client.post(
        "/mcp/oauth/register",
        json={"redirect_uris": ["http://evil.example/cb"], "token_endpoint_auth_method": "none"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_redirect_uri"


def test_two_github_identities_cannot_share_grants(tmp_path):
    env = make_env(tmp_path)
    _, ana = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ,))
    _, bob = tokens_for(env, principal_id="bob", subject="2002", scopes=(PROPOSE,), redirect_uri="http://127.0.0.1:54322/callback")
    ana_probe = env.client.get("/__probe", headers={"Authorization": f"Bearer {ana['access_token']}"})
    bob_probe = env.client.get("/__probe", headers={"Authorization": f"Bearer {bob['access_token']}"})
    assert ana_probe.status_code == bob_probe.status_code == 200
    assert ana_probe.json()["principal_id"] == "ana"
    assert bob_probe.json()["principal_id"] == "bob"
    assert ana_probe.json()["scopes"] == [READ]
    assert bob_probe.json()["scopes"] == [PROPOSE]
    assert ana_probe.json()["workspace_id"] == "personal"
    assert {"principal_id", "workspace_id", "scopes", "classifications", "family_id", "expires_at"} <= set(ana_probe.json())
    env.github.subject = "1001"
    registered = register_client(env, redirect_uri="http://127.0.0.1:54323/callback", name="Other")
    callback, verifier = complete_code_flow(
        env,
        client_id=registered["client_id"],
        redirect_uri="http://127.0.0.1:54323/callback",
        scope=PROPOSE,
    )
    # Ana identified, but she was never granted propose-only in this client request:
    # she has READ, requested PROPOSE → no overlap → access_denied.
    assert callback.status_code == 302
    assert "error=access_denied" in callback.headers["location"]
    assert "code=" not in callback.headers["location"]


def test_github_identity_without_grants_is_denied(tmp_path):
    env = make_env(tmp_path)
    add_principal(env, principal_id="empty", subject="3003", scopes=())
    env.github.subject = "3003"
    registered = register_client(env)
    callback, _ = complete_code_flow(env, client_id=registered["client_id"], scope=READ)
    assert callback.status_code == 302
    assert "error=access_denied" in callback.headers["location"]
    assert "code=" not in callback.headers["location"]


def test_unknown_github_identity_is_denied_without_auto_grant(tmp_path):
    env = make_env(tmp_path)
    env.github.subject = "99999"
    registered = register_client(env)
    callback, _ = complete_code_flow(env, client_id=registered["client_id"])
    assert "error=access_denied" in callback.headers["location"]
    store = SqlitePairingStore(env.path)
    try:
        assert store.list_principals() == []
    finally:
        store.close()


def test_authorize_rechecks_grants_after_revoke_and_downgrade(tmp_path):
    env = make_env(tmp_path)
    _, tokens = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ, PROPOSE))
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert env.client.get("/__probe", headers=headers).json()["scopes"] == [READ, PROPOSE]
    store = SqlitePairingStore(env.path)
    try:
        store.revoke_grant("ana", PROPOSE, env.clock[0])
    finally:
        store.close()
    downgraded = env.client.get("/__probe", headers=headers)
    assert downgraded.status_code == 200
    assert downgraded.json()["scopes"] == [READ]
    store = SqlitePairingStore(env.path)
    try:
        store.revoke_grant("ana", READ, env.clock[0])
    finally:
        store.close()
    denied = env.client.get("/__probe", headers=headers)
    assert denied.status_code == 403
    assert "insufficient_scope" in denied.headers.get("www-authenticate", "")


def test_tokens_are_not_stored_in_plaintext(tmp_path):
    env = make_env(tmp_path)
    _, tokens = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ,))
    raw = env.path.read_bytes()
    assert tokens["access_token"].encode() not in raw
    assert tokens["refresh_token"].encode() not in raw
    assert b"ghs-test" not in raw
    assert b"gho_" not in raw


def test_http_github_uses_fixed_endpoints_and_numeric_id():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_secret", "token_type": "bearer"})
        if request.url.host == "api.github.com" and request.url.path == "/user":
            return httpx.Response(200, json={"id": 4242, "login": "mutable-nick"})
        return httpx.Response(404)

    github = HttpGitHubAuthorizationCode(
        client_id="cid",
        client_secret="csecret",
        callback_url=f"{PUBLIC}/mcp/oauth/callback",
        transport=httpx.MockTransport(handler),
    )
    url = github.authorization_url(state="st", redirect_uri=f"{PUBLIC}/mcp/oauth/callback")
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert parse_qs(parsed.query)["scope"] == ["read:user"]
    assert "csecret" not in url
    subject = github.exchange_code(code="abc", redirect_uri=f"{PUBLIC}/mcp/oauth/callback")
    assert subject == "4242"
    assert calls == [
        ("POST", "https://github.com/login/oauth/access_token"),
        ("GET", "https://api.github.com/user"),
    ]


def test_refresh_rotates_and_issues_bearer_not_dpop(tmp_path):
    env = make_env(tmp_path)
    registered, tokens = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ,))
    rotated = env.client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert rotated.status_code == 200
    body = rotated.json()
    assert body["token_type"] == "Bearer"
    assert body["refresh_token"] != tokens["refresh_token"]
    replay = env.client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}
    later = env.client.get("/__probe", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert later.status_code == 200
    revoked = env.client.get("/__probe", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert revoked.status_code == 401
