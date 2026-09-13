"""HTTP boundary. Construct using create_app with explicitly trusted adapters.

POST /v1/context/query accepts three authentication modes:
- Legacy Ed25519 envelope (X-Agent-Envelope / X-Agent-Signature).
- Legacy opaque Bearer session from GitHub Device Flow, limited to a2a scopes
  and the Hermes proxy.
- DPoP-bound swarm access tokens. Those never fall through to Hermes; ctx
  grants do not open the proxy. Query/resolve/propose delegate to the C/D
  routers when their factories are supplied, and return 503 otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from contextlib import asynccontextmanager, closing
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapters.hermes import HermesClient, HermesError
from app.adapters.github import GitHubOAuth, GitHubOAuthError
from app.adapters.owner import OwnerAssertionVerifier, OwnerAuthenticationError
from app.adapters.sqlite import InvalidGrant, RefreshReuse, SqlitePairingStore
from app.mcp_oauth import build_router as build_mcp_oauth_router
from app.mcp_oauth.github import GitHubAuthorizationCode
from app.remote_mcp import TransportSecuritySettings, attach_mcp, build_remote_mcp
from app.agent_card import build_agent_card
from app.auth.dpop import (
    DPOP_IAT_WINDOW,
    InvalidDpopProof,
    access_token_hash,
    canonical_htu,
    verify_dpop_proof,
)
from app.auth.scopes import ScopeError, classifications_for, effective_scopes, requested_scopes
from app.auth.tokens import AccessTokenIssuer, InvalidAccessToken
from app.domain.pairing import InvalidPairingProof, create_request


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


def _mounted(router, path: str):
    """Endpoint of a C/D router, called directly so authorize() runs exactly once.

    Delegation instead of include_router: /v1/context/query must keep dispatching
    legacy Bearer sessions to Hermes, and a mounted route would shadow that. The
    endpoints take (request, body) with the same 415/413 contract as _body, so the
    DPoP proof is consumed by the router, never twice.
    """
    if router is None:
        return None
    for route in router.routes:
        if getattr(route, "path", None) == path and "POST" in getattr(route, "methods", ()):
            return route.endpoint
    raise RuntimeError(f"Router does not expose POST {path}")


def _attach_remote_mcp(app, authorize, public_url: str, *, query, resolve, propose, ask):
    parsed = urlparse(public_url)
    host = parsed.hostname
    if not host:
        raise ValueError("public_url must include a hostname")
    origin = public_url.rstrip("/")
    remote = build_remote_mcp(
        authorize=authorize,
        query=query,
        resolve=resolve,
        propose=propose,
        ask=ask,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{host}:*"],
            allowed_origins=[origin],
        ),
        host=host,
    )
    attach_mcp(app, remote)
    app.state.remote_mcp = remote
    return remote


def _oauth_error(error: str, status: int = 400, *, headers: dict[str, str] | None = None) -> JSONResponse:
    response = JSONResponse({"error": error}, status_code=status)
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


_DPOP_AUTHENTICATE = {
    "WWW-Authenticate": 'DPoP algs="ES256 EdDSA", error="invalid_dpop_proof"',
}


@dataclass
class _DeviceTx:
    mode: str
    github_code: str
    expires_at: datetime
    next_poll: datetime
    interval: int
    principal_id: str | None = None
    jkt: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)


class _DpopFailed(Exception):
    """Internal sentinel so DPoP failures do not leak proof material."""


def create_app(*, database_path: str | Path, audience: str,
               owner_verifier: OwnerAssertionVerifier, hermes: HermesClient,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
               agent_lifetime: timedelta = timedelta(hours=1),
               public_url: str = "https://a2a.mathai.com.br",
               github_oauth: GitHubOAuth | None = None,
               session_lifetime: timedelta | None = None,
               github_allowed_user_id: str | None = None,
               token_signing_key: str | bytes | None = None,
               workspace_id: str | None = None,
               issuer: str | None = None,
               access_token_lifetime: timedelta = timedelta(minutes=5),
               context_router_factory: Callable | None = None,
               proposal_router_factory: Callable | None = None,
               mcp_github: GitHubAuthorizationCode | None = None,
               mcp_registration_limit: int = 32,
               ask_router_factory: Callable | None = None,
               mcp_query: Callable | None = None,
               mcp_resolve: Callable | None = None,
               mcp_propose: Callable | None = None,
               mcp_ask: Callable | None = None) -> FastAPI:
    if not audience or not timedelta(0) < agent_lifetime <= timedelta(hours=1):
        raise ValueError("Audience and agent lifetime of at most one hour are required")
    if not timedelta(0) < access_token_lifetime <= timedelta(hours=1):
        raise ValueError("Access token lifetime must be at most one hour")
    remote_holder: dict[str, object] = {"remote": None}

    @asynccontextmanager
    async def lifespan(_app):
        remote = remote_holder["remote"]
        if remote is None:
            yield
            return
        async with remote.lifespan(_app):
            yield

    app = FastAPI(
        title="Agent Pairing Broker", version="0.0.1",
        lifespan=lifespan, redirect_slashes=False,
    )
    session_lifetime = session_lifetime or agent_lifetime
    device_transactions: dict[str, _DeviceTx] = {}
    token_issuer = (
        AccessTokenIssuer(
            signing_key=token_signing_key,
            issuer=issuer or public_url,
            audience=audience,
            lifetime=access_token_lifetime,
        )
        if token_signing_key else None
    )
    context_installed = context_router_factory is not None
    proposal_installed = proposal_router_factory is not None
    ask_installed = ask_router_factory is not None

    def _store() -> SqlitePairingStore:
        return SqlitePairingStore(database_path)

    def _dpop_error() -> JSONResponse:
        return _oauth_error("invalid_dpop_proof", 401, headers=_DPOP_AUTHENTICATE)

    def _consume_dpop(proof: str, *, htm: str, path: str, ath: str | None = None):
        try:
            verified = verify_dpop_proof(
                proof, htm=htm, htu=canonical_htu(public_url, path), now=clock(), ath=ath,
            )
            with closing(_store()) as store:
                store.consume_dpop_jti(verified.jti, clock(), clock() + DPOP_IAT_WINDOW)
        except (InvalidDpopProof, InvalidPairingProof):
            raise _DpopFailed() from None
        return verified

    def authorize(request: Request) -> dict:
        if token_issuer is None or not workspace_id:
            raise HTTPException(503, "Swarm token verification is not configured")
        authorization = request.headers.get("authorization", "")
        proof = request.headers.get("dpop", "")
        if not authorization.startswith("DPoP ") or not proof:
            raise HTTPException(401, "Missing DPoP credential", headers=_DPOP_AUTHENTICATE)
        access = authorization[5:].strip()
        try:
            principal = token_issuer.verify(access, clock())
        except InvalidAccessToken:
            raise HTTPException(401, "Invalid access token") from None
        if principal.workspace_id != workspace_id:
            raise HTTPException(401, "Invalid access token")
        try:
            verified = _consume_dpop(
                proof, htm=request.method.upper(), path=request.url.path, ath=access_token_hash(access),
            )
        except _DpopFailed:
            raise HTTPException(401, "Invalid DPoP proof", headers=_DPOP_AUTHENTICATE) from None
        if verified.jkt != principal.jkt:
            raise HTTPException(401, "Invalid DPoP proof", headers=_DPOP_AUTHENTICATE)
        with closing(_store()) as store:
            if not store.family_active(principal.family_id, clock()):
                raise HTTPException(401, "Invalid access token")
        return principal.as_mapping()

    def _require_swarm_issuer() -> AccessTokenIssuer:
        if token_issuer is None or not workspace_id:
            raise HTTPException(503, "Swarm token issuance is not configured")
        return token_issuer

    def _mint(store: SqlitePairingStore, principal, requested: tuple[str, ...], now: datetime,
              *, family_id: str, family_exp: datetime):
        granted = store.active_grant_scopes(principal.id, now)
        scopes = effective_scopes(requested, granted, principal.role)
        active = [grant for grant in store.list_grants(principal.id, now) if grant.scope in scopes]
        bound = min(grant.expires_at for grant in active)
        bound = min(bound, family_exp)
        access_exp = min(now + access_token_lifetime, bound)
        if access_exp <= now:
            raise ScopeError("Effective scope set is empty")
        token = _require_swarm_issuer().issue(
            principal_id=principal.id,
            workspace_id=workspace_id,
            scopes=scopes,
            classifications=classifications_for(principal.role),
            family_id=family_id,
            jkt=principal.jwk_thumbprint,
            now=now,
            expires_at=access_exp,
        )
        return token, scopes, access_exp

    def _issue_new_family(store: SqlitePairingStore, principal, requested: tuple[str, ...], now: datetime) -> dict:
        granted = store.active_grant_scopes(principal.id, now)
        scopes = effective_scopes(requested, granted, principal.role)
        active = [grant for grant in store.list_grants(principal.id, now) if grant.scope in scopes]
        family_exp = min(grant.expires_at for grant in active)
        refresh = secrets.token_urlsafe(32)
        family = store.create_family(
            principal_id=principal.id,
            workspace_id=workspace_id,
            refresh_token_hash=hashlib.sha256(refresh.encode()).hexdigest(),
            requested_scopes=requested,
            now=now,
            expires_at=family_exp,
        )
        token, scopes, access_exp = _mint(store, principal, requested, now, family_id=family.id, family_exp=family_exp)
        return {
            "access_token": token,
            "refresh_token": refresh,
            "token_type": "DPoP",
            "scope": " ".join(scopes),
            "expires_in": int((access_exp - now).total_seconds()),
        }

    mcp_oauth = None
    if mcp_github is not None:
        if token_signing_key is None or not workspace_id:
            raise ValueError("MCP OAuth requires a signing key and workspace_id")
        mcp_oauth = build_mcp_oauth_router(
            database_path=database_path,
            github=mcp_github,
            signing_key=token_signing_key,
            workspace_id=workspace_id,
            clock=clock,
            public_url=public_url,
            registration_limit=mcp_registration_limit,
        )
        app.include_router(mcp_oauth.router)
    app.state.authorize = authorize
    app.state.mcp_authorize = None if mcp_oauth is None else mcp_oauth.authorize
    app.state.mcp_oauth = mcp_oauth
    app.state.context_router_factory = context_router_factory
    app.state.proposal_router_factory = proposal_router_factory
    app.state.ask_router_factory = ask_router_factory
    context_router = context_router_factory(authorize=authorize) if context_installed else None
    proposal_router = proposal_router_factory(authorize=authorize) if proposal_installed else None
    ask_router = ask_router_factory(authorize=authorize) if ask_installed else None
    context_query = _mounted(context_router, "/v1/context/query")
    context_resolve = _mounted(context_router, "/v1/context/resolve")
    proposal_propose = _mounted(proposal_router, "/v1/context/propose")
    context_ask = _mounted(ask_router, "/v1/context/ask")
    if mcp_oauth is not None:
        remote_holder["remote"] = _attach_remote_mcp(
            app, mcp_oauth.authorize, public_url,
            query=mcp_query, resolve=mcp_resolve, propose=mcp_propose, ask=mcp_ask,
        )

    @app.get("/.well-known/agent-card.json")
    def agent_card():
        return build_agent_card(public_url)

    @app.post("/v1/oauth/github/device/start")
    def device_start(request: Request, body: bytes = Depends(_body)):
        if github_oauth is None:
            raise HTTPException(503, "GitHub OAuth is not configured")
        try:
            data = _object(body) if body else {}
        except ValueError:
            raise HTTPException(400, "Invalid JSON") from None
        proof = request.headers.get("dpop")
        if proof or data:
            return _device_start_dpop(data, proof)
        try:
            payload = github_oauth.start_device()
            now = clock()
            if len(device_transactions) >= 256:
                device_transactions.pop(next(iter(device_transactions)))
            interval = max(5, int(payload.get("interval", 5)))
            transaction = secrets.token_urlsafe(32)
            device_transactions[transaction] = _DeviceTx(
                "legacy", payload["device_code"], now + timedelta(minutes=10), now, interval,
            )
            return {"device_code": transaction, "user_code": payload["user_code"],
                    "verification_uri": payload["verification_uri"], "expires_in": 600, "interval": interval}
        except (KeyError, GitHubOAuthError):
            raise HTTPException(502, "GitHub device flow unavailable") from None

    def _device_start_dpop(data: dict, proof: str | None):
        _require_swarm_issuer()
        if proof is None:
            return _dpop_error()
        if set(data) != {"principal_id", "scopes"} or not isinstance(data.get("principal_id"), str):
            return _oauth_error("access_denied")
        try:
            scopes = requested_scopes(data["scopes"])
        except ScopeError:
            return _oauth_error("access_denied")
        try:
            verified = _consume_dpop(proof, htm="POST", path="/v1/oauth/github/device/start")
        except _DpopFailed:
            return _dpop_error()
        with closing(_store()) as store:
            principal = store.active_principal(data["principal_id"])
        if principal is None or principal.jwk is None or not principal.jwk_thumbprint:
            return _oauth_error("access_denied")
        if verified.jkt != principal.jwk_thumbprint:
            return _oauth_error("access_denied")
        try:
            payload = github_oauth.start_device()
            now = clock()
            if len(device_transactions) >= 256:
                device_transactions.pop(next(iter(device_transactions)))
            interval = max(5, int(payload.get("interval", 5)))
            transaction = secrets.token_urlsafe(32)
            device_transactions[transaction] = _DeviceTx(
                "dpop", payload["device_code"], now + timedelta(minutes=10), now, interval,
                principal.id, principal.jwk_thumbprint, scopes,
            )
            return {"device_code": transaction, "user_code": payload["user_code"],
                    "verification_uri": payload["verification_uri"], "expires_in": 600, "interval": interval}
        except (KeyError, GitHubOAuthError):
            raise HTTPException(502, "GitHub device flow unavailable") from None

    @app.post("/v1/oauth/github/device/poll")
    def device_poll(request: Request, body: bytes = Depends(_body)):
        if github_oauth is None:
            raise HTTPException(503, "GitHub OAuth is not configured")
        try:
            data = _object(body)
            if set(data) != {"device_code", "grant_type"} or data["grant_type"] != "urn:ietf:params:oauth:grant-type:device_code":
                raise ValueError()
            transaction = data["device_code"]
            tx = device_transactions[transaction]
            if tx.expires_at <= clock():
                del device_transactions[transaction]
                raise ValueError()
            if tx.mode == "dpop":
                return _device_poll_dpop(request, transaction, tx)
            if clock() < tx.next_poll:
                return {"status": "authorization_pending", "retry_after": int((tx.next_poll - clock()).total_seconds()) + 1}
            subject = github_oauth.poll_device(tx.github_code)
            if subject is None:
                tx.next_poll = clock() + timedelta(seconds=tx.interval)
                return {"status": "authorization_pending"}
            if github_allowed_user_id is None or subject != github_allowed_user_id:
                del device_transactions[transaction]
                raise ValueError()
            del device_transactions[transaction]
            token = secrets.token_urlsafe(32)
            now = clock()
            scopes = ("a2a:discover", "a2a:message", "a2a:history")
            with closing(_store()) as store:
                store.create_session(hashlib.sha256(token.encode()).hexdigest(), subject, scopes, now, now + session_lifetime)
            return {"access_token": token, "token_type": "Bearer", "scope": " ".join(scopes),
                    "expires_in": int(session_lifetime.total_seconds())}
        except (KeyError, TypeError, ValueError, GitHubOAuthError):
            raise HTTPException(403, "GitHub device authorization denied") from None

    def _device_poll_dpop(request: Request, transaction: str, tx: _DeviceTx):
        proof = request.headers.get("dpop")
        if not proof:
            return _dpop_error()
        try:
            verified = _consume_dpop(proof, htm="POST", path="/v1/oauth/github/device/poll")
        except _DpopFailed:
            return _dpop_error()
        if verified.jkt != tx.jkt:
            return _dpop_error()
        if clock() < tx.next_poll:
            return _oauth_error("slow_down")
        try:
            subject = github_oauth.poll_device(tx.github_code)
        except GitHubOAuthError:
            del device_transactions[transaction]
            return _oauth_error("access_denied")
        if subject is None:
            tx.next_poll = clock() + timedelta(seconds=tx.interval)
            return _oauth_error("authorization_pending")
        with closing(_store()) as store:
            principal = store.active_principal(tx.principal_id)
            if (
                principal is None
                or principal.jwk is None
                or not principal.jwk_thumbprint
                or subject != principal.github_subject
                or verified.jkt != principal.jwk_thumbprint
            ):
                del device_transactions[transaction]
                return _oauth_error("access_denied")
            now = clock()
            try:
                tokens = _issue_new_family(store, principal, tx.scopes, now)
            except ScopeError:
                del device_transactions[transaction]
                return _oauth_error("access_denied")
        del device_transactions[transaction]
        return tokens

    @app.post("/v1/oauth/token")
    def refresh_tokens(request: Request, body: bytes = Depends(_body)):
        _require_swarm_issuer()
        proof = request.headers.get("dpop")
        if not proof:
            return _dpop_error()
        try:
            verified = _consume_dpop(proof, htm="POST", path="/v1/oauth/token")
        except _DpopFailed:
            return _dpop_error()
        try:
            data = _object(body)
        except ValueError:
            return _oauth_error("invalid_grant")
        if set(data) != {"grant_type", "refresh_token"} or data["grant_type"] != "refresh_token":
            return _oauth_error("invalid_grant")
        presented = data.get("refresh_token")
        if not isinstance(presented, str) or not presented:
            return _oauth_error("invalid_grant")
        presented_hash = hashlib.sha256(presented.encode()).hexdigest()
        now = clock()
        new_refresh = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(new_refresh.encode()).hexdigest()
        try:
            with closing(_store()) as store:
                probe = store.connection.execute(
                    """SELECT f.principal_id, f.id AS family_id, f.requested_scopes, f.expires_at
                       FROM refresh_tokens r
                       JOIN token_families f ON f.id = r.family_id WHERE r.token_hash = ?""",
                    (presented_hash,),
                ).fetchone()
                if probe is None:
                    return _oauth_error("invalid_grant")
                principal = store.active_principal(probe["principal_id"])
                if principal is None or verified.jkt != principal.jwk_thumbprint:
                    return _oauth_error("invalid_grant")
                requested = tuple(probe["requested_scopes"].split())
                effective_scopes(requested, store.active_grant_scopes(principal.id, now), principal.role)
                family = store.rotate_refresh(presented_hash, now=now, new_token_hash=new_hash)
                token, scopes, access_exp = _mint(
                    store, principal, family.requested_scopes, now,
                    family_id=family.id, family_exp=family.expires_at,
                )
        except RefreshReuse:
            return _oauth_error("invalid_grant")
        except (InvalidGrant, ScopeError):
            return _oauth_error("invalid_grant")
        return {
            "access_token": token,
            "refresh_token": new_refresh,
            "token_type": "DPoP",
            "scope": " ".join(scopes),
            "expires_in": int((access_exp - now).total_seconds()),
        }

    @app.post("/v1/oauth/revoke")
    def revoke_session(request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("DPoP "):
            try:
                mapping = authorize(request)
            except HTTPException as exc:
                if exc.status_code in {401, 503}:
                    raise
                raise
            with closing(_store()) as store:
                store.revoke_family(mapping["family_id"], clock())
            return {"status": "revoked"}
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing session")
        with closing(_store()) as store:
            store.revoke_session(hashlib.sha256(authorization[7:].encode()).hexdigest(), clock())
        return {"status": "revoked"}

    @app.get("/v1/context/capabilities")
    def capabilities(request: Request):
        mapping = authorize(request)
        operations = ["capabilities"]
        if context_installed:
            operations.extend(["query", "resolve"])
        if ask_installed:
            operations.append("ask")
        if proposal_installed:
            operations.append("propose")
        return {
            "principal_id": mapping["principal_id"],
            "workspace_id": mapping["workspace_id"],
            "scopes": list(mapping["scopes"]),
            "classifications": list(mapping["classifications"]),
            "operations": operations,
        }

    @app.post("/v1/context/resolve")
    def resolve(request: Request, body: bytes = Depends(_body)):
        if context_resolve is not None:
            return context_resolve(request, body)
        mapping = authorize(request)
        if not any(scope.startswith("ctx:read:") for scope in mapping["scopes"]):
            raise HTTPException(403, "Operation is outside the granted scope")
        raise HTTPException(503, "Context resolve is not installed")

    @app.post("/v1/context/propose")
    def propose(request: Request, body: bytes = Depends(_body)):
        if proposal_propose is not None:
            return proposal_propose(request, body)
        mapping = authorize(request)
        if not any(scope.startswith("ctx:propose:") for scope in mapping["scopes"]):
            raise HTTPException(403, "Operation is outside the granted scope")
        raise HTTPException(503, "Context propose is not installed")

    @app.post("/v1/context/ask")
    def ask(request: Request, body: bytes = Depends(_body)):
        if context_ask is not None:
            return context_ask(request, body)
        mapping = authorize(request)
        if not any(scope.startswith("ctx:read:") for scope in mapping["scopes"]):
            raise HTTPException(403, "Operation is outside the granted scope")
        raise HTTPException(503, "Ask generator is not installed")

    @app.post("/v1/pairing-requests", status_code=201)
    def new_pairing(body: bytes = Depends(_body)):
        if owner_verifier is None:
            raise HTTPException(404, "Legacy pairing is disabled")
        try:
            data = _object(body)
            if set(data) != {"public_key"} or not isinstance(data["public_key"], str):
                raise ValueError()
            pairing = create_request(data["public_key"], clock())
            with closing(_store()) as store:
                store.create(pairing)
        except ValueError:
            raise HTTPException(400, "Invalid pairing request") from None
        return {"id": pairing.id, "challenge": base64.b64encode(pairing.challenge).decode(),
                "expires_at": pairing.expires_at.isoformat(), "status": pairing.status}

    @app.post("/v1/pairing-requests/{request_id}/proof")
    def proof(request_id: str, body: bytes = Depends(_body)):
        if owner_verifier is None:
            raise HTTPException(404, "Legacy pairing is disabled")
        try:
            data = _object(body)
            if set(data) != {"challenge", "signature"}:
                raise ValueError()
            challenge = base64.b64decode(data["challenge"], validate=True)
            signature = base64.b64decode(data["signature"], validate=True)
            with closing(_store()) as store:
                store.verify_proof(request_id, challenge, signature, clock())
        except (ValueError, TypeError):
            raise HTTPException(403, "Invalid pairing proof") from None
        return {"status": "proof_verified"}

    @app.post("/v1/pairing-requests/{request_id}/approve")
    def approve(request_id: str, request: Request, body: bytes = Depends(_body)):
        if owner_verifier is None:
            raise HTTPException(404, "Legacy pairing is disabled")
        try:
            assertion = request.headers.get("cf-access-jwt-assertion", "")
            if not assertion or len(assertion) > 16384:
                raise OwnerAuthenticationError()
            owner_verifier.verify(assertion)
            if _object(body) != {}:
                raise ValueError()
            now = clock()
            with closing(_store()) as store:
                agent = store.approve(request_id, now, now + agent_lifetime)
        except (ValueError, TypeError):
            raise HTTPException(403, "Owner approval denied") from None
        return {"agent_id": agent.id, "expires_at": agent.expires_at.isoformat(), "status": "approved"}

    @app.post("/v1/context/query")
    def query(request: Request, body: bytes = Depends(_body)):
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("DPoP ") or request.headers.get("dpop"):
            if context_query is not None:
                return context_query(request, body)
            mapping = authorize(request)
            if not any(scope.startswith("ctx:read:") for scope in mapping["scopes"]):
                raise HTTPException(403, "Operation is outside the granted scope")
            raise HTTPException(503, "Context query is not installed")
        if authorization.startswith("Bearer ") and not request.headers.get("x-agent-envelope"):
            token = authorization[7:]
            try:
                data = _object(body)
                if set(data) != {"query"} or not isinstance(data["query"], str) or not data["query"].strip():
                    raise ValueError()
                with closing(_store()) as store:
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
            with closing(_store()) as store:
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
