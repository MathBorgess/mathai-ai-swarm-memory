"""HTTPS client for the swarm v1 JSON contract. Redirects off; DPoP on every call."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from mathai_swarm_mcp import dpop
from mathai_swarm_mcp.errors import (
    ConflictError,
    DependencyUnavailable,
    ForbiddenError,
    InputError,
    LoginRequiredError,
    OAuthProtocolError,
    PayloadTooLarge,
    RedirectRejected,
    SwarmMcpError,
    redact,
)
from mathai_swarm_mcp.keystore import CredentialStore, StoredPrincipal, ensure_key
from mathai_swarm_mcp.origin import assert_same_origin, parse_origin, request_url, validate_github_verification_uri

SCOPE_RE = re.compile(r"^ctx:(read|propose):[a-z0-9]+(?:\.[a-z0-9]+)*$")
DEFAULT_SCOPES = ("ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc")
MAX_BODY = 16384
MAX_QUERY = 2000
MAX_MARKDOWN = 12288
MAX_HANDLES = 50
MAX_LIMIT = 50
PROACTIVE_SKEW = timedelta(seconds=30)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_ERRORS = {
    "authorization_pending",
    "slow_down",
    "expired_token",
    "access_denied",
    "invalid_grant",
    "invalid_dpop_proof",
}
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass
class AccessToken:
    value: str
    token_type: str
    scope: str
    expires_at: datetime


class SwarmHttpClient:
    def __init__(
        self,
        origin: str,
        principal_id: str,
        *,
        store: CredentialStore,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not principal_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", principal_id):
            raise InputError("principal_id is invalid")
        self.origin = parse_origin(origin)
        self.principal_id = principal_id
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._access: AccessToken | None = None
        self._refresh_uncertain = False
        self._operations: list | None = None
        self._http = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def public_material(self) -> dict:
        record = ensure_key(self.store, self.origin, self.principal_id)
        from mathai_swarm_mcp.keystore import public_material

        return public_material(record)

    def start_device(self, scopes: list[str] | tuple[str, ...] | None = None) -> dict:
        scopes = _scopes(scopes)
        record = ensure_key(self.store, self.origin, self.principal_id)
        payload = {"principal_id": self.principal_id, "scopes": list(scopes)}
        data = self._oauth(
            "POST",
            "/v1/oauth/github/device/start",
            payload,
            record,
            expect_tokens=False,
        )
        required = ("device_code", "user_code", "verification_uri", "expires_in", "interval")
        if not all(k in data for k in required):
            raise SwarmMcpError("device start response is incomplete")
        validate_github_verification_uri(str(data["verification_uri"]))
        if data.get("verification_uri_complete"):
            validate_github_verification_uri(str(data["verification_uri_complete"]))
        return data

    def poll_device(self, device_code: str) -> dict | None:
        if not device_code or not isinstance(device_code, str):
            raise InputError("device_code is required")
        record = self._record()
        payload = {"device_code": device_code, "grant_type": DEVICE_GRANT}
        try:
            data = self._oauth("POST", "/v1/oauth/github/device/poll", payload, record, expect_tokens=True)
        except OAuthProtocolError as exc:
            if exc.error in {"authorization_pending", "slow_down"}:
                return {"error": exc.error}
            raise
        self._accept_tokens(record, data)
        return {"status": "authorized"}

    def refresh(self) -> AccessToken:
        with self._lock:
            return self._refresh_locked()

    def revoke(self) -> None:
        record = self._record()
        try:
            access = self._ensure_access()
            self._send("POST", "/v1/oauth/revoke", json_body={}, record=record, access=access)
        except (LoginRequiredError, OAuthProtocolError, SwarmMcpError):
            pass
        self._access = None
        self._refresh_uncertain = False
        self._operations = None
        record.refresh_token = None
        record.scope = None
        self.store.save(record)

    def logout(self) -> None:
        self.revoke()
        self.store.delete(self.origin, self.principal_id)

    def capabilities(self) -> dict:
        data = self._authorized("GET", "/v1/context/capabilities")
        operations = data.get("operations")
        self._operations = operations if isinstance(operations, list) else []
        return data

    def query(self, query: str, limit: int = 10) -> dict:
        if not isinstance(query, str) or not query or len(query) > MAX_QUERY:
            raise InputError("query must be 1..2000 characters")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise InputError("limit must be 1..50")
        self._require_operation("query")
        return self._authorized("POST", "/v1/context/query", {"query": query, "limit": limit})

    def resolve(self, handles: list[str]) -> dict:
        if not isinstance(handles, list) or not handles or len(handles) > MAX_HANDLES:
            raise InputError("handles must be a list of 1..50 opaque ids")
        if any(not isinstance(item, str) or not item for item in handles):
            raise InputError("handles must be a list of 1..50 opaque ids")
        self._require_operation("resolve")
        return self._authorized("POST", "/v1/context/resolve", {"handles": list(handles)})

    def propose(self, payload: dict, idempotency_key: str) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256:
            raise InputError("idempotency_key is required")
        body = _propose_body(payload)
        self._require_operation("propose")
        return self._authorized(
            "POST",
            "/v1/context/propose",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def _require_operation(self, name: str) -> None:
        if self._operations is None:
            self.capabilities()
        if not self._operations or name not in self._operations:
            raise DependencyUnavailable(f"operation {name!r} is not installed on this origin")

    def _ensure_access(self) -> str:
        with self._lock:
            if self._refresh_uncertain:
                raise LoginRequiredError(_ROTATION_MSG)
            token = self._access
            if token is not None and token.expires_at - self._clock() > PROACTIVE_SKEW:
                return token.value
            return self._refresh_locked().value

    def _refresh_after_401(self, previous: str) -> str:
        with self._lock:
            if self._refresh_uncertain:
                raise LoginRequiredError(_ROTATION_MSG)
            token = self._access
            if token is not None and token.value != previous and token.expires_at > self._clock():
                return token.value
            return self._refresh_locked().value

    def _refresh_locked(self) -> AccessToken:
        if self._refresh_uncertain:
            raise LoginRequiredError(_ROTATION_MSG)
        record = self._record()
        if not record.refresh_token:
            raise LoginRequiredError("not authenticated; run mathai-swarm-mcp login in a terminal")
        payload = {"grant_type": "refresh_token", "refresh_token": record.refresh_token}
        try:
            data = self._oauth("POST", "/v1/oauth/token", payload, record, expect_tokens=True, rotation_sensitive=True)
        except OAuthProtocolError as exc:
            if exc.error == "invalid_grant":
                record.refresh_token = None
                self.store.save(record)
                self._access = None
                raise LoginRequiredError("refresh was rejected; run mathai-swarm-mcp login") from None
            raise
        return self._accept_tokens(record, data)

    def _authorized(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        record = self._record()
        access = self._ensure_access()
        response = self._send(method, path, json_body=json_body, record=record, access=access, extra_headers=extra_headers)
        if response.status_code == 403:
            raise ForbiddenError(_status_message(response, "forbidden"))
        if response.status_code == 401:
            access = self._refresh_after_401(access)
            response = self._send(
                method, path, json_body=json_body, record=record, access=access, extra_headers=extra_headers
            )
            if response.status_code == 401:
                raise LoginRequiredError("credential rejected after refresh; run mathai-swarm-mcp login")
            if response.status_code == 403:
                raise ForbiddenError(_status_message(response, "forbidden"))
        return _parse_resource(response)

    def _oauth(
        self,
        method: str,
        path: str,
        json_body: dict,
        record: StoredPrincipal,
        *,
        expect_tokens: bool,
        rotation_sensitive: bool = False,
    ) -> dict:
        try:
            response = self._send(method, path, json_body=json_body, record=record, access=None)
        except SwarmMcpError:
            if rotation_sensitive:
                self._mark_uncertain(record)
                raise LoginRequiredError(_ROTATION_MSG) from None
            raise
        data = _json_object(response)
        error = data.get("error")
        if isinstance(error, str) and error in OAUTH_ERRORS:
            raise OAuthProtocolError(error)
        if response.status_code == 401:
            raise OAuthProtocolError("invalid_dpop_proof")
        if expect_tokens:
            _require_token_envelope(data)
        if response.status_code >= 400:
            raise SwarmMcpError("oauth request failed")
        return data

    def _mark_uncertain(self, record: StoredPrincipal) -> None:
        self._refresh_uncertain = True
        self._access = None
        record.refresh_token = None
        self.store.save(record)

    def _accept_tokens(self, record: StoredPrincipal, data: dict) -> AccessToken:
        refresh = data["refresh_token"]
        access = AccessToken(
            value=data["access_token"],
            token_type=data["token_type"],
            scope=str(data.get("scope") or ""),
            expires_at=self._clock() + timedelta(seconds=int(data["expires_in"])),
        )
        record.refresh_token = refresh
        record.scope = access.scope
        self.store.save(record)
        self._access = access
        self._refresh_uncertain = False
        return access

    def _record(self) -> StoredPrincipal:
        record = self.store.load(self.origin, self.principal_id)
        if record is None:
            raise LoginRequiredError("no local key; run mathai-swarm-mcp show-key then login")
        return record

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None,
        record: StoredPrincipal,
        access: str | None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = request_url(self.origin, path)
        assert_same_origin(self.origin, url)
        headers = {
            "Accept": "application/json",
            "DPoP": dpop.proof(record.private_jwk, method, url, access_token=access, now=self._clock()),
        }
        content = None
        if json_body is not None:
            content = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(content) > MAX_BODY:
                raise PayloadTooLarge("request body exceeds 16 KiB")
            headers["Content-Type"] = "application/json"
        if access is not None:
            headers["Authorization"] = f"DPoP {access}"
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._http.request(method, url, content=content, headers=headers)
        except httpx.TimeoutException:
            raise SwarmMcpError("request timed out") from None
        except httpx.HTTPError:
            raise SwarmMcpError("HTTP transport error") from None
        if response.status_code in _REDIRECTS:
            raise RedirectRejected("refusing HTTP redirect from the configured origin")
        return response


def _scopes(scopes: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    requested = tuple(scopes) if scopes else DEFAULT_SCOPES
    if not requested:
        raise InputError("scopes must not be empty")
    out = []
    for scope in requested:
        if not isinstance(scope, str) or not SCOPE_RE.fullmatch(scope):
            raise InputError("unsupported or unknown scope")
        out.append(scope)
    return tuple(out)


def _propose_body(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"namespace", "title", "body_markdown", "sources"}:
        raise InputError("propose payload must be namespace, title, body_markdown, sources")
    namespace, title, body = payload["namespace"], payload["title"], payload["body_markdown"]
    sources = payload["sources"]
    if not isinstance(namespace, str) or not namespace or not isinstance(title, str) or not title:
        raise InputError("propose title and namespace are required")
    if not isinstance(body, str) or not body or len(body.encode("utf-8")) > MAX_MARKDOWN:
        raise InputError("body_markdown must be 1..12 KiB")
    if not isinstance(sources, list) or any(
        not isinstance(item, dict) or set(item) != {"url", "label"} or not isinstance(item["url"], str) or not isinstance(item["label"], str)
        for item in sources
    ):
        raise InputError("sources must be {url,label} objects")
    return {"namespace": namespace, "title": title, "body_markdown": body, "sources": sources}


def _json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        raise SwarmMcpError("response is not JSON") from None
    if not isinstance(data, dict):
        raise SwarmMcpError("response is not a JSON object")
    return data


def _require_token_envelope(data: dict) -> None:
    token_type = data.get("token_type")
    if token_type != "DPoP":
        raise SwarmMcpError("token_type must be DPoP")
    if not isinstance(data.get("access_token"), str) or not isinstance(data.get("refresh_token"), str):
        raise SwarmMcpError("token envelope is incomplete")
    try:
        expires_in = int(data["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise SwarmMcpError("token envelope is incomplete") from None
    if expires_in <= 0:
        raise SwarmMcpError("token envelope is incomplete")


def _parse_resource(response: httpx.Response) -> dict:
    if response.status_code == 400:
        raise InputError(_status_message(response, "invalid input"))
    if response.status_code == 409:
        raise ConflictError(_status_message(response, "idempotency conflict"))
    if response.status_code == 413:
        raise PayloadTooLarge(_status_message(response, "payload too large"))
    if response.status_code == 503:
        raise DependencyUnavailable(_status_message(response, "dependency unavailable"))
    if response.status_code >= 400:
        raise SwarmMcpError(_status_message(response, "request failed"))
    return _json_object(response)


def _status_message(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
    except ValueError:
        return fallback
    if not isinstance(data, dict):
        return fallback
    for key in ("error", "detail", "message", "status"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return redact(value)
    return fallback


_ROTATION_MSG = (
    "refresh timed out after a possible rotation; the previous refresh token must not be reused. "
    "Run mathai-swarm-mcp login again."
)
