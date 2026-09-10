"""HTTP boundary. Construct using create_app with explicitly trusted adapters.

Query authentication: X-Agent-Envelope is standard base64 of canonical JSON
(sorted keys, compact separators, ASCII escaping), with exactly agent_id,
audience, timestamp (YYYY-MM-DDTHH:MM:SSZ), nonce (base64url of 32 random bytes,
no padding), and body_sha256 (hex digest of the exact HTTP body). The detached
X-Agent-Signature is standard base64 Ed25519 over those canonical JSON bytes.
Only POST /v1/context/query accepts this envelope. No bearer authenticates it.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, HTTPException, Request

from app.adapters.hermes import HermesClient, HermesError
from app.adapters.github import GitHubOAuth, GitHubOAuthError
from app.adapters.owner import OwnerAssertionVerifier, OwnerAuthenticationError
from app.adapters.sqlite import SqlitePairingStore
from app.agent_card import build_agent_card
from app.domain.pairing import create_request


def _object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except RecursionError:
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


async def _body(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Expected application/json")
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > 16384:
            raise HTTPException(413, "Request body too large")
    return bytes(result)


def create_app(*, database_path: str | Path, audience: str,
               owner_verifier: OwnerAssertionVerifier, hermes: HermesClient,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
               agent_lifetime: timedelta = timedelta(hours=1),
               public_url: str = "https://a2a.mathai.com.br",
               github_oauth: GitHubOAuth | None = None,
               session_lifetime: timedelta | None = None,
               github_redirect_uri: str | None = None,
               github_allowed_user_id: str | None = None) -> FastAPI:
    if not audience or not timedelta(0) < agent_lifetime <= timedelta(hours=1):
        raise ValueError("Audience and agent lifetime of at most one hour are required")
    app = FastAPI(title="Agent Pairing Broker", version="0.0.1")
    session_lifetime = session_lifetime or agent_lifetime

    @app.get("/.well-known/agent-card.json")
    def agent_card():
        return build_agent_card(public_url)

    @app.post("/v1/oauth/github/start")
    def github_start(body: bytes = Depends(_body)):
        if github_oauth is None or not github_redirect_uri:
            raise HTTPException(503, "GitHub OAuth is not configured")
        state = secrets.token_urlsafe(32); now = clock()
        with closing(SqlitePairingStore(database_path)) as store:
            store.create_oauth_state(hashlib.sha256(state.encode()).hexdigest(), github_redirect_uri, now, now + timedelta(minutes=10))
        return {"authorization_url": "https://github.com/login/oauth/authorize", "state": state, "redirect_uri": github_redirect_uri}

    @app.post("/v1/oauth/github/token")
    def github_token(body: bytes = Depends(_body)):
        if github_oauth is None:
            raise HTTPException(503, "GitHub OAuth is not configured")
        try:
            data = _object(body)
            if set(data) != {"code", "state", "redirect_uri", "scope"} or not all(isinstance(data[k], str) for k in data):
                raise ValueError()
            if not github_redirect_uri or data["redirect_uri"] != github_redirect_uri:
                raise ValueError()
            requested = tuple(data["scope"].split())
            if set(requested) != {"a2a:discover", "a2a:message", "a2a:history"}:
                raise ValueError()
            with closing(SqlitePairingStore(database_path)) as store:
                if not store.consume_oauth_state(hashlib.sha256(data["state"].encode()).hexdigest(), data["redirect_uri"], clock()):
                    raise ValueError()
            subject = github_oauth.exchange_and_identify(data["code"], data["redirect_uri"])
            if github_allowed_user_id is None or subject != github_allowed_user_id:
                raise ValueError()
            token = secrets.token_urlsafe(32)
            now = clock()
            with closing(SqlitePairingStore(database_path)) as store:
                store.create_session(hashlib.sha256(token.encode()).hexdigest(), subject, requested, now, now + session_lifetime)
            return {"access_token": token, "token_type": "Bearer", "scope": " ".join(requested), "expires_in": int(session_lifetime.total_seconds())}
        except (ValueError, TypeError, GitHubOAuthError):
            raise HTTPException(403, "GitHub OAuth denied") from None

    @app.post("/v1/oauth/revoke")
    def revoke_session(request: Request):
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            raise HTTPException(401, "Missing session")
        with closing(SqlitePairingStore(database_path)) as store:
            store.revoke_session(hashlib.sha256(token[7:].encode()).hexdigest(), clock())
        return {"status": "revoked"}

    @app.post("/v1/pairing-requests", status_code=201)
    def new_pairing(body: bytes = Depends(_body)):
        try:
            data = _object(body)
            if set(data) != {"public_key"} or not isinstance(data["public_key"], str):
                raise ValueError()
            pairing = create_request(data["public_key"], clock())
            with closing(SqlitePairingStore(database_path)) as store:
                store.create(pairing)
        except ValueError:
            raise HTTPException(400, "Invalid pairing request") from None
        return {"id": pairing.id, "challenge": base64.b64encode(pairing.challenge).decode(),
                "expires_at": pairing.expires_at.isoformat(), "status": pairing.status}

    @app.post("/v1/pairing-requests/{request_id}/proof")
    def proof(request_id: str, body: bytes = Depends(_body)):
        try:
            data = _object(body)
            if set(data) != {"challenge", "signature"}:
                raise ValueError()
            challenge = base64.b64decode(data["challenge"], validate=True)
            signature = base64.b64decode(data["signature"], validate=True)
            with closing(SqlitePairingStore(database_path)) as store:
                store.verify_proof(request_id, challenge, signature, clock())
        except (ValueError, TypeError):
            raise HTTPException(403, "Invalid pairing proof") from None
        return {"status": "proof_verified"}

    @app.post("/v1/pairing-requests/{request_id}/approve")
    def approve(request_id: str, request: Request, body: bytes = Depends(_body)):
        try:
            assertion = request.headers.get("cf-access-jwt-assertion", "")
            if not assertion or len(assertion) > 16384:
                raise OwnerAuthenticationError()
            owner_verifier.verify(assertion)
            if _object(body) != {}:
                raise ValueError()
            now = clock()
            with closing(SqlitePairingStore(database_path)) as store:
                agent = store.approve(request_id, now, now + agent_lifetime)
        except (ValueError, TypeError):
            raise HTTPException(403, "Owner approval denied") from None
        return {"agent_id": agent.id, "expires_at": agent.expires_at.isoformat(), "status": "approved"}

    @app.post("/v1/context/query")
    def query(request: Request, body: bytes = Depends(_body)):
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer ") and not request.headers.get("x-agent-envelope"):
            token = authorization[7:]
            try:
                data = _object(body)
                if set(data) != {"query"} or not isinstance(data["query"], str) or not data["query"].strip():
                    raise ValueError()
                with closing(SqlitePairingStore(database_path)) as store:
                    session = store.active_session(hashlib.sha256(token.encode()).hexdigest(), clock())
                if session is None or "a2a:message" not in session.scopes:
                    raise ValueError()
                return hermes.query_as_broker(agent_id="github:" + session.subject, query=data["query"])
            except (ValueError, TypeError, HermesError):
                raise HTTPException(403, "Agent authentication denied") from None
        try:
            encoded = request.headers.get("x-agent-envelope", "")
            signature = request.headers.get("x-agent-signature", "")
            if len(encoded) > 4096 or len(signature) > 128:
                raise ValueError()
            raw = base64.b64decode(encoded, validate=True)
            envelope = _object(raw)
            if set(envelope) != {"agent_id", "audience", "timestamp", "nonce", "body_sha256"} or not all(isinstance(v, str) for v in envelope.values()):
                raise ValueError()
            if raw != json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode():
                raise ValueError()
            if envelope["audience"] != audience:
                raise ValueError()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", envelope["timestamp"]):
                raise ValueError()
            timestamp = datetime.strptime(envelope["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            now = clock()
            if abs((now - timestamp).total_seconds()) >= 60:
                raise ValueError()
            nonce_text = envelope["nonce"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce_text):
                raise ValueError()
            nonce = base64.urlsafe_b64decode(nonce_text + "=")
            if base64.urlsafe_b64encode(nonce).decode().rstrip("=") != nonce_text:
                raise ValueError()
            if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), envelope["body_sha256"]):
                raise ValueError()
            data = _object(body)
            if set(data) != {"query"} or not isinstance(data["query"], str) or not data["query"].strip():
                raise ValueError()
            with closing(SqlitePairingStore(database_path)) as store:
                agent = store.active_agent(envelope["agent_id"], now)
                if agent is None:
                    raise ValueError()
                key = Ed25519PublicKey.from_public_bytes(base64.b64decode(agent.public_key, validate=True))
                key.verify(base64.b64decode(signature, validate=True), raw)
                store.consume_nonce(agent.id, nonce, now, timestamp + timedelta(seconds=60))
        except (ValueError, TypeError, InvalidSignature):
            raise HTTPException(403, "Agent authentication denied") from None
        try:
            return hermes.query_as_broker(agent_id=agent.id, query=data["query"])
        except HermesError:
            raise HTTPException(502, "Hermes query failed") from None

    return app
