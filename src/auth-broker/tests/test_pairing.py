import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.domain.pairing import (
    InvalidPairingProof,
    InvalidPairingState,
    PairingExpired,
    create_request,
    verify_proof,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


@pytest.fixture
def keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(public).decode("ascii")


def test_requests_have_unique_opaque_ids_and_challenges(keypair):
    _, public = keypair
    first, second = create_request(public, NOW), create_request(public, NOW)
    assert first.id != second.id
    assert len(first.id) >= 32
    assert public not in first.id
    assert first.challenge != second.challenge
    assert isinstance(first.challenge, bytes) and len(first.challenge) == 32
    assert first.public_key == public
    assert first.created_at == NOW
    assert first.expires_at == NOW + timedelta(minutes=10)
    assert first.status == "pending"
    assert repr(first.challenge) not in repr(first)


@pytest.mark.parametrize("elapsed", [timedelta(minutes=10), timedelta(minutes=10, seconds=1)])
def test_expiration_includes_exact_ten_minute_boundary(keypair, elapsed):
    private, public = keypair
    request = create_request(public, NOW)
    with pytest.raises(PairingExpired):
        verify_proof(request, private.sign(request.challenge), NOW + elapsed)
    assert request.status == "pending"


def test_valid_proof_does_not_approve_and_cannot_be_replayed(keypair, caplog, capsys):
    private, public = keypair
    request = create_request(public, NOW)
    signature = private.sign(request.challenge)
    assert verify_proof(request, signature, NOW + timedelta(minutes=9, seconds=59)) is None
    assert request.status == "proof_verified"
    with pytest.raises(InvalidPairingState):
        verify_proof(request, signature, NOW + timedelta(minutes=9, seconds=59))
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("kind", ["wrong_key", "wrong_challenge", "malformed"])
def test_invalid_signature_is_rejected_without_state_change(keypair, kind, caplog, capsys):
    private, public = keypair
    request = create_request(public, NOW)
    if kind == "wrong_key":
        signature = Ed25519PrivateKey.generate().sign(request.challenge)
    elif kind == "wrong_challenge":
        signature = private.sign(create_request(public, NOW).challenge)
    else:
        signature = b"invalid"
    with pytest.raises(InvalidPairingProof):
        verify_proof(request, signature, NOW)
    assert request.status == "pending"
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("public", ["", "invalid!", base64.b64encode(b"short").decode()])
def test_invalid_public_key_is_rejected(public):
    with pytest.raises(ValueError):
        create_request(public, NOW)


def test_naive_creation_time_is_rejected(keypair):
    with pytest.raises(ValueError):
        create_request(keypair[1], NOW.replace(tzinfo=None))


@pytest.mark.parametrize("now", [NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)])
def test_invalid_verification_time_is_rejected(keypair, now):
    private, public = keypair
    request = create_request(public, NOW)
    with pytest.raises(ValueError):
        verify_proof(request, private.sign(request.challenge), now)
