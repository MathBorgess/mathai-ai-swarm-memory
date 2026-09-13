"""Independent MCP OAuth tables in the configured broker SQLite file."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


class InvalidOAuthGrant(ValueError):
    error = "invalid_grant"


class RefreshReuse(InvalidOAuthGrant):
    pass


def _aware(at: datetime) -> None:
    if at.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_oauth_registration_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_oauth_transactions (
    id TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    resource TEXT NOT NULL,
    client_state TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    principal_id TEXT
);
CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    resource TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS mcp_oauth_families (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    requested_scopes TEXT NOT NULL,
    current_token_hash TEXT NOT NULL,
    previous_token_hash TEXT,
    current_access_jti TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS mcp_oauth_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES mcp_oauth_families(id),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class OAuthTransaction:
    id: str
    csrf_hash: str
    client_id: str
    redirect_uri: str
    resource: str
    client_state: str
    code_challenge: str
    scopes: tuple[str, ...]
    status: str
    expires_at: datetime
    principal_id: str | None


@dataclass(frozen=True)
class OAuthCode:
    client_id: str
    redirect_uri: str
    resource: str
    code_challenge: str
    principal_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class OAuthFamily:
    id: str
    principal_id: str
    client_id: str
    resource: str
    requested_scopes: tuple[str, ...]
    expires_at: datetime
    current_access_jti: str


class McpOAuthStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def prune(self, now: datetime, *, registration_window: timedelta) -> None:
        _aware(now)
        iso = now.isoformat()
        window_start = (now - registration_window).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM mcp_oauth_transactions WHERE expires_at <= ?", (iso,))
            self.connection.execute(
                "DELETE FROM mcp_oauth_codes WHERE expires_at <= ? OR consumed_at IS NOT NULL", (iso,),
            )
            self.connection.execute("DELETE FROM mcp_oauth_clients WHERE expires_at <= ?", (iso,))
            self.connection.execute(
                "DELETE FROM mcp_oauth_registration_events WHERE occurred_at <= ?", (window_start,),
            )

    def registration_count(self, now: datetime, window: timedelta) -> int:
        _aware(now)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM mcp_oauth_registration_events WHERE occurred_at > ?",
            ((now - window).isoformat(),),
        ).fetchone()
        return int(row[0])

    def record_registration(self, now: datetime) -> None:
        _aware(now)
        with self.connection:
            self.connection.execute(
                "INSERT INTO mcp_oauth_registration_events VALUES (?, ?)",
                (secrets.token_urlsafe(16), now.isoformat()),
            )

    def put_client(
        self,
        *,
        client_id: str,
        client_name: str,
        redirect_uris: tuple[str, ...],
        now: datetime,
        expires_at: datetime,
    ) -> OAuthClient:
        _aware(now)
        _aware(expires_at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO mcp_oauth_clients VALUES (?, ?, ?, 'none', ?, ?)",
                (
                    client_id, client_name,
                    json.dumps(list(redirect_uris), separators=(",", ":")),
                    now.isoformat(), expires_at.isoformat(),
                ),
            )
        return OAuthClient(client_id, client_name, redirect_uris, expires_at)

    def get_client(self, client_id: str, now: datetime) -> OAuthClient | None:
        _aware(now)
        row = self.connection.execute(
            "SELECT * FROM mcp_oauth_clients WHERE client_id = ?", (client_id,),
        ).fetchone()
        if row is None or now >= datetime.fromisoformat(row["expires_at"]):
            return None
        return OAuthClient(
            row["client_id"], row["client_name"], tuple(json.loads(row["redirect_uris"])),
            datetime.fromisoformat(row["expires_at"]),
        )

    def put_transaction(
        self,
        *,
        tx_id: str,
        csrf_hash: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        client_state: str,
        code_challenge: str,
        scopes: tuple[str, ...],
        now: datetime,
        expires_at: datetime,
    ) -> None:
        _aware(now)
        _aware(expires_at)
        with self.connection:
            self.connection.execute(
                """INSERT INTO mcp_oauth_transactions
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_consent', ?, NULL)""",
                (
                    tx_id, csrf_hash, client_id, redirect_uri, resource, client_state,
                    code_challenge, " ".join(scopes), expires_at.isoformat(),
                ),
            )

    def get_transaction(self, tx_id: str, now: datetime) -> OAuthTransaction | None:
        _aware(now)
        row = self.connection.execute(
            "SELECT * FROM mcp_oauth_transactions WHERE id = ?", (tx_id,),
        ).fetchone()
        if row is None or now >= datetime.fromisoformat(row["expires_at"]):
            return None
        return OAuthTransaction(
            row["id"], row["csrf_hash"], row["client_id"], row["redirect_uri"], row["resource"],
            row["client_state"], row["code_challenge"], tuple(row["scopes"].split()),
            row["status"], datetime.fromisoformat(row["expires_at"]), row["principal_id"],
        )

    def set_transaction_status(self, tx_id: str, status: str, *, principal_id: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE mcp_oauth_transactions SET status = ?, principal_id = COALESCE(?, principal_id) WHERE id = ?",
                (status, principal_id, tx_id),
            )

    def consume_transaction(self, tx_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE mcp_oauth_transactions SET status = 'consumed' WHERE id = ?", (tx_id,),
            )

    def put_code(
        self,
        *,
        code_hash: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        code_challenge: str,
        principal_id: str,
        scopes: tuple[str, ...],
        expires_at: datetime,
    ) -> None:
        _aware(expires_at)
        with self.connection:
            self.connection.execute(
                "INSERT INTO mcp_oauth_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    code_hash, client_id, redirect_uri, resource, code_challenge,
                    principal_id, " ".join(scopes), expires_at.isoformat(),
                ),
            )

    def consume_code(self, code_hash: str, now: datetime) -> OAuthCode:
        _aware(now)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM mcp_oauth_codes WHERE code_hash = ?", (code_hash,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None or now >= datetime.fromisoformat(row["expires_at"]):
                raise InvalidOAuthGrant("Invalid authorization code")
            self.connection.execute(
                "UPDATE mcp_oauth_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL",
                (now.isoformat(), code_hash),
            )
        return OAuthCode(
            row["client_id"], row["redirect_uri"], row["resource"], row["code_challenge"],
            row["principal_id"], tuple(row["scopes"].split()),
        )

    def create_family(
        self,
        *,
        principal_id: str,
        client_id: str,
        resource: str,
        refresh_token_hash: str,
        requested_scopes: tuple[str, ...],
        access_jti: str,
        now: datetime,
        expires_at: datetime,
    ) -> OAuthFamily:
        _aware(now)
        _aware(expires_at)
        family_id = secrets.token_urlsafe(32)
        with self.connection:
            self.connection.execute(
                "INSERT INTO mcp_oauth_families VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
                (
                    family_id, principal_id, client_id, resource, " ".join(requested_scopes),
                    refresh_token_hash, access_jti, now.isoformat(), expires_at.isoformat(),
                ),
            )
            self.connection.execute(
                "INSERT INTO mcp_oauth_refresh_tokens VALUES (?, ?, ?, ?, NULL)",
                (refresh_token_hash, family_id, now.isoformat(), expires_at.isoformat()),
            )
        return OAuthFamily(
            family_id, principal_id, client_id, resource, requested_scopes, expires_at, access_jti,
        )

    def family(self, family_id: str, now: datetime) -> OAuthFamily | None:
        _aware(now)
        row = self.connection.execute(
            "SELECT * FROM mcp_oauth_families WHERE id = ?", (family_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or now >= datetime.fromisoformat(row["expires_at"]):
            return None
        return OAuthFamily(
            row["id"], row["principal_id"], row["client_id"], row["resource"],
            tuple(row["requested_scopes"].split()), datetime.fromisoformat(row["expires_at"]),
            row["current_access_jti"],
        )

    def revoke_family(self, family_id: str, now: datetime) -> None:
        _aware(now)
        with self.connection:
            self._revoke_family_locked(family_id, now)

    def _revoke_family_locked(self, family_id: str, now: datetime) -> None:
        changed = self.connection.execute(
            "UPDATE mcp_oauth_families SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now.isoformat(), family_id),
        )
        if changed.rowcount:
            self.connection.execute(
                "UPDATE mcp_oauth_refresh_tokens SET consumed_at = ? WHERE family_id = ? AND consumed_at IS NULL",
                (now.isoformat(), family_id),
            )

    def rotate_refresh(
        self,
        token_hash: str,
        *,
        now: datetime,
        new_token_hash: str,
        new_access_jti: str,
        client_id: str,
        resource: str,
    ) -> OAuthFamily:
        _aware(now)
        reuse = False
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT r.token_hash, r.consumed_at, r.expires_at AS token_expires,
                          f.id AS family_id, f.principal_id, f.client_id, f.resource,
                          f.current_token_hash, f.previous_token_hash, f.requested_scopes,
                          f.expires_at AS family_expires, f.revoked_at, f.current_access_jti
                   FROM mcp_oauth_refresh_tokens r
                   JOIN mcp_oauth_families f ON f.id = r.family_id
                   WHERE r.token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise InvalidOAuthGrant("Invalid refresh token")
            if row["client_id"] != client_id or row["resource"] != resource:
                raise InvalidOAuthGrant("Invalid refresh token")
            family_expires = datetime.fromisoformat(row["family_expires"])
            token_expires = datetime.fromisoformat(row["token_expires"])
            if row["revoked_at"] is not None or now >= family_expires or now >= token_expires:
                raise InvalidOAuthGrant("Invalid refresh token")
            if row["current_token_hash"] == token_hash:
                self.connection.execute(
                    "UPDATE mcp_oauth_refresh_tokens SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL",
                    (now.isoformat(), token_hash),
                )
                changed = self.connection.execute(
                    """UPDATE mcp_oauth_families
                       SET previous_token_hash = current_token_hash,
                           current_token_hash = ?, current_access_jti = ?
                       WHERE id = ? AND current_token_hash = ? AND revoked_at IS NULL""",
                    (new_token_hash, new_access_jti, row["family_id"], token_hash),
                )
                if changed.rowcount != 1:
                    raise InvalidOAuthGrant("Invalid refresh token")
                self.connection.execute(
                    "INSERT INTO mcp_oauth_refresh_tokens VALUES (?, ?, ?, ?, NULL)",
                    (new_token_hash, row["family_id"], now.isoformat(), family_expires.isoformat()),
                )
                return OAuthFamily(
                    row["family_id"], row["principal_id"], row["client_id"], row["resource"],
                    tuple(row["requested_scopes"].split()), family_expires, new_access_jti,
                )
            if row["previous_token_hash"] == token_hash:
                raise InvalidOAuthGrant("Invalid refresh token")
            self._revoke_family_locked(row["family_id"], now)
            reuse = True
        if reuse:
            raise RefreshReuse("Refresh token reuse revoked the family")

    def family_for_refresh(self, token_hash: str) -> str | None:
        row = self.connection.execute(
            "SELECT family_id FROM mcp_oauth_refresh_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return None if row is None else row["family_id"]
