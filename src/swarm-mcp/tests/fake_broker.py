"""HTTP fake of the swarm v1 contract. Proves the adapter, not integration with A/C/D."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
import jwt

from mathai_swarm_mcp.dpop import access_token_hash, jwk_thumbprint, public_jwk
from mathai_swarm_mcp.origin import canonical_htu

ORIGIN = "https://a2a.mathai.com.br"
ENVELOPE = {
    "items": [
        {
            "handle": "opaque-test-1",
            "text": "already authorized excerpt",
            "source_revision": "abc1234",
        }
    ],
    "capability_receipt": {
        "principal_id": "advisor-01",
        "workspace_id": "personal",
        "scopes_used": ["ctx:read:pesquisa.tcc"],
        "pass_as": "handle",
        "policy_version": "v1",
    },
}


class FakeBroker:
    def __init__(self, *, operations=None) -> None:
        self.operations = list(operations or ["capabilities", "query", "resolve", "propose"])
        self.seen_jti: set[str] = set()
        self.refresh_calls = 0
        self.requests: list[httpx.Request] = []
        self.poll_counts: dict[str, int] = {}
        self.devices: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.revoked: set[str] = set()
        self.redirect_paths: set[str] = set()
        self.force_timeout_refresh = False
        self.secret_in_query_error = False
        self.propose_keys: dict[str, str] = {}
        self.retired: dict[str, str] = {}
        self.bound_jkt: dict[str, str] = {}
        self.clock = lambda: datetime.now(timezone.utc)
        self.forbidden = False
        self.verification_uri = "https://github.com/login/device"

    def expire_access(self) -> None:
        past = self.clock() - timedelta(seconds=1)
        for session in self.access.values():
            session["exp"] = past

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlsplit(str(request.url)).path
        if path in self.redirect_paths:
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"}, request=request)
        try:
            proof = self._dpop(request)
        except ValueError as exc:
            error = str(exc)
            code = 401 if error == "invalid_dpop_proof" else 400
            return _json(request, code, {"error": error})
        jkt = proof["jkt"]
        if request.method == "POST" and path == "/v1/oauth/github/device/start":
            return self._start(request, jkt)
        if request.method == "POST" and path == "/v1/oauth/github/device/poll":
            return self._poll(request, jkt)
        if request.method == "POST" and path == "/v1/oauth/token":
            return self._token(request, jkt)
        if request.method == "POST" and path == "/v1/oauth/revoke":
            return self._revoke(request, proof)
        if request.method == "GET" and path == "/v1/context/capabilities":
            return self._resource(request, proof, self._capabilities)
        if request.method == "POST" and path == "/v1/context/query":
            return self._resource(request, proof, self._query)
        if request.method == "POST" and path == "/v1/context/resolve":
            return self._resource(request, proof, self._resolve)
        if request.method == "POST" and path == "/v1/context/propose":
            return self._resource(request, proof, self._propose)
        return _json(request, 404, {"error": "not_found"})

    def _start(self, request: httpx.Request, jkt: str) -> httpx.Response:
        body = _body(request)
        principal = body.get("principal_id")
        scopes = body.get("scopes")
        if not isinstance(principal, str) or not isinstance(scopes, list):
            return _json(request, 400, {"error": "invalid_request"})
        device_code = "device_" + secrets.token_hex(8)
        self.devices[device_code] = {"principal": principal, "jkt": jkt, "scopes": scopes}
        self.bound_jkt[principal] = jkt
        return _json(
            request,
            200,
            {
                "device_code": device_code,
                "user_code": "WD4X-T7NK",
                "verification_uri": self.verification_uri,
                "expires_in": 600,
                "interval": 0,
            },
        )

    def _poll(self, request: httpx.Request, jkt: str) -> httpx.Response:
        body = _body(request)
        if body.get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code":
            return _json(request, 400, {"error": "invalid_request"})
        device_code = body.get("device_code")
        device = self.devices.get(device_code)
        if not device or device["jkt"] != jkt:
            return _json(request, 400, {"error": "invalid_dpop_proof"})
        count = self.poll_counts.get(device_code, 0) + 1
        self.poll_counts[device_code] = count
        if count == 1:
            return _json(request, 400, {"error": "authorization_pending"})
        if count == 2:
            return _json(request, 400, {"error": "slow_down"})
        return _json(request, 200, self._issue(device["principal"], jkt, device["scopes"]))

    def _token(self, request: httpx.Request, jkt: str) -> httpx.Response:
        if self.force_timeout_refresh:
            raise httpx.TimeoutException("refresh timeout")
        body = _body(request)
        if body.get("grant_type") != "refresh_token":
            return _json(request, 400, {"error": "invalid_grant"})
        presented = body.get("refresh_token")
        record = self.refresh.get(presented)
        self.refresh_calls += 1
        if record is None:
            family = self.retired.get(presented)
            if family:
                self.revoked.add(family)
            return _json(request, 400, {"error": "invalid_grant"})
        if record["jkt"] != jkt or record["family"] in self.revoked:
            return _json(request, 400, {"error": "invalid_grant"})
        self.refresh.pop(presented, None)
        self.retired[presented] = record["family"]
        tokens = self._issue(record["principal"], jkt, record["scopes"], family=record["family"])
        return _json(request, 200, tokens)

    def _revoke(self, request: httpx.Request, proof: dict) -> httpx.Response:
        session = self._session(request, proof)
        if session is None:
            return _json(request, 401, {"error": "invalid_grant"})
        self.revoked.add(session["family"])
        return _json(request, 200, {"status": "revoked"})

    def _capabilities(self, request: httpx.Request, session: dict) -> httpx.Response:
        return _json(
            request,
            200,
            {
                "principal_id": session["principal"],
                "workspace_id": "personal",
                "scopes": session["scopes"],
                "operations": list(self.operations),
            },
        )

    def _query(self, request: httpx.Request, session: dict) -> httpx.Response:
        if self.forbidden:
            return _json(request, 403, {"error": "forbidden"})
        if self.secret_in_query_error:
            return _json(
                request,
                500,
                {"error": "boom", "access_token": "leak-access-token", "refresh_token": "leak-refresh-token"},
            )
        body = _body(request)
        if not isinstance(body.get("query"), str):
            return _json(request, 400, {"error": "invalid_request"})
        envelope = json.loads(json.dumps(ENVELOPE))
        envelope["capability_receipt"]["principal_id"] = session["principal"]
        envelope["capability_receipt"]["scopes_used"] = list(session["scopes"])
        return _json(request, 200, envelope)

    def _resolve(self, request: httpx.Request, session: dict) -> httpx.Response:
        body = _body(request)
        handles = body.get("handles")
        if not isinstance(handles, list):
            return _json(request, 400, {"error": "invalid_request"})
        items = [item for item in ENVELOPE["items"] if item["handle"] in handles]
        return _json(
            request,
            200,
            {
                "items": items,
                "capability_receipt": {
                    "principal_id": session["principal"],
                    "workspace_id": "personal",
                    "scopes_used": list(session["scopes"]),
                    "pass_as": "handle",
                    "policy_version": "v1",
                },
            },
        )

    def _propose(self, request: httpx.Request, session: dict) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        body = request.content
        if not key:
            return _json(request, 400, {"error": "invalid_request"})
        digest = hashlib.sha256(body).hexdigest()
        previous = self.propose_keys.get(key)
        if previous and previous != digest:
            return _json(request, 409, {"error": "idempotency conflict"})
        self.propose_keys[key] = digest
        return _json(
            request,
            200,
            {
                "status": "created",
                "proposal_id": "prop-test-1",
                "branch": "swarm/proposal-prop-test-1",
                "pr_url": "https://github.com/example/discarded/pull/0",
            },
        )

    def _resource(self, request: httpx.Request, proof: dict, handler):
        try:
            session = self._session(request, proof)
        except ValueError as exc:
            return _json(request, 401, {"error": str(exc)})
        if session is None:
            return _json(request, 401, {"error": "invalid_grant"})
        if session["family"] in self.revoked:
            return _json(request, 401, {"error": "invalid_grant"})
        return handler(request, session)

    def _session(self, request: httpx.Request, proof: dict) -> dict | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("DPoP "):
            return None
        token = header[5:]
        session = self.access.get(token)
        if session is None or session["jkt"] != proof["jkt"]:
            return None
        if proof["ath"] != access_token_hash(token):
            raise ValueError("invalid_dpop_proof")
        if session["exp"] <= self.clock():
            return None
        return session

    def _issue(self, principal: str, jkt: str, scopes: list, family: str | None = None) -> dict:
        family = family or "fam_" + secrets.token_hex(8)
        access = "access_" + secrets.token_hex(16)
        refresh = "refresh_" + secrets.token_hex(16)
        session = {
            "principal": principal,
            "jkt": jkt,
            "scopes": list(scopes),
            "family": family,
            "exp": self.clock() + timedelta(minutes=5),
        }
        self.access[access] = session
        self.refresh[refresh] = {"principal": principal, "jkt": jkt, "scopes": list(scopes), "family": family}
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "DPoP",
            "scope": " ".join(scopes) if not isinstance(scopes, str) else scopes,
            "expires_in": 300,
        }

    def _dpop(self, request: httpx.Request) -> dict:
        token = request.headers.get("DPoP")
        if not token:
            raise ValueError("invalid_dpop_proof")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise ValueError("invalid_dpop_proof") from exc
        if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
            raise ValueError("invalid_dpop_proof")
        jwk = header.get("jwk")
        if not isinstance(jwk, dict) or "d" in jwk:
            raise ValueError("invalid_dpop_proof")
        try:
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(jwk).key,
                algorithms=["ES256"],
                options={"require": ["jti", "htm", "htu", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise ValueError("invalid_dpop_proof") from exc
        jti = claims["jti"]
        if jti in self.seen_jti:
            raise ValueError("invalid_dpop_proof")
        self.seen_jti.add(jti)
        if claims["htm"] != request.method.upper():
            raise ValueError("invalid_dpop_proof")
        if claims["htu"] != canonical_htu(str(request.url)):
            raise ValueError("invalid_dpop_proof")
        now = int(self.clock().timestamp())
        if abs(now - int(claims["iat"])) > 300:
            raise ValueError("invalid_dpop_proof")
        return {"jkt": jwk_thumbprint(public_jwk(jwk)), "ath": claims.get("ath"), "jwk": jwk}


def _body(request: httpx.Request) -> dict:
    if not request.content:
        return {}
    data = json.loads(request.content.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_request")
    return data


def _json(request: httpx.Request, status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=request)
