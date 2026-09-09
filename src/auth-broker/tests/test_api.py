import base64
import hashlib
import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app.api import create_app
from app.adapters.hermes import HttpHermesClient
from app.adapters.owner import CloudflareAccessVerifier, OwnerAuthenticationError
from app.adapters.sqlite import SqlitePairingStore

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
AUDIENCE = "https://pair.example.test"


class FakeOwnerVerifier:
    def verify(self, assertion):
        if assertion != "test-owner-assertion":
            raise OwnerAuthenticationError("Invalid owner assertion")


class FakeHermes:
    def __init__(self):
        self.queries = []

    def query_as_broker(self, *, agent_id, query):
        self.queries.append((agent_id, query))
        return {"answer": "test-response-only"}


@pytest.fixture
def broker(tmp_path):
    clock = [NOW]
    hermes = FakeHermes()
    path = tmp_path / "broker.sqlite3"
    app = create_app(database_path=path, audience=AUDIENCE,
                     owner_verifier=FakeOwnerVerifier(), hermes=hermes, clock=lambda: clock[0])
    with TestClient(app) as client:
        yield client, hermes, clock, path


def pair(client, *, approve=True):
    private = ed25519.Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    created = client.post("/v1/pairing-requests", json={"public_key": public})
    assert created.status_code == 201
    request = created.json()
    challenge = base64.b64decode(request["challenge"])
    proof = {"challenge": request["challenge"], "signature": base64.b64encode(private.sign(challenge)).decode()}
    assert client.post(f"/v1/pairing-requests/{request['id']}/proof", json=proof).status_code == 200
    if not approve:
        return private, request["id"]
    approval = client.post(f"/v1/pairing-requests/{request['id']}/approve", json={},
                           headers={"Cf-Access-Jwt-Assertion": "test-owner-assertion"})
    assert approval.status_code == 200
    return private, approval.json()["agent_id"]


def signed(private, agent_id, *, body=b'{"query":"private-query-only"}', **overrides):
    envelope = {"agent_id": agent_id, "audience": AUDIENCE,
                "timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "nonce": secrets.token_urlsafe(32), "body_sha256": hashlib.sha256(body).hexdigest()}
    envelope.update(overrides)
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    headers = {"X-Agent-Envelope": base64.b64encode(encoded).decode(),
               "X-Agent-Signature": base64.b64encode(private.sign(encoded)).decode(),
               "Content-Type": "application/json"}
    return body, headers


def test_successful_query_and_replay_rejected_without_storing_payload(broker):
    client, hermes, _, path = broker
    private, agent = pair(client)
    body, headers = signed(private, agent)
    headers["Authorization"] = "Bearer caller-credential-only"
    assert client.post("/v1/context/query", content=body, headers=headers).json() == {"answer": "test-response-only"}
    assert client.post("/v1/context/query", content=body, headers=headers).status_code == 403
    assert hermes.queries == [(agent, "private-query-only")]
    nonce = json.loads(base64.b64decode(headers["X-Agent-Envelope"]))["nonce"]
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT nonce_hash, expires_at FROM consumed_nonces").fetchone()
        assert row[0] == hashlib.sha256(base64.urlsafe_b64decode(nonce + "=")).hexdigest()
        assert datetime.fromisoformat(row[1]) == NOW + timedelta(seconds=60)
    raw = path.read_bytes()
    for value in [body, nonce.encode(), headers["X-Agent-Signature"].encode(), b"caller-credential-only", b"test-response-only"]:
        assert value not in raw


@pytest.mark.parametrize("change", [
    {"audience": "https://other.test"}, {"timestamp": "2026-09-08T23:59:00Z"},
    {"timestamp": "2026-09-09T00:01:00Z"}, {"timestamp": "2026-09-09T00:00:00"},
    {"nonce": "short"}, {"body_sha256": "0" * 64}, {"extra": "forbidden"},
])
def test_invalid_signed_envelopes_are_rejected(broker, change):
    client, hermes, _, _ = broker
    private, agent = pair(client)
    body, headers = signed(private, agent, **change)
    assert client.post("/v1/context/query", content=body, headers=headers).status_code == 403
    assert not hermes.queries


@pytest.mark.parametrize("kind", ["missing", "bearer", "wrong_key", "tampered_body", "malformed"])
def test_agent_id_or_bearer_does_not_authenticate(broker, kind):
    client, hermes, _, _ = broker
    private, agent = pair(client)
    body, headers = signed(private, agent)
    if kind == "missing":
        headers = {"Content-Type": "application/json", "X-Agent-Id": agent}
    elif kind == "bearer":
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + agent}
    elif kind == "wrong_key":
        body, headers = signed(ed25519.Ed25519PrivateKey.generate(), agent)
    elif kind == "tampered_body":
        body = b'{"query":"changed"}'
    else:
        headers["X-Agent-Signature"] = "invalid!"
    assert client.post("/v1/context/query", content=body, headers=headers).status_code == 403
    assert not hermes.queries


