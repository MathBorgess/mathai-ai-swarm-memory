import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from app.auth.scopes import ScopeError
from app.cli import main
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

V1_SCHEMA = """
CREATE TABLE pairing_requests (
    id TEXT PRIMARY KEY, public_key TEXT NOT NULL, challenge_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'proof_verified', 'approved'))
);
CREATE TABLE agents (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE REFERENCES pairing_requests(id),
    public_key TEXT NOT NULL, approved_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'request_created', 'proof_verified', 'agent_approved', 'agent_revoked'
    )),
    occurred_at TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES pairing_requests(id),
    agent_id TEXT REFERENCES agents(id)
);
CREATE TABLE consumed_nonces (
    agent_id TEXT NOT NULL REFERENCES agents(id), nonce_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, nonce_hash)
);
CREATE TABLE broker_sessions (
    token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, scopes TEXT NOT NULL,
    issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
"""


@pytest.fixture
def signing():
    return es256_material()


def open_store(path):
    store = SqlitePairingStore(path)
    return store


def register(store, key_jwk, *, principal_id="advisor-01", subject="12345", role="advisor", now=NOW):
    return store.add_principal(principal_id, github_subject=subject, jwk=key_jwk, role=role, now=now)


def test_new_and_legacy_databases_migrate_without_duplicating_events(tmp_path):
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.executescript(V1_SCHEMA)
        db.execute("INSERT INTO pairing_requests VALUES ('req-1','pk','hash',?,?, 'pending')",
                   (NOW.isoformat(), (NOW + timedelta(minutes=10)).isoformat()))
        db.execute("INSERT INTO audit_events VALUES ('evt-1','request_created',?,'req-1',NULL)", (NOW.isoformat(),))
        db.execute(
            "INSERT INTO broker_sessions VALUES ('abc','12345','a2a:message',?,?, NULL)",
            (NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()),
        )
    first = open_store(legacy)
    try:
        first.add_principal("advisor-01", github_subject="12345", jwk=es256_material()[2], role="advisor", now=NOW)
    finally:
        first.close()
    second = open_store(legacy)
    try:
        second.add_principal("self-01", github_subject="12345", jwk=es256_material()[2], role="self-harness", now=NOW)
        with sqlite3.connect(legacy) as db:
            assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM grant_audit_events").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM broker_sessions").fetchone()[0] == 1
            assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
    finally:
        second.close()
    fresh = open_store(tmp_path / "fresh.sqlite3")
    try:
        with sqlite3.connect(tmp_path / "fresh.sqlite3") as db:
            assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
            assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    finally:
        fresh.close()


def test_same_subject_keeps_distinct_grants_and_swapped_keys_fail(tmp_path, signing):
    key_a, _, jwk_a, _ = signing
    key_b, _, jwk_b, _ = es256_material()
    path = tmp_path / "broker.sqlite3"
    store = open_store(path)
    try:
        register(store, jwk_a, principal_id="advisor-01")
        register(store, jwk_b, principal_id="advisor-02", role="advisor")
        store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=30))
        store.set_grant("advisor-02", "ctx:propose:pesquisa.tcc", NOW, NOW + timedelta(days=30))
        assert [g.scope for g in store.list_grants("advisor-01", NOW)] == ["ctx:read:pesquisa.tcc"]
        assert [g.scope for g in store.list_grants("advisor-02", NOW)] == ["ctx:propose:pesquisa.tcc"]
        with pytest.raises(ScopeError):
            store.set_grant("advisor-01", "ctx:write:wiki", NOW, NOW + timedelta(days=1))
        with pytest.raises(ScopeError):
            store.set_grant("advisor-01", "a2a:message", NOW, NOW + timedelta(days=1))
        with pytest.raises(ScopeError):
            store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=91))
    finally:
        store.close()
    hermes = FakeHermes()
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=hermes,
        clock=lambda: NOW, github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=signing[1], workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    with TestClient(app) as client:
        denied = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "advisor-01", "scopes": ["ctx:read:pesquisa.tcc"]},
            headers=dpop_headers(key_b, method="POST", path="/v1/oauth/github/device/start"),
        )
        assert denied.json() == {"error": "access_denied"}
        start = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "advisor-01", "scopes": ["ctx:write:wiki"]},
            headers=dpop_headers(key_a, method="POST", path="/v1/oauth/github/device/start"),
        )
        assert start.json() == {"error": "access_denied"}


