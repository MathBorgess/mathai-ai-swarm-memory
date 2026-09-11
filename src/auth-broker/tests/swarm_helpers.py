"""Shared builders for swarm DPoP tests. Fixtures stay synthetic."""

from __future__ import annotations

import json
import secrets
from datetime import datetime

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app.auth.dpop import access_token_hash, canonical_htu, public_jwk_thumbprint

PUBLIC_URL = "https://a2a.example.test"
AUDIENCE = "https://pair.example.test"
WORKSPACE = "personal"
NOW = datetime.fromisoformat("2026-09-11T00:00:00+00:00")


class FakeHermes:
    def __init__(self):
        self.queries = []

    def query_as_broker(self, *, agent_id, query):
        self.queries.append((agent_id, query))
        return {"answer": "hermes-only"}


class GitHubOAuth:
    def __init__(self, subject="12345"):
        self.subject = subject
        self.pending = False

    def start_device(self):
        return {
            "device_code": "upstream-secret",
            "user_code": "ABCD",
            "verification_uri": "https://github.com/login/device",
            "interval": 0,
        }

    def poll_device(self, code):
        if self.pending:
            return None
        return self.subject


def es256_material():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return key, pem, jwk, public_jwk_thumbprint(jwk)


def ed25519_material():
    key = ed25519.Ed25519PrivateKey.generate()
    jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(key.public_key()))
    return key, jwk, public_jwk_thumbprint(jwk)


def dpop_proof(private_key, *, htm, path, now=NOW, ath=None, jti=None, jwk=None, alg="ES256",
               iat=None, htu=None, extra_claims=None, private_jwk=False, public_url=PUBLIC_URL):
    if jwk is None:
        if alg == "EdDSA":
            jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(private_key.public_key()))
        else:
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key()))
    if private_jwk:
        jwk = dict(jwk)
        jwk["d"] = "private-material"
    headers = {"typ": "dpop+jwt", "alg": alg, "jwk": jwk}
    claims = {
        "jti": jti or secrets.token_urlsafe(16),
        "htm": htm,
        "htu": htu or canonical_htu(public_url, path),
        "iat": int((iat or now).timestamp()),
    }
    if ath is not None:
        claims["ath"] = ath
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_key, algorithm=alg if alg != "none" else "HS256", headers=headers)


def dpop_headers(private_key, *, method, path, access_token=None, now=NOW, **kwargs):
    ath = access_token_hash(access_token) if access_token is not None else None
    proof = dpop_proof(private_key, htm=method, path=path, now=now, ath=ath, **kwargs)
    headers = {"DPoP": proof, "Content-Type": "application/json"}
    if access_token is not None:
        headers["Authorization"] = "DPoP " + access_token
    return headers
