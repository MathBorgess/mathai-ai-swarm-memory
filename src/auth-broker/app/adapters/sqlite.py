"""Local SQLite state; raw challenges and signatures never reach SQL.

Call approve only after authenticating the owner at the HTTP boundary. Each
store owns one connection and must be closed by its caller. Schema version 1
is bootstrapped here; version 2 is an additive migration for principals,
grants, token families and DPoP replay; version 3 adds a cached GitHub login
and reads unkeyed principals (empty JWK) without generating dummy keys.
Reopening an already-migrated store does not duplicate rows.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.auth.dpop import public_jwk_thumbprint
from app.auth.scopes import ScopeError, validate_grant_ttl, validate_role, validate_scope
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

@dataclass(frozen=True)
class BrokerSession:
    token_hash: str
    subject: str
    scopes: tuple[str, ...]
    expires_at: datetime

@dataclass(frozen=True)
class Principal:
    id: str
    github_subject: str
    jwk_thumbprint: str | None
    jwk: dict | None
    role: str
    created_at: datetime
    revoked_at: datetime | None = None
    github_login: str | None = None

@dataclass(frozen=True)
class Grant:
    id: str
    principal_id: str
    scope: str
    created_at: datetime
    expires_at: datetime

@dataclass(frozen=True)
class TokenFamily:
    id: str
    principal_id: str
    workspace_id: str
    current_token_hash: str
    previous_token_hash: str | None
    created_at: datetime
    expires_at: datetime
    requested_scopes: tuple[str, ...] = ()
    revoked_at: datetime | None = None


class InvalidGrant(ValueError):
    error = "invalid_grant"


class RefreshReuse(InvalidGrant):
    """A refresh token from an older generation was presented."""


def _aware(at: datetime) -> None:
    if at.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


_V2_SCHEMA = """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS principals (
                id TEXT PRIMARY KEY,
                github_subject TEXT NOT NULL,
                jwk_thumbprint TEXT NOT NULL,
                jwk TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner', 'self-harness', 'advisor')),
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                github_login TEXT
            );
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL REFERENCES principals(id),
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS grant_audit_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'principal_added', 'grant_set', 'grant_revoked', 'family_revoked'
                )),
                occurred_at TEXT NOT NULL,
                principal_id TEXT,
                scope TEXT,
                family_id TEXT
            );
            CREATE TRIGGER IF NOT EXISTS grant_audit_events_no_update
                BEFORE UPDATE ON grant_audit_events
                BEGIN SELECT RAISE(ABORT, 'Grant audit events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS grant_audit_events_no_delete
                BEFORE DELETE ON grant_audit_events
                BEGIN SELECT RAISE(ABORT, 'Grant audit events are append-only'); END;
            CREATE TABLE IF NOT EXISTS token_families (
                id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL REFERENCES principals(id),
                workspace_id TEXT NOT NULL,
                current_token_hash TEXT,
                previous_token_hash TEXT,
                requested_scopes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                family_id TEXT NOT NULL REFERENCES token_families(id),
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS dpop_replays (
                jti_hash TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL
            );
        """


class SqlitePairingStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, timeout=30)
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
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'request_created', 'proof_verified', 'agent_approved', 'agent_revoked'
                )),
                occurred_at TEXT NOT NULL,
                request_id TEXT NOT NULL REFERENCES pairing_requests(id),
                agent_id TEXT REFERENCES agents(id)
            );
            CREATE TABLE IF NOT EXISTS consumed_nonces (
                agent_id TEXT NOT NULL REFERENCES agents(id),
                nonce_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (agent_id, nonce_hash)
            );
            CREATE TABLE IF NOT EXISTS broker_sessions (
                token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, scopes TEXT NOT NULL,
                issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
            );
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
        """)
        self._migrate()

    def _table_names(self) -> set[str]:
        return {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    def _schema_version(self) -> int:
        tables = self._table_names()
        if "schema_migrations" in tables:
            row = self.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)
        if "pairing_requests" in tables:
            return 1
        return 0

    def _migrate(self) -> None:
        applied = datetime.now(timezone.utc).isoformat()
        if self._schema_version() < 2:
            with self.connection:
                self.connection.executescript(_V2_SCHEMA)
                self.connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)", (applied,),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)", (applied,),
                )
        if self._schema_version() < 3:
            with self.connection:
                columns = {row[1] for row in self.connection.execute("PRAGMA table_info(principals)")}
                if "github_login" not in columns:
                    self.connection.execute("ALTER TABLE principals ADD COLUMN github_login TEXT")
                self.connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, ?)", (applied,),
                )

    def close(self) -> None:
        self.connection.close()

    def _audit(self, event_type: str, at: datetime, request_id: str, agent_id: str | None = None) -> None:
        """Append only identifiers and metadata inside the caller's transaction."""
        _aware(at)
        self.connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
            (secrets.token_urlsafe(32), event_type, at.isoformat(), request_id, agent_id),
        )

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
            self._audit("request_created", request.created_at, request.id)

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
            self._audit("proof_verified", now, request_id)

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
            self._audit("agent_approved", at, request_id, agent.id)
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
            changed = self.connection.execute(
                "UPDATE agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (at.isoformat(), agent_id),
            )
            if changed.rowcount:
                row = self.connection.execute(
                    "SELECT request_id FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                self._audit("agent_revoked", at, row["request_id"], agent_id)

    def create_session(self, token_hash: str, subject: str, scopes: tuple[str, ...], issued_at: datetime, expires_at: datetime) -> None:
        _aware(issued_at); _aware(expires_at)
        if not token_hash or not subject or not scopes or expires_at <= issued_at:
            raise ValueError("Invalid broker session")
        with self.connection:
            self.connection.execute("INSERT INTO broker_sessions VALUES (?, ?, ?, ?, ?, NULL)", (token_hash, subject, " ".join(scopes), issued_at.isoformat(), expires_at.isoformat()))

    def active_session(self, token_hash: str, now: datetime) -> BrokerSession | None:
        _aware(now)
        row = self.connection.execute("SELECT * FROM broker_sessions WHERE token_hash = ? AND revoked_at IS NULL", (token_hash,)).fetchone()
        if row is None or not datetime.fromisoformat(row["issued_at"]) <= now < datetime.fromisoformat(row["expires_at"]):
            return None
        return BrokerSession(row["token_hash"], row["subject"], tuple(row["scopes"].split()), datetime.fromisoformat(row["expires_at"]))

    def revoke_session(self, token_hash: str, now: datetime) -> None:
        with self.connection:
            self.connection.execute("UPDATE broker_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL", (now.isoformat(), token_hash))

    def consume_nonce(self, agent_id: str, nonce: bytes, now: datetime, expires_at: datetime) -> None:
        """Atomically reject replay; only a nonce digest survives this call."""
        _aware(now)
        _aware(expires_at)
        if len(nonce) != 32 or expires_at <= now:
            raise ValueError("Invalid nonce lifetime")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.active_agent(agent_id, now) is None:
                raise InvalidPairingState("Inactive agent")
            self.connection.execute("DELETE FROM consumed_nonces WHERE expires_at <= ?", (now.astimezone(timezone.utc).isoformat(),))
            try:
                self.connection.execute(
                    "INSERT INTO consumed_nonces VALUES (?, ?, ?)",
                    (agent_id, hashlib.sha256(nonce).hexdigest(), expires_at.astimezone(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError:
                raise InvalidPairingProof("Repeated nonce") from None

    def _grant_audit(self, event_type: str, at: datetime, *, principal_id: str | None = None,
                     scope: str | None = None, family_id: str | None = None) -> None:
        _aware(at)
        self.connection.execute(
            "INSERT INTO grant_audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (secrets.token_urlsafe(32), event_type, at.isoformat(), principal_id, scope, family_id),
        )

    def _public_jwk(self, jwk: dict) -> tuple[dict, str]:
        try:
            thumbprint = public_jwk_thumbprint(jwk)
        except ValueError as exc:
            raise ValueError("JWK must be a supported public key") from exc
        if jwk.get("kty") == "EC":
            stored = {"kty": "EC", "crv": jwk["crv"], "x": jwk["x"], "y": jwk["y"]}
        else:
            stored = {"kty": "OKP", "crv": jwk["crv"], "x": jwk["x"]}
        return stored, thumbprint

    def add_principal(
        self,
        principal_id: str,
        *,
        github_subject: str,
        role: str,
        now: datetime,
        jwk: dict | None = None,
        github_login: str | None = None,
    ) -> Principal:
        _aware(now)
        validate_role(role)
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 64:
            raise ValueError("Invalid principal id")
        if not isinstance(github_subject, str) or not github_subject.isdigit():
            raise ValueError("GitHub subject must be a numeric account id")
        login = self._cached_login(github_login)
        jwk_sql, thumb_sql, stored, thumbprint = self._jwk_columns(jwk)
        with self.connection:
            self._insert_principal_locked(
                principal_id, github_subject=github_subject, role=role, now=now,
                jwk_sql=jwk_sql, thumb_sql=thumb_sql, github_login=login,
            )
        return Principal(principal_id, github_subject, thumbprint, stored, role, now, None, login)

    def list_principals(self) -> list[Principal]:
        rows = self.connection.execute(
            "SELECT * FROM principals ORDER BY created_at, id"
        ).fetchall()
        return [self._principal_row(row) for row in rows]

    def get_principal(self, principal_id: str) -> Principal | None:
        row = self.connection.execute("SELECT * FROM principals WHERE id = ?", (principal_id,)).fetchone()
        return None if row is None else self._principal_row(row)

    def get_principal_by_github_subject(self, github_subject: str) -> Principal | None:
        """Active principal anchored to a numeric GitHub account id.

        Cached login is ignored. Prefer canonical github:{id} when present so
        OAuth and Device Flow keep the same grants after DPoP key enrollment.
        A single non-canonical row is selected explicitly; several fail closed.
        """
        if not isinstance(github_subject, str) or not github_subject.isdigit():
            raise ValueError("GitHub subject must be a numeric account id")
        rows = self.connection.execute(
            """SELECT * FROM principals WHERE github_subject = ? AND revoked_at IS NULL
               ORDER BY created_at, id""",
            (github_subject,),
        ).fetchall()
        principals = [self._principal_row(row) for row in rows]
        canonical_id = f"github:{github_subject}"
        for principal in principals:
            if principal.id == canonical_id:
                return principal
        if len(principals) == 1:
            return principals[0]
        return None

    def active_principal(self, principal_id: str) -> Principal | None:
        principal = self.get_principal(principal_id)
        if principal is None or principal.revoked_at is not None:
            return None
        return principal

    def allow_github_principal(
        self,
        *,
        github_subject: str,
        github_login: str,
        role: str,
        scopes: tuple[str, ...] | list[str],
        now: datetime,
        expires_at: datetime,
    ) -> Principal:
        _aware(now)
        _aware(expires_at)
        validate_role(role)
        if not isinstance(github_subject, str) or not github_subject.isdigit():
            raise ValueError("GitHub subject must be a numeric account id")
        login = self._cached_login(github_login)
        if login is None:
            raise ValueError("GitHub login cache is invalid")
        if not scopes:
            raise ScopeError("Requested scopes are required")
        checked: list[str] = []
        for scope in scopes:
            validate_scope(scope, role=role)
            if scope not in checked:
                checked.append(scope)
        validate_grant_ttl(now=now, expires_at=expires_at)
        principal_id = f"github:{github_subject}"
        if len(principal_id) > 64:
            raise ValueError("Invalid principal id")
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM principals WHERE id = ?", (principal_id,),
            ).fetchone()
            if row is None:
                self._insert_principal_locked(
                    principal_id, github_subject=github_subject, role=role, now=now,
                    jwk_sql="", thumb_sql="", github_login=login,
                )
            else:
                existing = self._principal_row(row)
                if existing.revoked_at is not None:
                    raise ValueError("Unknown or revoked principal")
                if existing.github_subject != github_subject:
                    raise ValueError("GitHub subject does not match principal")
                self.connection.execute(
                    "UPDATE principals SET github_login = ?, role = ? WHERE id = ?",
                    (login, role, principal_id),
                )
            for scope in checked:
                self._insert_grant_locked(principal_id, scope, now, expires_at)
        principal = self.get_principal(principal_id)
        if principal is None:
            raise ValueError("Unknown or revoked principal")
        return principal

    def enroll_principal_key(self, principal_id: str, jwk: dict, now: datetime) -> Principal:
        _aware(now)
        stored, thumbprint = self._public_jwk(jwk)
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM principals WHERE id = ?", (principal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown or revoked principal")
            principal = self._principal_row(row)
            if principal.revoked_at is not None:
                raise ValueError("Unknown or revoked principal")
            if principal.jwk is not None or principal.jwk_thumbprint:
                raise ValueError("Principal already has an enrolled DPoP key")
            self.connection.execute(
                "UPDATE principals SET jwk = ?, jwk_thumbprint = ? WHERE id = ?",
                (json.dumps(stored, separators=(",", ":"), sort_keys=True), thumbprint, principal_id),
            )
        enrolled = self.get_principal(principal_id)
        if enrolled is None:
            raise ValueError("Unknown or revoked principal")
        return enrolled

    def _cached_login(self, github_login: str | None) -> str | None:
        if github_login is None:
            return None
        if not isinstance(github_login, str) or not github_login or "/" in github_login:
            raise ValueError("GitHub login cache is invalid")
        if any(ch.isspace() for ch in github_login):
            raise ValueError("GitHub login cache is invalid")
        return github_login

    def _jwk_columns(self, jwk: dict | None) -> tuple[str, str, dict | None, str | None]:
        if jwk is None:
            return "", "", None, None
        stored, thumbprint = self._public_jwk(jwk)
        return json.dumps(stored, separators=(",", ":"), sort_keys=True), thumbprint, stored, thumbprint

    def _insert_principal_locked(
        self,
        principal_id: str,
        *,
        github_subject: str,
        role: str,
        now: datetime,
        jwk_sql: str,
        thumb_sql: str,
        github_login: str | None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO principals (
                   id, github_subject, jwk_thumbprint, jwk, role, created_at, revoked_at, github_login
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (principal_id, github_subject, thumb_sql, jwk_sql, role, now.isoformat(), github_login),
        )
        self._grant_audit("principal_added", now, principal_id=principal_id)

    def _principal_row(self, row: sqlite3.Row) -> Principal:
        revoked = None if row["revoked_at"] is None else datetime.fromisoformat(row["revoked_at"])
        raw_jwk = row["jwk"]
        if not raw_jwk:
            jwk = None
            thumbprint = None
        else:
            jwk = json.loads(raw_jwk)
            thumbprint = row["jwk_thumbprint"] or None
        login = None
        try:
            login = row["github_login"] or None
        except (IndexError, KeyError):
            login = None
        return Principal(
            row["id"], row["github_subject"], thumbprint, jwk,
            row["role"], datetime.fromisoformat(row["created_at"]), revoked, login,
        )

    def set_grant(self, principal_id: str, scope: str, now: datetime, expires_at: datetime) -> Grant:
        _aware(now)
        _aware(expires_at)
        validate_grant_ttl(now=now, expires_at=expires_at)
        principal = self.active_principal(principal_id)
        if principal is None:
            raise ValueError("Unknown or revoked principal")
        validate_scope(scope, role=principal.role)
        with self.connection:
            return self._insert_grant_locked(principal_id, scope, now, expires_at)

    def _insert_grant_locked(self, principal_id: str, scope: str, now: datetime, expires_at: datetime) -> Grant:
        grant = Grant(secrets.token_urlsafe(16), principal_id, scope, now, expires_at)
        self.connection.execute(
            "UPDATE grants SET revoked_at = ? WHERE principal_id = ? AND scope = ? AND revoked_at IS NULL",
            (now.isoformat(), principal_id, scope),
        )
        self.connection.execute(
            "INSERT INTO grants VALUES (?, ?, ?, ?, ?, NULL)",
            (grant.id, principal_id, scope, now.isoformat(), expires_at.isoformat()),
        )
        self._grant_audit("grant_set", now, principal_id=principal_id, scope=scope)
        return grant

    def list_grants(self, principal_id: str, now: datetime) -> list[Grant]:
        _aware(now)
        rows = self.connection.execute(
            """SELECT * FROM grants WHERE principal_id = ? AND revoked_at IS NULL AND expires_at > ?
               ORDER BY scope""",
            (principal_id, now.isoformat()),
        ).fetchall()
        return [Grant(row["id"], row["principal_id"], row["scope"],
                      datetime.fromisoformat(row["created_at"]),
                      datetime.fromisoformat(row["expires_at"])) for row in rows]

    def active_grant_scopes(self, principal_id: str, now: datetime) -> frozenset[str]:
        return frozenset(grant.scope for grant in self.list_grants(principal_id, now))

    def revoke_grant(self, principal_id: str, scope: str, now: datetime) -> None:
        _aware(now)
        validate_scope(scope)
        with self.connection:
            changed = self.connection.execute(
                """UPDATE grants SET revoked_at = ? WHERE principal_id = ? AND scope = ?
                   AND revoked_at IS NULL""",
                (now.isoformat(), principal_id, scope),
            )
            if changed.rowcount:
                self._grant_audit("grant_revoked", now, principal_id=principal_id, scope=scope)

    def create_family(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        refresh_token_hash: str,
        requested_scopes: tuple[str, ...],
        now: datetime,
        expires_at: datetime,
    ) -> TokenFamily:
        _aware(now)
        _aware(expires_at)
        if not workspace_id or not refresh_token_hash or not requested_scopes or expires_at <= now:
            raise ValueError("Invalid token family")
        principal = self.active_principal(principal_id)
        if principal is None:
            raise ValueError("Unknown or revoked principal")
        if not principal.jwk_thumbprint:
            raise ValueError("Principal has no enrolled DPoP key")
        family_id = secrets.token_urlsafe(32)
        family = TokenFamily(
            family_id, principal_id, workspace_id, refresh_token_hash, None, now, expires_at,
            requested_scopes,
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO token_families VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
                (family_id, principal_id, workspace_id, refresh_token_hash,
                 " ".join(requested_scopes), now.isoformat(), expires_at.isoformat()),
            )
            self.connection.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, NULL)",
                (refresh_token_hash, family_id, now.isoformat(), expires_at.isoformat()),
            )
        return family

    def family_active(self, family_id: str, now: datetime) -> bool:
        _aware(now)
        row = self.connection.execute(
            "SELECT revoked_at, expires_at FROM token_families WHERE id = ?", (family_id,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        return now < datetime.fromisoformat(row["expires_at"])

    def revoke_family(self, family_id: str, now: datetime) -> None:
        _aware(now)
        with self.connection:
            self._revoke_family_locked(family_id, now)

    def _revoke_family_locked(self, family_id: str, now: datetime) -> None:
        changed = self.connection.execute(
            "UPDATE token_families SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now.isoformat(), family_id),
        )
        if changed.rowcount:
            row = self.connection.execute(
                "SELECT principal_id FROM token_families WHERE id = ?", (family_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE refresh_tokens SET consumed_at = ? WHERE family_id = ? AND consumed_at IS NULL",
                (now.isoformat(), family_id),
            )
            self._grant_audit("family_revoked", now, principal_id=None if row is None else row["principal_id"],
                              family_id=family_id)

    def rotate_refresh(
        self,
        token_hash: str,
        *,
        now: datetime,
        new_token_hash: str,
        new_expires_at: datetime | None = None,
    ) -> TokenFamily:
        _aware(now)
        if not token_hash or not new_token_hash:
            raise InvalidGrant("Invalid refresh token")
        reuse = False
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT r.token_hash, r.consumed_at, r.expires_at AS token_expires,
                          f.id AS family_id, f.principal_id, f.workspace_id, f.current_token_hash,
                          f.previous_token_hash, f.requested_scopes, f.created_at,
                          f.expires_at AS family_expires, f.revoked_at
                   FROM refresh_tokens r JOIN token_families f ON f.id = r.family_id
                   WHERE r.token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise InvalidGrant("Invalid refresh token")
            family_expires = datetime.fromisoformat(row["family_expires"])
            token_expires = datetime.fromisoformat(row["token_expires"])
            if row["revoked_at"] is not None or now >= family_expires or now >= token_expires:
                raise InvalidGrant("Invalid refresh token")
            successor_expires = new_expires_at or family_expires
            if successor_expires > family_expires:
                successor_expires = family_expires
            if successor_expires <= now:
                raise InvalidGrant("Invalid refresh token")
            if row["current_token_hash"] == token_hash:
                self.connection.execute(
                    "UPDATE refresh_tokens SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL",
                    (now.isoformat(), token_hash),
                )
                changed = self.connection.execute(
                    """UPDATE token_families SET previous_token_hash = current_token_hash,
                       current_token_hash = ? WHERE id = ? AND current_token_hash = ? AND revoked_at IS NULL""",
                    (new_token_hash, row["family_id"], token_hash),
                )
                if changed.rowcount != 1:
                    raise InvalidGrant("Invalid refresh token")
                self.connection.execute(
                    "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, NULL)",
                    (new_token_hash, row["family_id"], now.isoformat(), successor_expires.isoformat()),
                )
                return TokenFamily(
                    row["family_id"], row["principal_id"], row["workspace_id"], new_token_hash,
                    token_hash, datetime.fromisoformat(row["created_at"]), family_expires,
                    tuple(row["requested_scopes"].split()),
                )
            self._revoke_family_locked(row["family_id"], now)
            reuse = True
        if reuse:
            raise RefreshReuse("Refresh token reuse revoked the family")

    def consume_dpop_jti(self, jti: str, now: datetime, expires_at: datetime) -> None:
        _aware(now)
        _aware(expires_at)
        if not jti or expires_at <= now:
            raise InvalidPairingProof("Invalid DPoP replay lifetime")
        digest = hashlib.sha256(jti.encode("utf-8")).hexdigest()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "DELETE FROM dpop_replays WHERE expires_at <= ?", (now.astimezone(timezone.utc).isoformat(),)
            )
            try:
                self.connection.execute(
                    "INSERT INTO dpop_replays VALUES (?, ?)",
                    (digest, expires_at.astimezone(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError:
                raise InvalidPairingProof("Repeated DPoP jti") from None
