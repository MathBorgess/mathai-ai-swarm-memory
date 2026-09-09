"""Local SQLite state; raw challenges and signatures never reach SQL.

Call approve only after authenticating the owner at the HTTP boundary. Each
store owns one connection and must be closed by its caller. Schema version 1
is bootstrapped here; future versions require explicit migration code.
"""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.domain.pairing import (
    InvalidPairingProof,
    InvalidPairingState,
    PairingExpired,
    PairingRequest,
    verify_proof,
)


@dataclass(frozen=True)
class ActiveAgent:
    id: str
    public_key: str
    approved_at: datetime
    expires_at: datetime


def _aware(at: datetime) -> None:
    if at.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


class SqlitePairingStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS pairing_requests (
                id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                challenge_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'proof_verified', 'approved'))
            );
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE REFERENCES pairing_requests(id),
                public_key TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def create(self, request: PairingRequest) -> None:
        if request.status != "pending":
            raise InvalidPairingState("Only pending requests may be created")
        _aware(request.created_at)
        _aware(request.expires_at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO pairing_requests VALUES (?, ?, ?, ?, ?, 'pending')",
                (request.id, request.public_key, hashlib.sha256(request.challenge).hexdigest(),
                 request.created_at.isoformat(), request.expires_at.isoformat()),
            )

    def _request(self, request_id: str, at: datetime) -> sqlite3.Row:
        _aware(at)
        row = self.connection.execute(
            "SELECT * FROM pairing_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise InvalidPairingState("Unknown pairing request")
        if at < datetime.fromisoformat(row["created_at"]):
            raise ValueError("Operation precedes request creation")
        if at >= datetime.fromisoformat(row["expires_at"]):
            raise PairingExpired("Pairing request expired")
        return row

    def verify_proof(self, request_id: str, challenge: bytes, signature: bytes, now: datetime) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self._request(request_id, now)
            if row["status"] != "pending":
                raise InvalidPairingState("Pairing request does not accept proofs")
            if not hmac.compare_digest(hashlib.sha256(challenge).hexdigest(), row["challenge_hash"]):
                raise InvalidPairingProof("Invalid pairing proof")
            request = PairingRequest(
                id=row["id"], public_key=row["public_key"], challenge=challenge,
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]), status=row["status"],
            )
            verify_proof(request, signature, now)
            self.connection.execute(
                "UPDATE pairing_requests SET status = 'proof_verified' WHERE id = ?", (request_id,)
            )

    def approve(self, request_id: str, at: datetime, expires_at: datetime) -> ActiveAgent:
        """Record explicit owner approval with a caller-selected agent lifetime."""
        _aware(at)
        _aware(expires_at)
        if expires_at <= at:
            raise ValueError("Agent expiry must follow approval")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self._request(request_id, at)
            if row["status"] != "proof_verified":
                raise InvalidPairingState("Owner approval requires a verified proof")
            agent = ActiveAgent(secrets.token_urlsafe(32), row["public_key"], at, expires_at)
            self.connection.execute(
                "INSERT INTO agents VALUES (?, ?, ?, ?, ?, NULL)",
                (agent.id, request_id, agent.public_key, at.isoformat(), expires_at.isoformat()),
            )
            self.connection.execute(
                "UPDATE pairing_requests SET status = 'approved' WHERE id = ?", (request_id,)
            )
        return agent

    def active_agent(self, agent_id: str, now: datetime | None = None) -> ActiveAgent | None:
        now = now if now is not None else datetime.now(timezone.utc)
        _aware(now)
        row = self.connection.execute("""
            SELECT a.* FROM agents a JOIN pairing_requests p ON p.id = a.request_id
            WHERE a.id = ? AND a.revoked_at IS NULL AND p.status = 'approved'
        """, (agent_id,)).fetchone()
        if row is None:
            return None
        agent = ActiveAgent(row["id"], row["public_key"],
                            datetime.fromisoformat(row["approved_at"]),
                            datetime.fromisoformat(row["expires_at"]))
        return agent if agent.approved_at <= now < agent.expires_at else None

    def revoke(self, agent_id: str, at: datetime) -> None:
        _aware(at)
        with self.connection:
            self.connection.execute(
                "UPDATE agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (at.isoformat(), agent_id),
            )