@pytest.mark.parametrize("state", ["unapproved", "expired", "revoked"])
def test_inactive_agents_never_reach_hermes(broker, state):
    client, hermes, clock, path = broker
    private, agent = pair(client, approve=state != "unapproved")
    if state == "expired":
        clock[0] = NOW + timedelta(hours=1)
    if state == "revoked":
        store = SqlitePairingStore(path)
        try:
            store.revoke(agent, NOW)
        finally:
            store.close()
    body, headers = signed(private, agent, timestamp=clock[0].strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert client.post("/v1/context/query", content=body, headers=headers).status_code == 403
    assert not hermes.queries


def test_owner_header_and_unverified_proof_cannot_approve(broker):
    client, _, _, _ = broker
    _, request_id = pair(client, approve=False)
    for headers in [{}, {"Cf-Access-Authenticated-User-Email": "owner@example.test"},
                    {"Cf-Access-Jwt-Assertion": "bad"}]:
        assert client.post(f"/v1/pairing-requests/{request_id}/approve", json={}, headers=headers).status_code == 403
    key = ed25519.Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    pending = client.post("/v1/pairing-requests", json={"public_key": public}).json()
    assert client.post(f"/v1/pairing-requests/{pending['id']}/approve", json={},
                       headers={"Cf-Access-Jwt-Assertion": "test-owner-assertion"}).status_code == 403


def test_hermes_adapter_uses_only_server_credential():
    captured = []
    def handler(request):
        captured.append(request)
        payload = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}})
    adapter = HttpHermesClient("https://hermes.example.test/", "server-local-only",
                               transport=httpx.MockTransport(handler))
    assert adapter.query_as_broker(agent_id="agent-public-id", query="hello") == {"ok": True}
    assert captured[0].headers["authorization"] == "Bearer server-local-only"
    assert "x-agent-signature" not in captured[0].headers
    assert "cf-access-jwt-assertion" not in captured[0].headers
    payload = json.loads(captured[0].content)
    assert payload["method"] == "message/send"
    assert payload["params"]["message"]["parts"] == [{"kind": "text", "text": "hello"}]


@pytest.fixture
def owner_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    return private, jwk


def test_production_owner_verifier_validates_signed_jwks_claims(owner_keys, monkeypatch):
    private, jwk = owner_keys
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    verifier = CloudflareAccessVerifier("https://team.cloudflareaccess.com", "access-app", "owner@example.test")
    now = datetime.now(timezone.utc)
    claims = {"iss": "https://team.cloudflareaccess.com", "aud": ["access-app"],
              "email": "owner@example.test", "sub": "owner-id", "iat": now, "exp": now + timedelta(minutes=5)}
    verifier.verify(jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"}))
    for changed in [{"iss": "https://attacker.test"}, {"aud": ["wrong"]},
                    {"email": "other@example.test"}, {"exp": now - timedelta(seconds=1)}]:
        token = jwt.encode(claims | changed, private, algorithm="RS256", headers={"kid": "test-key"})
        with pytest.raises(OwnerAuthenticationError):
            verifier.verify(token)
    forged = jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048),
                        algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(OwnerAuthenticationError):
        verifier.verify(forged)


def test_concurrent_replay_only_dispatches_once(broker):
    client, hermes, _, _ = broker
    private, agent = pair(client)
    body, headers = signed(private, agent)
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: client.post("/v1/context/query", content=body, headers=headers).status_code, range(2)))
    assert sorted(responses) == [200, 403]
    assert len(hermes.queries) == 1


