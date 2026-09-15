"""Cloudflare Access verification and the gate that refuses an unsafe bind."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from swarm_reports.server.access import (
    AccessDenied,
    JwksCache,
    build_unsigned_test_token,
    verify_access_jwt,
)
from swarm_reports.server.app import build_server
from swarm_reports.server.config import (
    AUTH_ACCESS_JWT,
    AUTH_SYNTHETIC_LOCAL,
    AUTH_TUNNEL_LOOPBACK,
    AccessConfig,
    ServerConfig,
)
from tests.conftest import DAY, payload_dict
from tests.rsa_testkey import RsaTestKey
from tests.test_server_http import post, request, running

TEAM = "mathai.cloudflareaccess.com"
AUD = "a" * 64
EMAIL = "owner@mathai.com.br"


@pytest.fixture(scope="module")
def key() -> RsaTestKey:
    return RsaTestKey()


@pytest.fixture
def access() -> AccessConfig:
    return AccessConfig(
        team_domain=TEAM,
        audience=AUD,
        certs_url=f"https://{TEAM}/cdn-cgi/access/certs",
        allowed_emails=(EMAIL,),
    )


@pytest.fixture
def jwks(key) -> JwksCache:
    return JwksCache(f"https://{TEAM}/cdn-cgi/access/certs", opener=lambda url: key.jwks())


def claims(**extra) -> dict:
    body = {
        "iss": f"https://{TEAM}",
        "aud": [AUD],
        "email": EMAIL,
        "sub": "owner-sub",
        "iat": int(time.time()) - 5,
        "exp": int(time.time()) + 600,
    }
    body.update(extra)
    return body


# --------------------------------------------------------------------------------------
# Token verification
# --------------------------------------------------------------------------------------


def test_valid_token_is_accepted(key, access, jwks):
    identity = verify_access_jwt(key.token(claims()), access, jwks)
    assert identity.email == EMAIL
    assert identity.subject == "owner-sub"


def test_unsigned_token_is_refused(key, access, jwks):
    token = build_unsigned_test_token({"alg": "none", "kid": key.kid}, claims())
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(token, access, jwks)
    assert exc.value.code == "bad_algorithm"


def test_token_signed_by_another_key_is_refused(access, jwks):
    other = RsaTestKey(kid="test-kid")
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(other.token(claims()), access, jwks)
    assert exc.value.code == "bad_signature"


def test_tampered_payload_is_refused(key, access, jwks):
    header, payload, signature = key.token(claims()).split(".")
    forged = build_unsigned_test_token(
        json.loads(_pad(header)), claims(email="attacker@example.com")
    ).split(".")[1]
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(f"{header}.{forged}.{signature}", access, jwks)
    assert exc.value.code == "bad_signature"


@pytest.mark.parametrize(
    "override,code",
    [
        ({"iss": "https://evil.cloudflareaccess.com"}, "bad_issuer"),
        ({"aud": ["b" * 64]}, "bad_audience"),
        ({"aud": None}, "bad_audience"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"email": "someone@example.com"}, "not_allowed"),
        ({"email": ""}, "not_allowed"),
        ({"iat": int(time.time()) + 3600}, "not_yet_valid"),
    ],
)
def test_claims_are_all_checked(key, access, jwks, override, code):
    body = claims(**override)
    if override.get("exp"):
        body["iat"] = override["exp"] - 60
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(key.token(body), access, jwks)
    assert exc.value.code == code


def test_missing_exp_is_refused(key, access, jwks):
    body = claims()
    del body["exp"]
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(key.token(body), access, jwks)
    assert exc.value.code == "malformed_token"


def test_unknown_kid_refreshes_the_jwks_once_then_refuses(key, access):
    calls = []

    def opener(url):
        calls.append(url)
        return key.jwks()

    cache = JwksCache(access.certs_url, opener=opener)
    stranger = RsaTestKey(kid="rotated-kid")
    with pytest.raises(AccessDenied) as exc:
        verify_access_jwt(stranger.token(claims()), access, cache)
    assert exc.value.code == "unknown_key"
    assert len(calls) == 2  # first fetch, then one forced refresh for the rotation case


def test_jwks_must_be_https_on_the_team_domain():
    base = {
        "team_domain": TEAM,
        "audience": AUD,
        "allowed_emails": [EMAIL],
    }
    for bad in (
        f"http://{TEAM}/cdn-cgi/access/certs",
        "https://evil.example/cdn-cgi/access/certs",
    ):
        with pytest.raises(ValueError, match="certs_url"):
            AccessConfig.from_mapping({**base, "certs_url": bad})
    assert AccessConfig.from_mapping(base).certs_url.startswith(f"https://{TEAM}/")


def _pad(segment: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# --------------------------------------------------------------------------------------
# The server in access-jwt mode
# --------------------------------------------------------------------------------------


@pytest.fixture
def jwt_config(reports_config, access, key):
    server = replace(
        reports_config.server,
        auth_mode=AUTH_ACCESS_JWT,
        access=access,
    )
    return replace(reports_config, server=server, public_origin="https://reports.example")


def test_forged_identity_headers_do_not_authenticate(jwt_config, key, jwks):
    from swarm_reports.server.app import ReportServer

    server = ReportServer(jwt_config, jwt_config.server, jwks=jwks)
    server.start()
    try:
        for headers in (
            {},
            {"Cf-Access-Authenticated-User-Email": EMAIL},
            {"Cf-Access-Jwt-Assertion": "not.a.token"},
            {"Cookie": f"CF_Authorization={build_unsigned_test_token({'alg': 'none'}, claims())}"},
        ):
            status, _h, body = request(server, "GET", f"/{DAY.isoformat()}.html", headers=headers)
            assert status == 401, headers
            assert b"report" not in body
    finally:
        server.stop()


def test_valid_jwt_reads_and_writes(jwt_config, key, jwks):
    from swarm_reports.server.app import ReportServer

    server = ReportServer(jwt_config, jwt_config.server, jwks=jwks)
    server.start()
    try:
        token = key.token(claims())
        status, _h, body = request(
            server,
            "GET",
            f"/{DAY.isoformat()}.html",
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        assert status == 200
        assert b"report" in body

        status, _h, body = post(
            server,
            payload_dict(),
            origin=jwt_config.public_origin,
            extra={"Cf-Access-Jwt-Assertion": token},
        )
        assert status == 200
        assert json.loads(body)["revision"] == 1
    finally:
        server.stop()


def test_cookie_carries_the_token_too(jwt_config, key, jwks):
    from swarm_reports.server.app import ReportServer

    server = ReportServer(jwt_config, jwt_config.server, jwks=jwks)
    server.start()
    try:
        status, _h, _body = request(
            server,
            "GET",
            f"/{DAY.isoformat()}.html",
            headers={"Cookie": f"other=1; CF_Authorization={key.token(claims())}"},
        )
    finally:
        server.stop()
    assert status == 200


def test_healthz_stays_open_on_loopback_without_a_token(jwt_config, jwks):
    from swarm_reports.server.app import ReportServer

    server = ReportServer(jwt_config, jwt_config.server, jwks=jwks)
    server.start()
    try:
        status, _h, body = request(server, "GET", "/healthz")
    finally:
        server.stop()
    assert (status, body) == (200, b"ok\n")


# --------------------------------------------------------------------------------------
# Bind gate
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "reports.example"])
@pytest.mark.parametrize("mode", [AUTH_TUNNEL_LOOPBACK, AUTH_SYNTHETIC_LOCAL])
def test_only_access_jwt_may_bind_a_public_address(mode, host):
    with pytest.raises(ValueError, match="loopback"):
        ServerConfig(bind_host=host, auth_mode=mode).check_bind_gate()


def test_access_jwt_without_an_access_block_is_refused():
    with pytest.raises(ValueError, match="server.access"):
        ServerConfig(bind_host="0.0.0.0", auth_mode=AUTH_ACCESS_JWT).check_bind_gate()


def test_synthetic_local_may_not_claim_a_public_origin(access):
    config = ServerConfig(auth_mode=AUTH_SYNTHETIC_LOCAL)
    with pytest.raises(ValueError, match="loopback public_origin"):
        config.require_ready_for_mutation("https://reports.mathai.com.br")
    config.require_ready_for_mutation("http://127.0.0.1:8787")


def test_tunnel_loopback_may_serve_a_public_origin():
    ServerConfig(auth_mode=AUTH_TUNNEL_LOOPBACK).require_ready_for_mutation(
        "https://reports.mathai.com.br"
    )


def test_server_refuses_to_start_without_a_public_origin(reports_config):
    with pytest.raises(ValueError, match="public_origin"):
        build_server(replace(reports_config, public_origin=None))


def test_server_refuses_to_start_without_a_server_block(reports_config):
    with pytest.raises(ValueError, match="server"):
        build_server(replace(reports_config, server=None))


def test_overrides_are_gated_too(reports_config):
    with pytest.raises(ValueError, match="loopback"):
        build_server(reports_config, overrides={"bind_host": "0.0.0.0"})
