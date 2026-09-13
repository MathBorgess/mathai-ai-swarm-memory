"""Remote MCP OAuth 2.1 provider: metadata, DCR, authorization code + PKCE S256."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.adapters.sqlite import SqlitePairingStore
from app.auth.scopes import ALLOWED_SCOPES, ScopeError, classifications_for, effective_scopes, requested_scopes
from app.mcp_oauth.github import GitHubAuthorizationCode, GitHubIdentityError
from app.mcp_oauth.store import InvalidOAuthGrant, McpOAuthStore, RefreshReuse, digest
from app.mcp_oauth.tokens import InvalidMcpToken, McpAccessTokenIssuer

MAX_BODY = 16384
MAX_REDIRECTS = 5
MAX_CLIENT_NAME = 128
CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(minutes=5)
CLIENT_TTL = timedelta(days=30)
TX_TTL = timedelta(minutes=10)
REGISTRATION_WINDOW = timedelta(hours=1)
COOKIE = "mcp_oauth_tx"
COOKIE_PATH = "/mcp/oauth"
SCOPE_ORDER = ("ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc")
PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
PKCE_S256 = re.compile(r"^[A-Za-z0-9_-]{43}$")
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass
class McpOAuth:
    router: APIRouter
    authorize: Callable[[Request], dict]


def build_router(
    *,
    database_path: str | Path,
    github: GitHubAuthorizationCode,
    signing_key: str | bytes,
    workspace_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    public_url: str = "https://a2a.mathai.com.br",
    resource: str | None = None,
    registration_limit: int = 32,
) -> McpOAuth:
    if not workspace_id or not public_url:
        raise ValueError("workspace_id and public_url are required")
    issuer = public_url.rstrip("/")
    audience = resource or f"{issuer}/mcp"
    metadata_url = f"{issuer}/.well-known/oauth-protected-resource"
    callback_url = f"{issuer}/mcp/oauth/callback"
    token_issuer = McpAccessTokenIssuer(
        signing_key=signing_key, issuer=issuer, audience=audience, lifetime=ACCESS_TTL,
    )
    router = APIRouter()

    def pairing() -> SqlitePairingStore:
        return SqlitePairingStore(database_path)

    def oauth() -> McpOAuthStore:
        return McpOAuthStore(database_path)

    def authenticate_header(error: str | None = None) -> dict[str, str]:
        parts = [f'resource_metadata="{metadata_url}"']
        if error:
            parts.insert(0, f'error="{error}"')
        parts.append(f'scope="{" ".join(SCOPE_ORDER)}"')
        return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}

    def authorize(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "invalid_token", headers=authenticate_header("invalid_token"))
        presented = header[7:].strip()
        now = clock()
        try:
            token = token_issuer.verify(presented, now)
        except InvalidMcpToken:
            raise HTTPException(401, "invalid_token", headers=authenticate_header("invalid_token")) from None
        if token.workspace_id != workspace_id:
            raise HTTPException(401, "invalid_token", headers=authenticate_header("invalid_token"))
        with closing(pairing()) as principals, closing(oauth()) as store:
            family = store.family(token.family_id, now)
            if family is None or family.current_access_jti != token.jti:
                raise HTTPException(401, "invalid_token", headers=authenticate_header("invalid_token"))
            principal = principals.active_principal(token.principal_id)
            if principal is None:
                raise HTTPException(401, "invalid_token", headers=authenticate_header("invalid_token"))
            granted = principals.active_grant_scopes(principal.id, now)
            try:
                scopes = effective_scopes(token.scopes, granted, principal.role)
            except ScopeError:
                raise HTTPException(
                    403, "insufficient_scope", headers=authenticate_header("insufficient_scope"),
                ) from None
        mapping = token.as_mapping()
        mapping["scopes"] = scopes
        mapping["classifications"] = classifications_for(principal.role)
        return mapping

    def resource_metadata():
        return {
            "resource": audience,
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(SCOPE_ORDER),
        }

    def server_metadata():
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/mcp/oauth/authorize",
            "token_endpoint": f"{issuer}/mcp/oauth/token",
            "registration_endpoint": f"{issuer}/mcp/oauth/register",
            "revocation_endpoint": f"{issuer}/mcp/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPE_ORDER),
            "resource_indicators_supported": True,
        }

    @router.get("/.well-known/oauth-protected-resource")
    @router.get("/.well-known/oauth-protected-resource/mcp")
    def protected_resource_metadata():
        return resource_metadata()

    @router.get("/.well-known/oauth-authorization-server")
    def authorization_server_metadata():
        return server_metadata()

    @router.post("/mcp/oauth/register")
    async def register(request: Request):
        raw = await _read_body(request, {"application/json"})
        now = clock()
        with closing(oauth()) as store:
            store.prune(now, registration_window=REGISTRATION_WINDOW)
            if store.registration_count(now, REGISTRATION_WINDOW) >= registration_limit:
                return _error("too_many_requests", 429)
            store.record_registration(now)
            try:
                payload = _object(raw)
                redirects = _redirects(payload.get("redirect_uris"))
                name = payload.get("client_name", "MCP client")
                if not isinstance(name, str) or not name or len(name) > MAX_CLIENT_NAME:
                    raise ValueError("invalid_client_metadata")
                method = payload.get("token_endpoint_auth_method", "none")
                if method != "none":
                    raise ValueError("invalid_client_metadata")
                grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
                responses = payload.get("response_types", ["code"])
                if grants is not None and set(grants) - {"authorization_code", "refresh_token"}:
                    raise ValueError("invalid_client_metadata")
                if responses is not None and list(responses) != ["code"]:
                    raise ValueError("invalid_client_metadata")
            except RedirectError:
                return _error("invalid_redirect_uri")
            except (ValueError, TypeError):
                return _error("invalid_client_metadata")
            client_id = secrets.token_urlsafe(24)
            client = store.put_client(
                client_id=client_id, client_name=name, redirect_uris=redirects,
                now=now, expires_at=now + CLIENT_TTL,
            )
        return JSONResponse(
            {
                "client_id": client.client_id,
                "client_id_issued_at": int(now.timestamp()),
                "client_name": client.client_name,
                "redirect_uris": list(client.redirect_uris),
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
            status_code=201,
        )

    @router.get("/mcp/oauth/authorize")
    def authorize_endpoint(request: Request):
        try:
            params = _unique_query(request)
        except DuplicateError:
            return _error("invalid_request")
        now = clock()
        with closing(oauth()) as store:
            store.prune(now, registration_window=REGISTRATION_WINDOW)
            client = store.get_client(params.get("client_id", ""), now)
            redirect_uri = params.get("redirect_uri", "")
            if client is None or redirect_uri not in client.redirect_uris:
                return _error("invalid_request")
            try:
                if params.get("response_type") != "code":
                    raise ValueError("invalid_request")
                if params.get("resource") != audience:
                    raise ValueError("invalid_target")
                if params.get("code_challenge_method") != "S256":
                    raise ValueError("invalid_request")
                challenge = params.get("code_challenge", "")
                if not PKCE_S256.fullmatch(challenge):
                    raise ValueError("invalid_request")
                scopes = _scopes(params.get("scope"))
                state = params.get("state", "")
                if not isinstance(state, str) or len(state) > 512:
                    raise ValueError("invalid_request")
            except ScopeError:
                return _error("invalid_scope")
            except ValueError as exc:
                return _error(str(exc) if str(exc) in {"invalid_request", "invalid_target"} else "invalid_request")
            tx_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            store.put_transaction(
                tx_id=tx_id, csrf_hash=digest(csrf), client_id=client.client_id,
                redirect_uri=redirect_uri, resource=audience, client_state=state,
                code_challenge=challenge, scopes=scopes, now=now, expires_at=now + TX_TTL,
            )
        page = HTMLResponse(_consent_html(client.client_name, client.client_id, redirect_uri, scopes, csrf))
        page.set_cookie(
            COOKIE, tx_id, httponly=True, samesite="lax", path=COOKIE_PATH,
            secure=request.url.scheme == "https",
        )
        return page

    @router.post("/mcp/oauth/consent")
    async def consent(request: Request):
        raw = await _read_body(request, {"application/x-www-form-urlencoded"})
        try:
            form = _unique_pairs(raw)
        except DuplicateError:
            raise HTTPException(400, "invalid_request") from None
        tx_id = request.cookies.get(COOKIE, "")
        now = clock()
        with closing(oauth()) as store:
            tx = store.get_transaction(tx_id, now)
            if (
                tx is None
                or tx.status != "pending_consent"
                or not hmac.compare_digest(tx.csrf_hash, digest(form.get("csrf", "")))
            ):
                raise HTTPException(403, "invalid_csrf")
            if form.get("decision") != "allow":
                store.consume_transaction(tx.id)
                return RedirectResponse(_client_redirect(tx.redirect_uri, error="access_denied", state=tx.client_state), 302)
            store.set_transaction_status(tx.id, "pending_github")
        location = github.authorization_url(state=tx.id, redirect_uri=callback_url)
        return RedirectResponse(location, 302)

    @router.get("/mcp/oauth/callback")
    def github_callback(request: Request):
        try:
            params = _unique_query(request)
        except DuplicateError:
            return _error("invalid_request")
        tx_id = params.get("state", "")
        cookie = request.cookies.get(COOKIE, "")
        now = clock()
        with closing(oauth()) as store, closing(pairing()) as principals:
            tx = store.get_transaction(tx_id, now)
            if tx is None or tx.status != "pending_github" or not hmac.compare_digest(tx.id, cookie):
                return _error("invalid_request")
            client = store.get_client(tx.client_id, now)
            if client is None:
                return _error("invalid_client")
            try:
                subject = github.exchange_code(code=params.get("code", ""), redirect_uri=callback_url)
            except GitHubIdentityError:
                store.consume_transaction(tx.id)
                return RedirectResponse(
                    _client_redirect(tx.redirect_uri, error="access_denied", state=tx.client_state), 302,
                )
            resolved = _principal_for_subject(principals, subject, tx.scopes, now)
            if resolved is None:
                store.consume_transaction(tx.id)
                return RedirectResponse(
                    _client_redirect(tx.redirect_uri, error="access_denied", state=tx.client_state), 302,
                )
            principal, scopes = resolved
            code = secrets.token_urlsafe(32)
            store.put_code(
                code_hash=digest(code), client_id=tx.client_id, redirect_uri=tx.redirect_uri,
                resource=tx.resource, code_challenge=tx.code_challenge, principal_id=principal.id,
                scopes=scopes, expires_at=now + CODE_TTL,
            )
            store.consume_transaction(tx.id)
        response = RedirectResponse(
            _client_redirect(tx.redirect_uri, code=code, state=tx.client_state), 302,
        )
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        return response

    @router.post("/mcp/oauth/token")
    async def token_endpoint(request: Request):
        raw = await _read_body(request, {"application/x-www-form-urlencoded"})
        try:
            form = _unique_pairs(raw)
        except DuplicateError:
            return _error("invalid_request")
        now = clock()
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return _authorization_code_grant(form, now)
        if grant_type == "refresh_token":
            return _refresh_grant(form, now)
        return _error("unsupported_grant_type")

    def _authorization_code_grant(form: dict[str, str], now: datetime) -> Response:
        presented = form.get("code", "")
        verifier = form.get("code_verifier", "")
        if form.get("resource") != audience:
            return _error("invalid_target")
        with closing(oauth()) as store, closing(pairing()) as principals:
            try:
                issued = store.consume_code(digest(presented), now)
            except InvalidOAuthGrant:
                return _error("invalid_grant")
            if (
                issued.client_id != form.get("client_id")
                or issued.redirect_uri != form.get("redirect_uri")
                or issued.resource != audience
                or not _pkce_s256(verifier, issued.code_challenge)
            ):
                return _error("invalid_grant")
            return _issue_tokens(store, principals, issued.principal_id, issued.client_id, issued.scopes, now)

    def _refresh_grant(form: dict[str, str], now: datetime) -> Response:
        if form.get("resource") != audience:
            return _error("invalid_target")
        refresh = form.get("refresh_token", "")
        client_id = form.get("client_id", "")
        if not refresh or not client_id:
            return _error("invalid_grant")
        new_refresh = secrets.token_urlsafe(32)
        new_jti = secrets.token_urlsafe(16)
        with closing(oauth()) as store, closing(pairing()) as principals:
            try:
                family = store.rotate_refresh(
                    digest(refresh), now=now, new_token_hash=digest(new_refresh),
                    new_access_jti=new_jti, client_id=client_id, resource=audience,
                )
            except InvalidOAuthGrant:
                return _error("invalid_grant")
            principal = principals.active_principal(family.principal_id)
            if principal is None:
                return _error("invalid_grant")
            granted = principals.active_grant_scopes(principal.id, now)
            try:
                scopes = effective_scopes(family.requested_scopes, granted, principal.role)
            except ScopeError:
                store.revoke_family(family.id, now)
                return _error("invalid_grant")
            access, access_exp = _mint(
                principal.id, scopes, classifications_for(principal.role),
                family.id, now, family.expires_at, new_jti,
            )
        return _token_response(access, new_refresh, scopes, now, access_exp)

    def _issue_tokens(store, principals, principal_id, client_id, scopes, now):
        principal = principals.active_principal(principal_id)
        if principal is None:
            return _error("invalid_grant")
        granted = principals.active_grant_scopes(principal.id, now)
        try:
            scopes = effective_scopes(scopes, granted, principal.role)
        except ScopeError:
            return _error("invalid_grant")
        active = [grant for grant in principals.list_grants(principal.id, now) if grant.scope in scopes]
        family_exp = min(grant.expires_at for grant in active)
        refresh = secrets.token_urlsafe(32)
        jti = secrets.token_urlsafe(16)
        family = store.create_family(
            principal_id=principal.id, client_id=client_id, resource=audience,
            refresh_token_hash=digest(refresh), requested_scopes=scopes, access_jti=jti,
            now=now, expires_at=family_exp,
        )
        access, access_exp = _mint(
            principal.id, scopes, classifications_for(principal.role), family.id, now, family_exp, jti,
        )
        return _token_response(access, refresh, scopes, now, access_exp)

    def _mint(principal_id, scopes, classifications, family_id, now, family_exp, jti):
        access_exp = min(now + ACCESS_TTL, family_exp)
        token, _ = token_issuer.issue(
            principal_id=principal_id, workspace_id=workspace_id, scopes=scopes,
            classifications=classifications, family_id=family_id, now=now,
            jti=jti, expires_at=access_exp,
        )
        return token, access_exp

    @router.post("/mcp/oauth/revoke")
    async def revoke(request: Request):
        raw = await _read_body(request, {"application/x-www-form-urlencoded"})
        try:
            form = _unique_pairs(raw)
        except DuplicateError:
            return Response(status_code=200)
        now = clock()
        with closing(oauth()) as store:
            family_id = store.family_for_refresh(digest(form.get("token", "")))
            if family_id is not None:
                family = store.family(family_id, now)
                if family is not None and family.client_id == form.get("client_id", family.client_id):
                    store.revoke_family(family_id, now)
        return Response(status_code=200)

    return McpOAuth(router, authorize)


def _token_response(access, refresh, scopes, now, access_exp):
    body = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": int((access_exp - now).total_seconds()),
        "scope": " ".join(scopes),
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def _client_redirect(uri: str, **params: str) -> str:
    filtered = {key: value for key, value in params.items() if value is not None}
    separator = "&" if urlparse(uri).query else "?"
    return uri + separator + urlencode(filtered)


def _consent_html(client_name, client_id, redirect_uri, scopes, csrf) -> str:
    scopes_txt = " ".join(html.escape(item) for item in scopes)
    return (
        "<!doctype html><html><body>"
        "<h1>Autorizar MCP</h1>"
        f"<p>O cliente <strong>{html.escape(client_name)}</strong> "
        f"(<code>{html.escape(client_id)}</code>) pede acesso.</p>"
        f"<p>Redirect: <code>{html.escape(redirect_uri)}</code></p>"
        f"<p>Escopos: <code>{scopes_txt}</code></p>"
        '<form method="post" action="/mcp/oauth/consent">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        '<button type="submit" name="decision" value="allow">Permitir</button>'
        '<button type="submit" name="decision" value="deny">Negar</button>'
        "</form></body></html>"
    )


def _scopes(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw == "":
        return SCOPE_ORDER
    return requested_scopes(raw.split())


def _principal_for_subject(store: SqlitePairingStore, subject: str, requested: tuple[str, ...], now: datetime):
    try:
        principal = store.get_principal_by_github_subject(subject)
    except ValueError:
        return None
    if principal is None:
        return None
    granted = store.active_grant_scopes(principal.id, now)
    try:
        scopes = effective_scopes(requested, granted, principal.role)
    except ScopeError:
        return None
    return principal, scopes


def _pkce_s256(verifier: str, challenge: str) -> bool:
    if not PKCE_VERIFIER.fullmatch(verifier) or not PKCE_S256.fullmatch(challenge):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


class DuplicateError(ValueError):
    pass


class RedirectError(ValueError):
    pass


async def _read_body(request: Request, content_types: set[str]) -> bytes:
    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    if ctype not in content_types:
        raise HTTPException(415, "Unsupported media type")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_BODY:
            raise HTTPException(413, "Request body too large")
    return bytes(payload)


def _unique_query(request: Request) -> dict[str, str]:
    raw = request.url.query
    if len(raw) > MAX_BODY:
        raise HTTPException(400, "invalid_request")
    return _unique_pairs(raw.encode("utf-8"))


def _unique_pairs(raw: bytes) -> dict[str, str]:
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise DuplicateError("invalid") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise DuplicateError(key)
        values[key] = value
    return values


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
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _redirects(raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_REDIRECTS:
        raise RedirectError("invalid_redirect_uri")
    seen: list[str] = []
    for item in raw:
        uri = _validate_redirect(item)
        if uri in seen:
            raise RedirectError("invalid_redirect_uri")
        seen.append(uri)
    return tuple(seen)


def _validate_redirect(uri: object) -> str:
    if not isinstance(uri, str) or not uri or len(uri) > 2048 or "#" in uri:
        raise RedirectError("invalid_redirect_uri")
    parsed = urlparse(uri)
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise RedirectError("invalid_redirect_uri")
    if "@" in (parsed.netloc or "") or not parsed.scheme or not parsed.hostname:
        raise RedirectError("invalid_redirect_uri")
    if parsed.scheme == "https":
        return uri
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK:
        return uri
    raise RedirectError("invalid_redirect_uri")
