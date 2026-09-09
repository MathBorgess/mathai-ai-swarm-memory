import base64
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.adapters.sqlite import SqlitePairingStore
from app.domain.pairing import InvalidPairingProof, InvalidPairingState, PairingExpired, create_request

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
END = NOW + timedelta(hours=1)


@pytest.fixture
def pairing(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    request = create_request(public, NOW)
    store = SqlitePairingStore(tmp_path / "broker.sqlite3")
    store.create(request)
    yield store, request, private
    store.close()


def prove(store, request, private):
    store.verify_proof(request.id, request.challenge, private.sign(request.challenge), NOW)


def test_only_explicitly_approved_agent_is_active(pairing):
    store, request, private = pairing
    assert store.active_agent(request.id, NOW) is None
    with pytest.raises(InvalidPairingState):
        store.approve(request.id, NOW, END)
    prove(store, request, private)
    assert store.active_agent(request.id, NOW) is None
    agent = store.approve(request.id, NOW, END)
    assert agent.public_key == request.public_key
    assert agent.approved_at == NOW
    assert agent.expires_at == END
    assert store.active_agent(agent.id, NOW) == agent
    with pytest.raises(InvalidPairingState):
        store.approve(request.id, NOW, END)


def test_sqlite_keeps_hash_not_challenge_or_signature(pairing, tmp_path):
    store, request, private = pairing
    prove(store, request, private)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        row = db.execute("SELECT challenge_hash, status FROM pairing_requests").fetchone()
        assert row == (hashlib.sha256(request.challenge).hexdigest(), "proof_verified")
        dump = "\n".join(db.iterdump())
    assert request.challenge.hex() not in dump
    assert base64.b64encode(request.challenge).decode() not in dump
    assert request.challenge not in (tmp_path / "broker.sqlite3").read_bytes()
    assert private.sign(request.challenge) not in (tmp_path / "broker.sqlite3").read_bytes()


def test_replay_is_rejected_across_connections(pairing, tmp_path):
    store, request, private = pairing
    prove(store, request, private)
    other = SqlitePairingStore(tmp_path / "broker.sqlite3")
    try:
        with pytest.raises(InvalidPairingState):
            prove(other, request, private)
    finally:
        other.close()


@pytest.mark.parametrize("wrong", ["challenge", "signature"])
def test_invalid_proof_does_not_consume_request(pairing, wrong):
    store, request, private = pairing
    challenge = b"wrong" if wrong == "challenge" else request.challenge
    signature = b"wrong" if wrong == "signature" else private.sign(challenge)
    with pytest.raises(InvalidPairingProof):
        store.verify_proof(request.id, challenge, signature, NOW)
    prove(store, request, private)


def test_expired_request_cannot_be_proved_or_approved(pairing):
    store, request, private = pairing
    with pytest.raises(PairingExpired):
        store.verify_proof(request.id, request.challenge, private.sign(request.challenge), request.expires_at)
    prove(store, request, private)
    with pytest.raises(PairingExpired):
        store.approve(request.id, request.expires_at, END)


def test_revocation_is_durable_and_idempotent(pairing, tmp_path):
    store, request, private = pairing
    prove(store, request, private)
    agent = store.approve(request.id, NOW, END)
    store.revoke(agent.id, NOW)
    store.revoke(agent.id, NOW + timedelta(seconds=1))
    assert store.active_agent(agent.id, NOW) is None
    other = SqlitePairingStore(tmp_path / "broker.sqlite3")
    try:
        assert other.active_agent(agent.id, NOW) is None
    finally:
        other.close()
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        assert db.execute("SELECT revoked_at FROM agents").fetchone()[0] == NOW.isoformat()


def test_active_agent_rejects_expiry_and_future_approval(pairing):
    store, request, private = pairing
    prove(store, request, private)
    agent = store.approve(request.id, NOW, END)
    assert store.active_agent(agent.id, END) is None
    assert store.active_agent(agent.id, NOW - timedelta(seconds=1)) is None
    assert store.active_agent("unknown", NOW) is None


def test_caller_cannot_insert_an_approved_request(pairing):
    store, request, _ = pairing
    request.status = "approved"
    with pytest.raises(InvalidPairingState):
        store.create(request)


def test_invalid_approval_validity_rolls_back(pairing):
    store, request, private = pairing
    prove(store, request, private)
    with pytest.raises(ValueError):
        store.approve(request.id, NOW, NOW)
    assert store.approve(request.id, NOW, END).public_key == request.public_key


def test_unknown_request_cannot_be_proved_or_approved(pairing):
    store, _, _ = pairing
    with pytest.raises(InvalidPairingState):
        store.verify_proof("unknown", b"", b"", NOW)
    with pytest.raises(InvalidPairingState):
        store.approve("unknown", NOW, END)
    store.revoke("unknown", NOW)


def test_failed_revocation_rolls_back_and_can_be_retried(pairing, tmp_path):
    store, request, private = pairing
    prove(store, request, private)
    agent = store.approve(request.id, NOW, END)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        db.execute("""CREATE TRIGGER reject_revoke AFTER UPDATE OF revoked_at ON agents
                      BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.revoke(agent.id, NOW)
    assert store.active_agent(agent.id, NOW) == agent
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        db.execute("DROP TRIGGER reject_revoke")
    store.revoke(agent.id, NOW)
    assert store.active_agent(agent.id, NOW) is None


def test_audit_lifecycle_is_durable_minimal_and_idempotent(pairing, tmp_path):
    store, request, private = pairing
    prove(store, request, private)
    agent = store.approve(request.id, NOW, END)
    store.revoke(agent.id, NOW)
    store.revoke(agent.id, NOW + timedelta(seconds=1))
    store.revoke("unknown", NOW)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        db.row_factory = sqlite3.Row
        events = db.execute("SELECT * FROM audit_events ORDER BY rowid").fetchall()
    assert len(events) == 4
    assert set(events[0].keys()) == {"id", "event_type", "occurred_at", "request_id", "agent_id"}
    assert [event["event_type"] for event in events] == [
        "request_created", "proof_verified", "agent_approved", "agent_revoked"
    ]
    assert len({event["id"] for event in events}) == 4
    for event in events:
        assert len(event["id"]) >= 32
        assert datetime.fromisoformat(event["occurred_at"]) == NOW
        assert datetime.fromisoformat(event["occurred_at"]).utcoffset() is not None
        assert event["request_id"] == request.id
    assert [event["agent_id"] for event in events] == [None, None, agent.id, agent.id]


@pytest.mark.parametrize("sql", ["UPDATE audit_events SET event_type = 'agent_revoked'", "DELETE FROM audit_events"])
def test_audit_events_are_append_only(pairing, tmp_path, sql):
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["create", "prove", "approve", "revoke"])
def test_audit_failure_rolls_back_domain_mutation(pairing, tmp_path, operation):
    store, request, private = pairing
    if operation in {"approve", "revoke"}:
        prove(store, request, private)
    if operation == "revoke":
        agent = store.approve(request.id, NOW, END)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        before_requests = db.execute("SELECT * FROM pairing_requests").fetchall()
        before_agents = db.execute("SELECT * FROM agents").fetchall()
        before_events = db.execute("SELECT * FROM audit_events").fetchall()
        db.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON audit_events
                      BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        if operation == "create":
            store.create(create_request(request.public_key, NOW))
        elif operation == "prove":
            prove(store, request, private)
        elif operation == "approve":
            store.approve(request.id, NOW, END)
        else:
            store.revoke(agent.id, NOW)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as db:
        assert db.execute("SELECT * FROM pairing_requests").fetchall() == before_requests
        assert db.execute("SELECT * FROM agents").fetchall() == before_agents
        assert db.execute("SELECT * FROM audit_events").fetchall() == before_events