def test_nonce_survives_app_restart_then_is_pruned_after_expiry(broker):
    client, hermes, clock, path = broker
    private, agent = pair(client)
    nonce = secrets.token_urlsafe(32)
    body, headers = signed(private, agent, nonce=nonce)
    assert client.post("/v1/context/query", content=body, headers=headers).status_code == 200
    restarted = create_app(database_path=path, audience=AUDIENCE, owner_verifier=FakeOwnerVerifier(),
                           hermes=hermes, clock=lambda: clock[0])
    with TestClient(restarted) as other:
        assert other.post("/v1/context/query", content=body, headers=headers).status_code == 403
        clock[0] += timedelta(seconds=60)
        assert other.post("/v1/context/query", content=body, headers=headers).status_code == 403
        body, headers = signed(private, agent, timestamp=clock[0].strftime("%Y-%m-%dT%H:%M:%SZ"))
        assert other.post("/v1/context/query", content=body, headers=headers).status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM consumed_nonces").fetchone()[0] == 1


def test_real_adapter_does_not_forward_caller_credentials_and_failure_consumes_nonce(tmp_path):
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(500, text="private-upstream-error")
    app = create_app(database_path=tmp_path / "broker.sqlite3", audience=AUDIENCE,
                     owner_verifier=FakeOwnerVerifier(), clock=lambda: NOW,
                     hermes=HttpHermesClient("https://hermes.example.test/", "server-local-only",
                                             transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        private, agent = pair(client)
        body, headers = signed(private, agent)
        headers.update({"Authorization": "Bearer caller-only", "Cf-Access-Jwt-Assertion": "caller-jwt-only"})
        response = client.post("/v1/context/query", content=body, headers=headers)
        assert response.status_code == 502
        assert "private-upstream-error" not in response.text
        assert client.post("/v1/context/query", content=body, headers=headers).status_code == 403
    assert len(captured) == 1
    assert captured[0].headers["Authorization"] == "Bearer server-local-only"
    assert "x-agent-envelope" not in captured[0].headers
    assert "x-agent-signature" not in captured[0].headers
    assert "cf-access-jwt-assertion" not in captured[0].headers
    assert b"caller-only" not in captured[0].content


def test_owner_jwks_failure_missing_claims_and_wrong_algorithm_fail_closed(owner_keys, monkeypatch):
    private, jwk = owner_keys
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    verifier = CloudflareAccessVerifier("https://team.cloudflareaccess.com", "access-app", "owner@example.test")
    now = datetime.now(timezone.utc)
    claims = {"iss": "https://team.cloudflareaccess.com", "aud": "access-app", "email": "owner@example.test",
              "sub": "owner-id", "iat": now, "exp": now + timedelta(minutes=5)}
    for missing in claims:
        reduced = {k: v for k, v in claims.items() if k != missing}
        with pytest.raises(OwnerAuthenticationError):
            verifier.verify(jwt.encode(reduced, private, algorithm="RS256", headers={"kid": "test-key"}))
    with pytest.raises(OwnerAuthenticationError):
        verifier.verify(jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": "test-key"}))
    def unavailable(self):
        raise jwt.PyJWKClientConnectionError("Unavailable")
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unavailable)
    with pytest.raises(OwnerAuthenticationError):
        verifier.verify(jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"}))


def test_bad_payloads_are_bounded_and_not_reflected(broker):
    client, _, _, _ = broker
    assert client.post("/v1/pairing-requests", content=b"x" * 16385,
                       headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/v1/pairing-requests", content="public_key=bad").status_code == 415
    response = client.post("/v1/pairing-requests", json={"public_key": "sensitive-input-only"})
    assert response.status_code == 400
    assert "sensitive-input-only" not in response.text


def test_deep_json_is_a_controlled_client_error(broker):
    client, hermes, _, _ = broker
    body = b'{"public_key":' + b'[' * 2000 + b'"sensitive-nested-input"' + b']' * 2000 + b'}'
    assert len(body) < 16384
    response = client.post("/v1/pairing-requests", content=body,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "sensitive-nested-input" not in response.text
    assert not hermes.queries
