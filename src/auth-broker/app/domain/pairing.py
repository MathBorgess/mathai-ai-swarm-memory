"""Ephemeral Ed25519 pairing challenges; owner approval is a separate step.

Public keys use standard base64 encoding of the 32 raw Ed25519 bytes.
Challenge bytes exist only in memory: persistence must store their hash, never
serialize this object wholesale. This module does not log challenges or proofs.
"""

import base64
import binascii
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class PairingExpired(ValueError):
    """The request has reached its expiry time."""


class InvalidPairingProof(ValueError):
    """The proof does not verify against this request's key and challenge."""


class InvalidPairingState(ValueError):
    """The request no longer accepts a proof."""


@dataclass
class PairingRequest:
    id: str
    public_key: str
    challenge: bytes = field(repr=False)
    created_at: datetime
    expires_at: datetime
    status: str = "pending"


def _public_key(encoded: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("Expected a base64-encoded Ed25519 public key") from None


def _require_aware(now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


def create_request(public_key: str, now: datetime) -> PairingRequest:
    _require_aware(now)
    _public_key(public_key)
    return PairingRequest(
        id=secrets.token_urlsafe(32),
        public_key=public_key,
        challenge=secrets.token_bytes(32),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def verify_proof(request: PairingRequest, signature: bytes, now: datetime) -> None:
    """Consume one valid proof without granting owner approval or credentials."""
    _require_aware(now)
    if now < request.created_at:
        raise ValueError("Verification precedes request creation")
    if now >= request.expires_at:
        raise PairingExpired("Pairing request expired")
    if request.status != "pending":
        raise InvalidPairingState("Pairing request does not accept proofs")
    try:
        _public_key(request.public_key).verify(signature, request.challenge)
    except (InvalidSignature, ValueError, TypeError):
        raise InvalidPairingProof("Invalid pairing proof") from None
    request.status = "proof_verified"
