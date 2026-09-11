"""Sanitized errors: tokens, proofs, and private keys never leave this module intact."""

from __future__ import annotations

import re

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SCHEME = re.compile(r"(?i)\b(bearer|dpop)\s+[A-Za-z0-9._\-+=/]+")
_JSON_SECRET = re.compile(
    r'(?i)("(?:access_token|refresh_token|device_code|private_jwk|private_key|d)"\s*:\s*")([^"]*)(")'
)
_ASSIGN = re.compile(
    r"(?i)\b(access_token|refresh_token|device_code|private_key|private_jwk|dpop)\s*[:=]\s*\S+"
)


def redact(text: object) -> str:
    value = "" if text is None else str(text)
    value = _JWT.sub("[redacted]", value)
    value = _SCHEME.sub(r"\1 [redacted]", value)
    value = _JSON_SECRET.sub(r"\1[redacted]\3", value)
    value = _ASSIGN.sub(r"\1=[redacted]", value)
    return value


class SwarmMcpError(Exception):
    def __init__(self, message: str):
        super().__init__(redact(message))


class LoginRequiredError(SwarmMcpError):
    pass


class ForbiddenError(SwarmMcpError):
    pass


class OriginError(SwarmMcpError):
    pass


class RedirectRejected(OriginError):
    pass


class KeystoreError(SwarmMcpError):
    pass


class InputError(SwarmMcpError):
    pass


class DependencyUnavailable(SwarmMcpError):
    pass


class ConflictError(SwarmMcpError):
    pass


class PayloadTooLarge(SwarmMcpError):
    pass


class OAuthProtocolError(SwarmMcpError):
    def __init__(self, error: str, detail: str = ""):
        self.error = redact(error)
        super().__init__(self.error if not detail else f"{self.error}: {redact(detail)}")
