"""RFC 9449 DPoP proof verification. Canonical htu comes from configuration."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ALLOWED_DPOP_ALGS = ("ES256", "EdDSA")
DPOP_TYP = "dpop+jwt"
DPOP_IAT_WINDOW = timedelta(seconds=60)
PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "priv", "k"})


class InvalidDpopProof(ValueError):
    error = "invalid_dpop_proof"


@dataclass(frozen=True)
class DpopProof:
    jkt: str
    jti: str
    jwk: dict[str, Any]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def access_token_hash(token: str) -> str:
    return _b64url(hashlib.sha256(token.encode("ascii")).digest())


def public_jwk_thumbprint(jwk: dict[str, Any]) -> str:
    if not isinstance(jwk, dict):
        raise InvalidDpopProof("DPoP JWK must be a public JSON object")
    if PRIVATE_JWK_MEMBERS.intersection(jwk):
        raise InvalidDpopProof("DPoP JWK must not include private key material")
    kty = jwk.get("kty")
    if kty == "EC":
        members = {"crv": jwk.get("crv"), "kty": "EC", "x": jwk.get("x"), "y": jwk.get("y")}
        if members["crv"] != "P-256" or not isinstance(members["x"], str) or not isinstance(members["y"], str):
            raise InvalidDpopProof("DPoP JWK is not a supported public key")
    elif kty == "OKP":
        members = {"crv": jwk.get("crv"), "kty": "OKP", "x": jwk.get("x")}
        if members["crv"] != "Ed25519" or not isinstance(members["x"], str):
            raise InvalidDpopProof("DPoP JWK is not a supported public key")
    else:
        raise InvalidDpopProof("DPoP JWK is not a supported public key")
    packed = json.dumps(members, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(hashlib.sha256(packed).digest())


def _load_public_jwk(jwk: dict[str, Any]):
    try:
        key = jwt.algorithms.get_default_algorithms()["ES256"].from_jwk(json.dumps(jwk)) if jwk.get("kty") == "EC" \
            else jwt.algorithms.get_default_algorithms()["EdDSA"].from_jwk(json.dumps(jwk))
    except (ValueError, TypeError, jwt.InvalidKeyError):
        raise InvalidDpopProof("DPoP JWK is not a supported public key") from None
    if not isinstance(key, (EllipticCurvePublicKey, Ed25519PublicKey)):
        raise InvalidDpopProof("DPoP JWK is not a supported public key")
    return key


def canonical_htu(public_url: str, path: str) -> str:
    return public_url.rstrip("/") + path


def verify_dpop_proof(
    proof: str,
    *,
    htm: str,
    htu: str,
    now: datetime,
    ath: str | None = None,
) -> DpopProof:
    if not isinstance(proof, str) or not proof or len(proof) > 8192:
        raise InvalidDpopProof("DPoP proof is missing or too large")
    if now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    now = now.astimezone(timezone.utc)
    try:
        header = jwt.get_unverified_header(proof)
    except jwt.InvalidTokenError:
        raise InvalidDpopProof("DPoP proof is malformed") from None
    if header.get("typ") != DPOP_TYP:
        raise InvalidDpopProof("DPoP proof type is invalid")
    alg = header.get("alg")
    if alg not in ALLOWED_DPOP_ALGS:
        raise InvalidDpopProof("DPoP proof algorithm is not allowed")
    jwk = header.get("jwk")
    if not isinstance(jwk, dict) or PRIVATE_JWK_MEMBERS.intersection(jwk):
        raise InvalidDpopProof("DPoP JWK must be a public JSON object")
    key = _load_public_jwk(jwk)
    try:
        claims = jwt.decode(
            proof,
            key=key,
            algorithms=[alg],
            options={"verify_aud": False, "verify_iss": False, "verify_exp": False,
                     "verify_nbf": False, "verify_iat": False, "require": ["jti", "htm", "htu", "iat"]},
        )
    except jwt.InvalidTokenError:
        raise InvalidDpopProof("DPoP proof signature is invalid") from None
    if claims.get("htm") != htm or claims.get("htu") != htu:
        raise InvalidDpopProof("DPoP proof htm/htu does not match the request")
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        raise InvalidDpopProof("DPoP proof iat is invalid")
    issued = datetime.fromtimestamp(int(iat), tz=timezone.utc)
    if abs((now - issued).total_seconds()) >= DPOP_IAT_WINDOW.total_seconds():
        raise InvalidDpopProof("DPoP proof iat is invalid")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 128:
        raise InvalidDpopProof("DPoP proof jti is invalid")
    if ath is None:
        if "ath" in claims:
            raise InvalidDpopProof("DPoP proof ath is invalid")
    elif claims.get("ath") != ath:
        raise InvalidDpopProof("DPoP proof ath is invalid")
    return DpopProof(jkt=public_jwk_thumbprint(jwk), jti=jti, jwk=jwk)
