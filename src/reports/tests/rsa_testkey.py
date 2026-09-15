"""Test-only RSA keypair and RS256 signer.

The server verifies Cloudflare Access tokens for real, so the tests have to sign for
real too — asserting on a stub verifier would prove nothing about the signature path.
Keys are generated in-process to avoid committing key material, and 1024 bits is chosen
because these keys exist for a few milliseconds inside one test run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_SMALL_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47)


def _is_probable_prime(candidate: int, rounds: int = 24) -> bool:
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate % prime == 0:
            return candidate == prime
    d = candidate - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(candidate - 3) + 2
        x = pow(a, d, candidate)
        if x in (1, candidate - 1):
            continue
        for _ in range(r - 1):
            x = x * x % candidate
            if x == candidate - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


class RsaTestKey:
    def __init__(self, bits: int = 1024, kid: str = "test-kid") -> None:
        e = 65537
        while True:
            p = _prime(bits // 2)
            q = _prime(bits // 2)
            if p == q:
                continue
            phi = (p - 1) * (q - 1)
            if phi % e == 0:
                continue
            self.n = p * q
            self.e = e
            self.d = pow(e, -1, phi)
            self.kid = kid
            return

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def jwks(self) -> bytes:
        return json.dumps(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": self.kid,
                        "alg": "RS256",
                        "use": "sig",
                        "n": _b64(self.n.to_bytes(self.size, "big")),
                        "e": _b64((self.e).to_bytes(3, "big")),
                    }
                ]
            }
        ).encode("utf-8")

    def sign(self, message: bytes) -> bytes:
        tail = DIGEST_INFO + hashlib.sha256(message).digest()
        padding = self.size - 3 - len(tail)
        em = b"\x00\x01" + b"\xff" * padding + b"\x00" + tail
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(self.size, "big")

    def token(self, claims: dict[str, Any], *, header: dict[str, Any] | None = None) -> str:
        head = header or {"alg": "RS256", "kid": self.kid, "typ": "JWT"}
        signing_input = f"{_segment(head)}.{_segment(claims)}"
        return f"{signing_input}.{_b64(self.sign(signing_input.encode('ascii')))}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _segment(data: dict[str, Any]) -> str:
    return _b64(json.dumps(data, separators=(",", ":")).encode("utf-8"))
