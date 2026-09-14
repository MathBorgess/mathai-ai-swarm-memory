"""Cloudflare Access JWT verification, standard library only.

The signature is actually checked. A header like `Cf-Access-Authenticated-User-Email` is
trivially forged by anyone who can reach the socket, so it is never read: the only thing
that establishes the owner is an RS256 signature over the token, made by a key fetched
from the configured `certs_url` on the configured team domain, with `iss`, `aud`, `exp`
and the email allowlist all checked.

RSA PKCS#1 v1.5 verification is `pow(s, e, n)` plus a constant-time comparison against the
reconstructed EMSA encoding (RFC 8017 §8.2.2), which is a few lines of integer arithmetic
and keeps this package dependency-free.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from swarm_reports.server.config import AccessConfig

#: DER prefix of DigestInfo(SHA-256), RFC 8017 §9.2 note 1.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
JWKS_CACHE_SECONDS = 900


class AccessDenied(Exception):
    """Authentication failed. The reason is logged, never returned to the client."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AccessIdentity:
    email: str
    subject: str


def _b64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (ValueError, TypeError) as exc:
        raise AccessDenied("malformed_token", "base64url segment") from exc


def _rsa_pkcs1_v15_sha256_verify(n: int, e: int, signature: bytes, message: bytes) -> bool:
    size = (n.bit_length() + 7) // 8
    if len(signature) != size or size < 3 + 11 + len(_SHA256_DIGEST_INFO) + 32:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(size, "big")
    digest = hashlib.sha256(message).digest()
    tail = _SHA256_DIGEST_INFO + digest
    padding_length = size - 3 - len(tail)
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + tail
    return hmac.compare_digest(em, expected)


class JwksCache:
    """Fetches the configured JWKS over HTTPS and caches it briefly."""

    def __init__(
        self,
        certs_url: str,
        *,
        ttl_seconds: int = JWKS_CACHE_SECONDS,
        opener: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.certs_url = certs_url
        self.ttl_seconds = ttl_seconds
        self._opener = opener or _https_get
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched_at = 0.0

    def keys(self, *, force: bool = False) -> dict[str, tuple[int, int]]:
        with self._lock:
            age = self._clock() - self._fetched_at
            if self._keys and not force and age < self.ttl_seconds:
                return dict(self._keys)
            raw = self._opener(self.certs_url)
            self._keys = _parse_jwks(raw)
            self._fetched_at = self._clock()
            return dict(self._keys)


def _https_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise AccessDenied("jwks_unavailable", "certs_url must be https")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - https enforced
        return response.read(512 * 1024)


def _parse_jwks(raw: bytes) -> dict[str, tuple[int, int]]:
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AccessDenied("jwks_unavailable", "certs endpoint returned non-JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys:
        raise AccessDenied("jwks_unavailable", "certs endpoint has no keys")
    out: dict[str, tuple[int, int]] = {}
    for entry in keys:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        kid = entry.get("kid")
        n_raw = entry.get("n")
        e_raw = entry.get("e")
        if not isinstance(kid, str) or not isinstance(n_raw, str) or not isinstance(e_raw, str):
            continue
        n = int.from_bytes(_b64url(n_raw), "big")
        e = int.from_bytes(_b64url(e_raw), "big")
        if n <= 0 or e <= 0:
            continue
        out[kid] = (n, e)
    if not out:
        raise AccessDenied("jwks_unavailable", "certs endpoint has no usable RSA keys")
    return out


def verify_access_jwt(
    token: str,
    config: AccessConfig,
    jwks: JwksCache,
    *,
    now: float | None = None,
) -> AccessIdentity:
    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        raise AccessDenied("malformed_token", "expected three JWT segments")
    header_raw, payload_raw, signature_raw = parts

    try:
        header = json.loads(_b64url(header_raw))
        claims = json.loads(_b64url(payload_raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AccessDenied("malformed_token", "header or payload is not JSON") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AccessDenied("malformed_token", "header or payload is not an object")

    if header.get("alg") != "RS256":
        # Refusing anything else also refuses `alg: none` and HMAC confusion.
        raise AccessDenied("bad_algorithm", f"alg {header.get('alg')!r} is not RS256")
    kid = header.get("kid")
    if not isinstance(kid, str):
        raise AccessDenied("malformed_token", "missing kid")

    keys = jwks.keys()
    key = keys.get(kid)
    if key is None:
        keys = jwks.keys(force=True)
        key = keys.get(kid)
    if key is None:
        raise AccessDenied("unknown_key", "kid is not in the configured JWKS")

    message = f"{header_raw}.{payload_raw}".encode("ascii")
    if not _rsa_pkcs1_v15_sha256_verify(key[0], key[1], _b64url(signature_raw), message):
        raise AccessDenied("bad_signature", "RS256 verification failed")

    if claims.get("iss") != config.issuer:
        raise AccessDenied("bad_issuer", "iss does not match the configured team domain")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if config.audience not in [a for a in audiences if isinstance(a, str)]:
        raise AccessDenied("bad_audience", "aud does not contain the configured AUD tag")

    current = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise AccessDenied("malformed_token", "missing exp")
    if current > float(exp) + config.leeway_seconds:
        raise AccessDenied("expired", "token expired")
    for claim in ("nbf", "iat"):
        value = claims.get(claim)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if current + config.leeway_seconds < float(value):
                raise AccessDenied("not_yet_valid", f"{claim} is in the future")

    email = str(claims.get("email") or "").strip().lower()
    if not email or email not in config.allowed_emails:
        raise AccessDenied("not_allowed", "email is not on the allowlist")

    return AccessIdentity(email=email, subject=str(claims.get("sub") or ""))


def build_unsigned_test_token(header: dict[str, Any], claims: dict[str, Any]) -> str:
    """Helper for tests that assert an unsigned or wrongly signed token is refused."""

    def segment(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{segment(header)}.{segment(claims)}."
