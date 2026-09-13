"""Adversarial cases for the remote MCP OAuth provider. No live network."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

import jwt

from app.mcp_oauth.github import HttpGitHubAuthorizationCode
from test_mcp_oauth import (
    NOW,
    PUBLIC,
    READ,
    REDIRECT,
    RESOURCE,
    add_principal,
    complete_code_flow,
    csrf_token,
    exchange,
    make_env,
    pkce_pair,
    register_client,
    tokens_for,
)


def _client_code(env, *, redirect_uri=REDIRECT, scope=READ, resource=RESOURCE, **kwargs):
    add_principal(env, principal_id="ana", subject="1001", scopes=(scope,))
    env.github.subject = "1001"
    registered = register_client(env, redirect_uri=redirect_uri)
    callback, verifier = complete_code_flow(
        env,
        client_id=registered["client_id"],
        redirect_uri=redirect_uri,
        scope=scope,
        resource=resource,
        **kwargs,
    )
    return registered, callback, verifier


def test_authorization_code_replay_and_expiry(tmp_path):
    env = make_env(tmp_path)
    registered, callback, verifier = _client_code(env)
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    first = exchange(env, client_id=registered["client_id"], code=code, verifier=verifier)
    assert first.status_code == 200
    replay = exchange(env, client_id=registered["client_id"], code=code, verifier=verifier)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    env.clock[0] = NOW
    registered, callback, verifier = _client_code(env, redirect_uri="http://127.0.0.1:5555/cb")
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    env.clock[0] = NOW.replace(hour=NOW.hour) + __import__("datetime").timedelta(minutes=6)
    expired = exchange(
        env,
        client_id=registered["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:5555/cb",
    )
    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"


def test_wrong_pkce_client_resource_and_redirect(tmp_path):
    env = make_env(tmp_path)
    registered, callback, verifier = _client_code(env)
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    wrong_pkce = exchange(env, client_id=registered["client_id"], code=code, verifier="a" * 64)
    assert wrong_pkce.status_code == 400

    registered, callback, verifier = _client_code(env, redirect_uri="http://127.0.0.1:6001/cb")
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    other = register_client(env, redirect_uri="http://127.0.0.1:6002/cb", name="Other")
    wrong_client = exchange(
        env,
        client_id=other["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:6001/cb",
    )
    assert wrong_client.status_code == 400

    registered, callback, verifier = _client_code(env, redirect_uri="http://127.0.0.1:6003/cb")
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    wrong_resource = exchange(
        env,
        client_id=registered["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:6003/cb",
        resource="https://evil.example/mcp",
    )
    assert wrong_resource.status_code == 400
    assert wrong_resource.json()["error"] in {"invalid_grant", "invalid_target"}

    registered, callback, verifier = _client_code(env, redirect_uri="http://127.0.0.1:6004/cb")
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    wrong_redirect = exchange(
        env,
        client_id=registered["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:9/elsewhere",
    )
    assert wrong_redirect.status_code == 400


def test_authorize_rejects_unregistered_and_malicious_redirects(tmp_path):
    env = make_env(tmp_path)
    registered = register_client(env)
    verifier, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "state": "s",
        "scope": READ,
    }
    unregistered = env.client.get(
        "/mcp/oauth/authorize",
        params={**params, "redirect_uri": "http://127.0.0.1:1/other"},
        follow_redirects=False,
    )
    assert unregistered.status_code == 400
    assert "http://127.0.0.1:1/other" not in unregistered.headers.get("location", "")

    for uri in (
        "http://evil.example/cb",
        "https://app.example/cb#frag",
        "http://127.0.0.1/cb#x",
        "http://user:pass@127.0.0.1/cb",
        "javascript:alert(1)",
        "http://127.0.0.1.evil/cb",
        "http://localhost.evil/cb",
    ):
        denied = env.client.post(
            "/mcp/oauth/register",
            json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"},
        )
        assert denied.status_code == 400, uri
        assert denied.json()["error"] == "invalid_redirect_uri", uri


def test_csrf_mismatch_does_not_issue_code(tmp_path):
    env = make_env(tmp_path)
    add_principal(env, principal_id="ana", subject="1001", scopes=(READ,))
    registered = register_client(env)
    verifier, challenge = pkce_pair()
    page = env.client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "s",
            "scope": READ,
        },
    )
    assert page.status_code == 200
    forged = env.client.post(
        "/mcp/oauth/consent",
        data={"csrf": "not-the-token", "decision": "allow"},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    env.client.cookies.clear()
    missing = env.client.post(
        "/mcp/oauth/consent",
        data={"csrf": csrf_token(page.text), "decision": "allow"},
        follow_redirects=False,
    )
    assert missing.status_code == 403

    env.client.cookies.clear()
    page = env.client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "s",
            "scope": READ,
        },
    )
    csrf = csrf_token(page.text)
    consent = env.client.post(
        "/mcp/oauth/consent",
        data={"csrf": csrf, "decision": "allow"},
        follow_redirects=False,
    )
    github_state = parse_qs(urlparse(consent.headers["location"]).query)["state"][0]
    wrong_state = env.client.get(
        "/mcp/oauth/callback",
        params={"code": "ghs-test", "state": "not-" + github_state},
        follow_redirects=False,
    )
    assert wrong_state.status_code in {400, 403}


def test_registration_bound_and_rate_limited(tmp_path):
    env = make_env(tmp_path, registration_limit=3)
    first = register_client(env, redirect_uri="http://127.0.0.1:7001/a")
    second = register_client(env, redirect_uri="http://127.0.0.1:7002/b", name="Other")
    register_client(env, redirect_uri="http://127.0.0.1:7003/c", name="Third")
    limited = env.client.post(
        "/mcp/oauth/register",
        json={"redirect_uris": ["http://127.0.0.1:7004/d"], "token_endpoint_auth_method": "none"},
    )
    assert limited.status_code == 429

    add_principal(env, principal_id="ana", subject="1001", scopes=(READ,))
    env.github.subject = "1001"
    callback, verifier = complete_code_flow(
        env, client_id=first["client_id"], redirect_uri="http://127.0.0.1:7001/a", scope=READ,
    )
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    stolen = exchange(
        env,
        client_id=second["client_id"],
        code=code,
        verifier=verifier,
        redirect_uri="http://127.0.0.1:7001/a",
    )
    assert stolen.status_code == 400


def test_duplicate_parameters_and_oversized_bodies(tmp_path):
    env = make_env(tmp_path)
    registered = register_client(env)
    duplicate_query = env.client.get(
        "/mcp/oauth/authorize?response_type=code&response_type=token"
        f"&client_id={registered['client_id']}&redirect_uri={REDIRECT}"
        "&code_challenge=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        "&code_challenge_method=S256"
        f"&resource={RESOURCE}&state=s&scope={READ}",
        follow_redirects=False,
    )
    assert duplicate_query.status_code == 400
    duplicate_json = env.client.post(
        "/mcp/oauth/register",
        content=b'{"redirect_uris":["http://127.0.0.1/a"],"redirect_uris":["http://127.0.0.1/b"]}',
        headers={"content-type": "application/json"},
    )
    assert duplicate_json.status_code == 400
    huge = env.client.post(
        "/mcp/oauth/register",
        content=b"{" + b"a" * 20000 + b"}",
        headers={"content-type": "application/json"},
    )
    assert huge.status_code == 413
    duplicate_form = env.client.post(
        "/mcp/oauth/token",
        content=(
            b"grant_type=refresh_token&grant_type=authorization_code&client_id=x"
            b"&refresh_token=y&resource=" + RESOURCE.encode()
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert duplicate_form.status_code == 400


def test_legacy_a2a_and_dpop_tokens_are_rejected(tmp_path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    env = make_env(tmp_path)
    _, tokens = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ,))
    opaque = env.client.get("/__probe", headers={"Authorization": "Bearer " + "a" * 40})
    assert opaque.status_code == 401
    dpop = env.client.get("/__probe", headers={"Authorization": "DPoP " + tokens["access_token"]})
    assert dpop.status_code == 401
    private = load_pem_private_key(env.pem, password=None)
    wrong_aud = jwt.encode(
        {
            "iss": PUBLIC,
            "aud": PUBLIC,
            "sub": "ana",
            "workspace_id": "personal",
            "scope": READ,
            "classifications": ["public", "shared"],
            "iat": int(NOW.timestamp()),
            "nbf": int(NOW.timestamp()),
            "exp": int(NOW.timestamp()) + 300,
            "jti": "x",
            "sid": "family",
            "cnf": {"jkt": "not-used"},
        },
        private,
        algorithm="ES256",
    )
    rejected = env.client.get("/__probe", headers={"Authorization": f"Bearer {wrong_aud}"})
    assert rejected.status_code == 401
    challenge = rejected.headers.get("www-authenticate", "")
    assert "resource_metadata=" in challenge
    assert f"{PUBLIC}/.well-known/oauth-protected-resource" in challenge


def test_access_token_expiry_and_family_revoke(tmp_path):
    from datetime import timedelta

    env = make_env(tmp_path)
    registered, tokens = tokens_for(env, principal_id="ana", subject="1001", scopes=(READ,))
    env.clock[0] = NOW + timedelta(minutes=6)
    expired = env.client.get("/__probe", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert expired.status_code == 401
    refreshed = env.client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200
    env.client.post(
        "/mcp/oauth/revoke",
        data={"token": refreshed.json()["refresh_token"], "client_id": registered["client_id"]},
    )
    after = env.client.get(
        "/__probe", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert after.status_code == 401


def test_plain_pkce_and_missing_resource_rejected(tmp_path):
    env = make_env(tmp_path)
    registered = register_client(env)
    verifier, _ = pkce_pair()
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    plain = env.client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "plain",
            "resource": RESOURCE,
            "state": "s",
            "scope": READ,
        },
        follow_redirects=False,
    )
    assert plain.status_code == 400
    missing = env.client.get(
        "/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "code_challenge_method": "S256",
            "state": "s",
            "scope": READ,
        },
        follow_redirects=False,
    )
    assert missing.status_code == 400


def test_http_github_refuses_custom_hosts():
    github = HttpGitHubAuthorizationCode(
        client_id="cid",
        client_secret="sec",
        callback_url=f"{PUBLIC}/mcp/oauth/callback",
    )
    assert github.AUTHORIZE_URL == "https://github.com/login/oauth/authorize"
    assert github.TOKEN_URL == "https://github.com/login/oauth/access_token"
    assert github.USER_URL == "https://api.github.com/user"
