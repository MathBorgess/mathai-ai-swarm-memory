"""Validate the owner using a signed Cloudflare Access application JWT.

Trust anchors are local configuration, never fields or URLs supplied by a token.
"""

import re
from typing import Protocol

import jwt


class OwnerAuthenticationError(ValueError):
    pass


class OwnerAssertionVerifier(Protocol):
    def verify(self, assertion: str) -> None: ...


class CloudflareAccessVerifier:
    def __init__(self, issuer: str, audience: str, owner_email: str):
        if not re.fullmatch(r"https://[a-zA-Z0-9-]+\.cloudflareaccess\.com", issuer):
            raise ValueError("Expected a Cloudflare Access team issuer")
        if not audience or not owner_email:
            raise ValueError("Access audience and owner identity are required")
        self.issuer = issuer
        self.audience = audience
        self.owner_email = owner_email
        self.jwks = jwt.PyJWKClient(issuer + "/cdn-cgi/access/certs", timeout=5)

    def verify(self, assertion: str) -> None:
        try:
            key = self.jwks.get_signing_key_from_jwt(assertion).key
            claims = jwt.decode(
                assertion, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]},
            )
            if claims["email"] != self.owner_email:
                raise OwnerAuthenticationError("Invalid owner assertion")
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            raise OwnerAuthenticationError("Invalid owner assertion") from None
