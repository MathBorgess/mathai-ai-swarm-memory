"""MCP Bearer access tokens. Audience is the MCP resource; no JWK/cnf."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.auth.tokens import ACCESS_TOKEN_ALG, load_es256_private_key

REQUIRED_CLAIMS = (
    "iss", "aud", "sub", "workspace_id", "scope", "classifications",
    "iat", "nbf", "exp", "jti", "sid",
)


class InvalidMcpToken(ValueError):
    pass


@dataclass(frozen=True)
class McpAccessPrincipal:
    principal_id: str
    workspace_id: str
    scopes: tuple[str, ...]
    classifications: tuple[str, ...]
    family_id: str
    expires_at: datetime
    jti: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "workspace_id": self.workspace_id,
            "scopes": self.scopes,
            "classifications": self.classifications,
            "family_id": self.family_id,
            "expires_at": self.expires_at,
        }


class McpAccessTokenIssuer:
    def __init__(self, *, signing_key: str | bytes, issuer: str, audience: str,
                 lifetime: timedelta = timedelta(minutes=5)):
        if not issuer or not audience:
            raise ValueError("Issuer and audience are required")
        seconds = lifetime.total_seconds()
        if not 0 < seconds <= 3600:
            raise ValueError("Access token lifetime must be at most one hour")
        self._key = load_es256_private_key(signing_key)
        self.issuer = issuer
        self.audience = audience
        self.lifetime = lifetime

    def issue(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        scopes: tuple[str, ...],
        classifications: tuple[str, ...],
        family_id: str,
        now: datetime,
        jti: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[str, str]:
        if now.utcoffset() is None:
            raise ValueError("A timezone-aware datetime is required")
        exp = expires_at or (now + self.lifetime)
        if exp <= now or exp > now + timedelta(hours=1):
            raise ValueError("Access token lifetime is invalid")
        token_jti = jti or secrets.token_urlsafe(16)
        payload = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": principal_id,
            "workspace_id": workspace_id,
            "scope": " ".join(scopes),
            "classifications": list(classifications),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": token_jti,
            "sid": family_id,
        }
        return jwt.encode(payload, self._key, algorithm=ACCESS_TOKEN_ALG), token_jti

    def verify(self, token: str, now: datetime) -> McpAccessPrincipal:
        if now.utcoffset() is None:
            raise ValueError("A timezone-aware datetime is required")
        now = now.astimezone(timezone.utc)
        try:
            payload = jwt.decode(
                token,
                key=self._key.public_key(),
                algorithms=[ACCESS_TOKEN_ALG],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["iss", "aud", "sub", "exp", "nbf", "iat", "jti"],
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "strict_aud": True,
                },
            )
        except jwt.InvalidTokenError:
            raise InvalidMcpToken("Access token is invalid") from None
        if any(name not in payload for name in REQUIRED_CLAIMS) or "cnf" in payload:
            raise InvalidMcpToken("Access token is invalid")
        try:
            exp = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
            nbf = datetime.fromtimestamp(int(payload["nbf"]), tz=timezone.utc)
            iat = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            raise InvalidMcpToken("Access token is invalid") from None
        if not (iat <= nbf <= now < exp):
            raise InvalidMcpToken("Access token is not active")
        scope = payload["scope"]
        classifications = payload["classifications"]
        workspace_id = payload["workspace_id"]
        family_id = payload["sid"]
        principal_id = payload["sub"]
        if not isinstance(scope, str) or not scope.strip():
            raise InvalidMcpToken("Access token is invalid")
        if not isinstance(classifications, list) or not all(isinstance(item, str) for item in classifications):
            raise InvalidMcpToken("Access token is invalid")
        if not all(isinstance(value, str) and value for value in (workspace_id, family_id, principal_id)):
            raise InvalidMcpToken("Access token is invalid")
        return McpAccessPrincipal(
            principal_id=principal_id,
            workspace_id=workspace_id,
            scopes=tuple(scope.split()),
            classifications=tuple(classifications),
            family_id=family_id,
            expires_at=exp,
            jti=str(payload["jti"]),
        )