def test_cli_help_and_grant_cycle(tmp_path, signing):
    store = tmp_path / "cli.sqlite3"
    jwk = json.dumps(signing[2])
    assert main(["--help"]) == 0
    assert main(["--store", str(store), "principal", "add", "--id", "advisor-01",
                 "--subject", "12345", "--role", "advisor", "--jwk", jwk]) == 0
    assert main(["--store", str(store), "grant", "set", "--principal", "advisor-01",
                 "--scope", "ctx:read:pesquisa.tcc", "--days", "30"]) == 0
    assert main(["--store", str(store), "grant", "list", "--principal", "advisor-01"]) == 0
    assert main(["--store", str(store), "principal", "list"]) == 0
    assert main(["--store", str(store), "grant", "revoke", "--principal", "advisor-01",
                 "--scope", "ctx:read:pesquisa.tcc"]) == 0
    with closing(open_store(store)) as db:
        assert db.list_grants("advisor-01", datetime.now(timezone.utc)) == []
    assert main(["--store", "relative.sqlite3", "principal", "list"]) == 2
    assert main(["--store", str(store), "grant", "set", "--principal", "advisor-01",
                 "--scope", "ctx:write:wiki", "--days", "1"]) == 2


def test_legacy_bearer_still_reaches_hermes_ctx_does_not(tmp_path, signing):
    key, pem, jwk, _ = signing
    path = tmp_path / "broker.sqlite3"
    hermes = FakeHermes()
    store = open_store(path)
    try:
        register(store, jwk)
        store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=30))
    finally:
        store.close()
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=hermes,
        clock=lambda: NOW, github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    with TestClient(app) as client:
        legacy = client.post("/v1/oauth/github/device/start", json={}).json()
        polled = client.post(
            "/v1/oauth/github/device/poll",
            json={"device_code": legacy["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        ).json()
        assert polled["token_type"] == "Bearer"
        query = client.post(
            "/v1/context/query", json={"query": "hello"},
            headers={"Authorization": "Bearer " + polled["access_token"]},
        )
        assert query.json() == {"answer": "hermes-only"}
        start = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "advisor-01", "scopes": ["ctx:read:pesquisa.tcc"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start"),
        ).json()
        tokens = client.post(
            "/v1/oauth/github/device/poll",
            json={"device_code": start["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/poll"),
        ).json()
        assert tokens["token_type"] == "DPoP"
        assert "ctx:read:pesquisa.tcc" in tokens["scope"]
        assert "a2a:message" not in tokens["scope"]
        swarm_query = client.post(
            "/v1/context/query", json={"query": "hello", "limit": 10},
            headers=dpop_headers(
                key, method="POST", path="/v1/context/query", access_token=tokens["access_token"],
            ),
        )
        assert swarm_query.status_code == 503
        caps = client.get(
            "/v1/context/capabilities",
            headers=dpop_headers(
                key, method="GET", path="/v1/context/capabilities", access_token=tokens["access_token"],
            ),
        ).json()
        assert caps["operations"] == ["capabilities"]
        assert caps["principal_id"] == "advisor-01"
        assert client.get(
            "/v1/context/capabilities",
            headers={"Authorization": "Bearer " + polled["access_token"]},
        ).status_code == 401
    assert hermes.queries == [("github:12345", "hello")]


def test_expired_grant_omitted_from_refresh_issued_access_survives_until_exp(tmp_path, signing):
    key, pem, jwk, _ = signing
    path = tmp_path / "broker.sqlite3"
    clock = [NOW]
    store = open_store(path)
    try:
        register(store, jwk)
        store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=30))
        store.set_grant("advisor-01", "ctx:propose:pesquisa.tcc", NOW, NOW + timedelta(days=30))
    finally:
        store.close()
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
        clock=lambda: clock[0], github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    with TestClient(app) as client:
        start = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "advisor-01",
                  "scopes": ["ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start", now=clock[0]),
        ).json()
        tokens = client.post(
            "/v1/oauth/github/device/poll",
            json={"device_code": start["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/poll", now=clock[0]),
        ).json()
        assert set(tokens["scope"].split()) == {"ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"}
        with closing(open_store(path)) as db:
            db.revoke_grant("advisor-01", "ctx:propose:pesquisa.tcc", clock[0])
        still = client.get(
            "/v1/context/capabilities",
            headers=dpop_headers(
                key, method="GET", path="/v1/context/capabilities",
                access_token=tokens["access_token"], now=clock[0],
            ),
        ).json()
        assert "ctx:propose:pesquisa.tcc" in still["scopes"]
        refreshed = client.post(
            "/v1/oauth/token",
            json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/token", now=clock[0]),
        ).json()
        assert refreshed["scope"] == "ctx:read:pesquisa.tcc"
        sid = jwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
        with closing(open_store(path)) as db:
            db.revoke_family(sid, clock[0])
        assert client.get(
            "/v1/context/capabilities",
            headers=dpop_headers(
                key, method="GET", path="/v1/context/capabilities",
                access_token=tokens["access_token"], now=clock[0],
            ),
        ).status_code == 401
