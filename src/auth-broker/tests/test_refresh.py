from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from swarm_helpers import (
    AUDIENCE,
    GitHubOAuth,
    NOW,
    PUBLIC_URL,
    WORKSPACE,
    FakeHermes,
    dpop_headers,
    es256_material,
)


def _issue(tmp_path, signing, *, scopes=("ctx:read:pesquisa.tcc",), clock=None, name="broker.sqlite3"):
    clock = clock or [NOW]
    key, pem, jwk, _ = signing
    path = tmp_path / name
    store = SqlitePairingStore(path)
    try:
        store.add_principal("advisor-01", github_subject="12345", jwk=jwk, role="advisor", now=clock[0])
        for scope in scopes:
            store.set_grant("advisor-01", scope, clock[0], clock[0] + timedelta(days=30))
    finally:
        store.close()
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
        clock=lambda: clock[0], github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    client = TestClient(app)
    start = client.post(
        "/v1/oauth/github/device/start",
        json={"principal_id": "advisor-01", "scopes": list(scopes)},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start", now=clock[0]),
    ).json()
    tokens = client.post(
        "/v1/oauth/github/device/poll",
        json={"device_code": start["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/poll", now=clock[0]),
    ).json()
    return client, key, path, tokens, clock


@pytest.fixture
def signing():
    return es256_material()


def test_refresh_rotates_and_reuse_of_older_generation_revokes_family(tmp_path, signing):
    client, key, path, tokens, clock = _issue(tmp_path, signing)
    first = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert second.status_code == 200
    reused = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert reused.status_code == 400
    assert reused.json() == {"error": "invalid_grant"}
    assert tokens["refresh_token"] not in reused.text
    later = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": second.json()["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert later.json() == {"error": "invalid_grant"}


def test_concurrent_refresh_does_not_issue_two_successors(tmp_path, signing):
    client, key, _, tokens, clock = _issue(tmp_path, signing)

    def once(_):
        return client.post(
            "/v1/oauth/token",
            json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(once, range(2)))
    successes = [item for item in responses if item.status_code == 200]
    failures = [item for item in responses if item.status_code != 200]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].json()["error"] == "invalid_grant"
    assert "access_token" not in failures[0].text


def test_wrong_key_expired_and_family_revoke_are_invalid_grant(tmp_path, signing):
    client, key, path, tokens, clock = _issue(tmp_path, signing)
    other, _, _, _ = es256_material()
    wrong_key = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers=dpop_headers(other, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert wrong_key.json() == {"error": "invalid_grant"}
    assert tokens["refresh_token"] not in wrong_key.text
    sid = jwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
    with closing(SqlitePairingStore(path)) as store:
        store.revoke_family(sid, clock[0])
    revoked = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    )
    assert revoked.json() == {"error": "invalid_grant"}
    expired_client, expired_key, _, expired_tokens, expired_clock = _issue(
        tmp_path, signing, clock=[NOW], name="expired.sqlite3",
    )
    expired_clock[0] = NOW + timedelta(days=31)
    expired = expired_client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": expired_tokens["refresh_token"]},
        headers=dpop_headers(expired_key, method="POST", path="/v1/oauth/token", now=expired_clock[0]),
    )
    assert expired.json() == {"error": "invalid_grant"}
    assert expired_tokens["refresh_token"] not in expired.text


def test_refresh_does_not_extend_family_expiry(tmp_path, signing):
    clock = [NOW]
    client, key, path, tokens, clock = _issue(tmp_path, signing, clock=clock)
    clock[0] = NOW + timedelta(minutes=4)
    refreshed = client.post(
        "/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
    ).json()
    assert refreshed["expires_in"] <= 300
    with closing(SqlitePairingStore(path)) as store:
        row = store.connection.execute("SELECT expires_at FROM token_families").fetchone()
        assert row[0] == (NOW + timedelta(days=30)).isoformat()
