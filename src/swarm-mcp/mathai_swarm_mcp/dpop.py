"""RFC 9449 DPoP proofs (ES256) and RFC 7638 JWK thumbprints."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateNumbers,
    EllipticCurvePublicNumbers,
    SECP256R1,
)

from mathai_swarm_mcp.errors import InputError
from mathai_swarm_mcp.origin import canonical_htu

_P256_SIZE = 32


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_int(value: int) -> str:
    return _b64u(value.to_bytes(_P256_SIZE, "big"))


def _int(value: str) -> int:
    pad = "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(value + pad), "big")


def public_jwk(jwk: dict) -> dict:
    try:
        return {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}
    except KeyError as exc:
        raise InputError("incomplete JWK") from exc


def generate_private_jwk() -> dict:
    key = ec.generate_private_key(SECP256R1())
    priv = key.private_numbers()
    pub = priv.public_numbers
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u_int(pub.x),
        "y": _b64u_int(pub.y),
        "d": _b64u_int(priv.private_value),
    }


def jwk_thumbprint(jwk: dict) -> str:
    material = json.dumps(public_jwk(jwk), separators=(",", ":"), sort_keys=True).encode("ascii")
    return _b64u(hashlib.sha256(material).digest())


def private_key(jwk: dict):
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or "d" not in jwk:
        raise InputError("DPoP requires an ES256 private JWK")
    pub = EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), SECP256R1())
    return EllipticCurvePrivateNumbers(_int(jwk["d"]), pub).private_key()


def access_token_hash(access_token: str) -> str:
    return _b64u(hashlib.sha256(access_token.encode("ascii")).digest())


def proof(private_jwk: dict, method: str, url: str, *, access_token: str | None = None, now: datetime | None = None) -> str:
    issued = now or datetime.now(timezone.utc)
    claims = {
        "jti": secrets.token_urlsafe(16),
        "htm": method.upper(),
        "htu": canonical_htu(url),
        "iat": int(issued.timestamp()),
    }
    if access_token is not None:
        claims["ath"] = access_token_hash(access_token)
    headers = {"typ": "dpop+jwt", "alg": "ES256", "jwk": public_jwk(private_jwk)}
    return jwt.encode(claims, private_key(private_jwk), algorithm="ES256", headers=headers)
