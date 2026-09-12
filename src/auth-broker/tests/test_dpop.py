from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from app.auth.dpop import InvalidDpopProof, access_token_hash, verify_dpop_proof
from app.auth.tokens import AccessTokenIssuer, InvalidAccessToken
from swarm_helpers import (
    AUDIENCE,
    GitHubOAuth,
    NOW,
    PUBLIC_URL,
    WORKSPACE,
    FakeHermes,
    dpop_headers,
    dpop_proof,
    es256_material,
)


@pytest.fixture
def signing():
    return es256_material()


def _swarm(tmp_path, signing, clock=None):
    clock = clock or [NOW]
    key, pem, jwk, _ = signing
    path = tmp_path / "broker.sqlite3"
    store = SqlitePairingStore(path)
    try:
        store.add_principal("advisor-01", github_subject="12345", jwk=jwk, role="advisor", now=clock[0])
        store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", clock[0], clock[0] + timedelta(days=30))
    finally:
        store.close()
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
        clock=lambda: clock[0], github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    return TestClient(app), key, path, pem, clock


def _tokens(client, key, clock):
    start = client.post(
        "/v1/oauth/github/device/start",
        json={"principal_id": "advisor-01", "scopes": ["ctx:read:pesquisa.tcc"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start", now=clock[0]),
    ).json()
    return client.post(
        "/v1/oauth/github/device/poll",
        json={"device_code": start["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/poll", now=clock[0]),
    ).json()


@pytest.mark.parametrize("kind", ["htm", "htu", "ath", "iat", "alg", "private_jwk", "replay", "mismatch"])
def test_dpop_proof_failures_are_rejected(tmp_path, signing, kind):
    client, key, path, pem, clock = _swarm(tmp_path, signing)
    tokens = _tokens(client, key, clock)
    access = tokens["access_token"]
    other, _, _, _ = es256_material()
    kwargs = dict(method="GET", path="/v1/context/capabilities", access_token=access, now=clock[0])
    if kind == "htm":
        headers = dpop_headers(key, **kwargs)
        headers["DPoP"] = dpop_proof(key, htm="POST", path="/v1/context/capabilities", now=clock[0],
                                     ath=access_token_hash(access))
    elif kind == "htu":
        headers = dpop_headers(key, **kwargs)
        headers["DPoP"] = dpop_proof(key, htm="GET", path="/v1/oauth/token", now=clock[0],
                                     ath=access_token_hash(access))
    elif kind == "ath":
        headers = dpop_headers(key, **kwargs)
        headers["DPoP"] = dpop_proof(key, htm="GET", path="/v1/context/capabilities", now=clock[0], ath="deadbeef")
    elif kind == "iat":
        headers = dpop_headers(key, **kwargs, iat=clock[0] - timedelta(minutes=10))
    elif kind == "alg":
        headers = dpop_headers(key, **kwargs)
        hmac_key = b"0" * 32
        headers["DPoP"] = jwt.encode(
            {"jti": "x", "htm": "GET", "htu": "https://a2a.example.test/v1/context/capabilities",
             "iat": int(clock[0].timestamp()), "ath": access_token_hash(access)},
            hmac_key, algorithm="HS256",
            headers={"typ": "dpop+jwt", "alg": "HS256", "jwk": {"kty": "oct", "k": "AA"}},
        )
    elif kind == "private_jwk":
        headers = dpop_headers(key, **kwargs, private_jwk=True)
    elif kind == "replay":
        headers = dpop_headers(key, **kwargs, jti="replay-jti")
        assert client.get("/v1/context/capabilities", headers=headers).status_code == 200
    else:
        headers = dpop_headers(other, **kwargs)
    response = client.get("/v1/context/capabilities", headers=headers)
    assert response.status_code == 401
    if kind != "replay":
        restarted = create_app(
            database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
            clock=lambda: clock[0], github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
            token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
        )
        with TestClient(restarted) as other_client:
            if kind == "htm":
                return
            assert other_client.get("/v1/context/capabilities", headers=dpop_headers(
                key, method="GET", path="/v1/context/capabilities", access_token=access, now=clock[0],
            )).status_code in {200, 401}


def test_dpop_replay_survives_restart(tmp_path, signing):
    client, key, path, pem, clock = _swarm(tmp_path, signing)
    tokens = _tokens(client, key, clock)
    headers = dpop_headers(
        key, method="GET", path="/v1/context/capabilities",
        access_token=tokens["access_token"], now=clock[0], jti="stable-jti",
    )
    assert client.get("/v1/context/capabilities", headers=headers).status_code == 200
    restarted = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
        clock=lambda: clock[0], github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    with TestClient(restarted) as other:
        assert other.get("/v1/context/capabilities", headers=headers).status_code == 401
        assert other.get(
            "/v1/context/capabilities",
            headers=dpop_headers(
                key, method="GET", path="/v1/context/capabilities",
                access_token=tokens["access_token"], now=clock[0],
            ),
        ).status_code == 200


def test_access_token_claims_and_signature_are_enforced(tmp_path, signing):
    client, key, path, pem, clock = _swarm(tmp_path, signing)
    tokens = _tokens(client, key, clock)
    issuer = AccessTokenIssuer(signing_key=pem, issuer=PUBLIC_URL, audience=AUDIENCE)
    principal = issuer.verify(tokens["access_token"], clock[0])
    assert principal.principal_id == "advisor-01"
    assert principal.workspace_id == WORKSPACE
    assert principal.scopes == ("ctx:read:pesquisa.tcc",)
    assert principal.classifications == ("public", "shared")
    assert principal.jkt
    other = AccessTokenIssuer(signing_key=es256_material()[1], issuer=PUBLIC_URL, audience=AUDIENCE)
    with pytest.raises(InvalidAccessToken):
        other.verify(tokens["access_token"], clock[0])
    with pytest.raises(InvalidAccessToken):
        issuer.verify(tokens["access_token"], clock[0] + timedelta(hours=2))
    forged = jwt.encode(
        jwt.decode(tokens["access_token"], options={"verify_signature": False}),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        algorithm="RS256",
    )
    with pytest.raises(InvalidAccessToken):
        issuer.verify(forged, clock[0])


def test_private_jwk_and_unexpected_alg_rejected_before_store():
    key, _, jwk, _ = es256_material()
    with pytest.raises(InvalidDpopProof):
        verify_dpop_proof(
            dpop_proof(key, htm="POST", path="/v1/oauth/token", private_jwk=True),
            htm="POST", htu="https://a2a.example.test/v1/oauth/token", now=NOW,
        )
    hmac_proof = jwt.encode(
        {"jti": "x", "htm": "POST", "htu": "https://a2a.example.test/v1/oauth/token", "iat": int(NOW.timestamp())},
        b"0" * 32, algorithm="HS256",
        headers={"typ": "dpop+jwt", "alg": "HS256", "jwk": jwk},
    )
    with pytest.raises(InvalidDpopProof):
        verify_dpop_proof(hmac_proof, htm="POST", htu="https://a2a.example.test/v1/oauth/token", now=NOW)
