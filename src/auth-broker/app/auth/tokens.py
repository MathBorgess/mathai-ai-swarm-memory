"""Broker-issued DPoP-bound access tokens. Signing material has no default."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, SECP256R1
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ACCESS_TOKEN_ALG = "ES256"
REQUIRED_CLAIMS = (
    "iss", "aud", "sub", "workspace_id", "scope", "classifications",
    "iat", "nbf", "exp", "jti", "sid", "cnf",
)


class InvalidAccessToken(ValueError):
    """Access token failed signature or claim checks."""


@dataclass(frozen=True)
class AccessPrincipal:
    principal_id: str
    workspace_id: str
    scopes: tuple[str, ...]
    classifications: tuple[str, ...]
    family_id: str
    expires_at: datetime
    jkt: str
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


def load_es256_private_key(material: str | bytes) -> EllipticCurvePrivateKey:
    raw = material.encode() if isinstance(material, str) else material
    try:
        key = load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        raise ValueError("AUTH_BROKER_JWT_SIGNING_KEY must be an unencrypted ES256 PEM") from None
    if not isinstance(key, EllipticCurvePrivateKey) or not isinstance(key.curve, SECP256R1):
        raise ValueError("AUTH_BROKER_JWT_SIGNING_KEY must be an unencrypted ES256 PEM")
    return key


class AccessTokenIssuer:
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
        jkt: str,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> str:
        if now.utcoffset() is None:
            raise ValueError("A timezone-aware datetime is required")
        exp = expires_at or (now + self.lifetime)
        if exp <= now:
            raise ValueError("Access token expiry must follow issuance")
        if exp > now + timedelta(hours=1):
            raise ValueError("Access token lifetime must be at most one hour")
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
            "jti": secrets.token_urlsafe(16),
            "sid": family_id,
            "cnf": {"jkt": jkt},
        }
        return jwt.encode(payload, self._key, algorithm=ACCESS_TOKEN_ALG)

    def verify(self, token: str, now: datetime) -> AccessPrincipal:
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
            raise InvalidAccessToken("Access token is invalid") from None
        missing = [name for name in REQUIRED_CLAIMS if name not in payload]
        if missing:
            raise InvalidAccessToken("Access token is missing required claims")
        try:
            exp = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
            nbf = datetime.fromtimestamp(int(payload["nbf"]), tz=timezone.utc)
            iat = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            raise InvalidAccessToken("Access token time claims are invalid") from None
        if not (iat <= nbf <= now < exp):
            raise InvalidAccessToken("Access token is not active")
        cnf = payload["cnf"]
        if not isinstance(cnf, dict) or not isinstance(cnf.get("jkt"), str) or not cnf["jkt"]:
            raise InvalidAccessToken("Access token confirmation is invalid")
        scope = payload["scope"]
        if not isinstance(scope, str) or not scope.strip():
            raise InvalidAccessToken("Access token scope is invalid")
        classifications = payload["classifications"]
        if not isinstance(classifications, list) or not all(isinstance(item, str) for item in classifications):
            raise InvalidAccessToken("Access token classifications are invalid")
        workspace_id = payload["workspace_id"]
        family_id = payload["sid"]
        principal_id = payload["sub"]
        if not all(isinstance(value, str) and value for value in (workspace_id, family_id, principal_id)):
            raise InvalidAccessToken("Access token identity claims are invalid")
        if payload["iss"] != self.issuer or payload["aud"] != self.audience:
            raise InvalidAccessToken("Access token issuer or audience is invalid")
        return AccessPrincipal(
            principal_id=principal_id,
            workspace_id=workspace_id,
            scopes=tuple(scope.split()),
            classifications=tuple(classifications),
            family_id=family_id,
            expires_at=exp,
            jkt=cnf["jkt"],
            jti=str(payload["jti"]),
        )
