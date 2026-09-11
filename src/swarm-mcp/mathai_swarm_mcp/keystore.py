"""OS keystore for the DPoP private key and refresh token. Access tokens stay in memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from mathai_swarm_mcp.dpop import generate_private_jwk, jwk_thumbprint, public_jwk
from mathai_swarm_mcp.errors import KeystoreError
from mathai_swarm_mcp.origin import parse_origin

SERVICE = "mathai-swarm-mcp"
_INSECURE = ("fail", "file", "null", "plaintext", "encryptedkeyring")
_SECURE = ("secretservice", "macos", "winvault", "kwallet", "chainer")


@dataclass
class StoredPrincipal:
    origin: str
    principal_id: str
    private_jwk: dict
    refresh_token: str | None = None
    scope: str | None = None


class CredentialStore(Protocol):
    def load(self, origin: str, principal_id: str) -> StoredPrincipal | None: ...
    def save(self, record: StoredPrincipal) -> None: ...
    def delete(self, origin: str, principal_id: str) -> None: ...


class MemoryStore:
    """In-process store for tests. Not a production backend and not selectable from the CLI."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredPrincipal] = {}

    def load(self, origin: str, principal_id: str) -> StoredPrincipal | None:
        return self._records.get(_key(origin, principal_id))

    def save(self, record: StoredPrincipal) -> None:
        self._records[_key(record.origin, record.principal_id)] = record

    def delete(self, origin: str, principal_id: str) -> None:
        self._records.pop(_key(origin, principal_id), None)


class KeyringStore:
    def __init__(self, backend=None) -> None:
        import keyring

        self._backend = backend if backend is not None else keyring.get_keyring()
        _assert_secure(self._backend)

    def load(self, origin: str, principal_id: str) -> StoredPrincipal | None:
        payload = self._backend.get_password(SERVICE, _slot(origin, principal_id))
        if not payload:
            return None
        try:
            data = json.loads(payload)
            jwk = data["private_jwk"]
            if not isinstance(jwk, dict) or "d" not in jwk:
                raise ValueError("missing private key")
            return StoredPrincipal(
                origin=parse_origin(data["origin"]),
                principal_id=str(data["principal_id"]),
                private_jwk=jwk,
                refresh_token=data.get("refresh_token"),
                scope=data.get("scope"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KeystoreError("stored credential is unreadable") from exc

    def save(self, record: StoredPrincipal) -> None:
        payload = json.dumps(
            {
                "v": 1,
                "origin": parse_origin(record.origin),
                "principal_id": record.principal_id,
                "private_jwk": record.private_jwk,
                "refresh_token": record.refresh_token,
                "scope": record.scope,
            },
            separators=(",", ":"),
        )
        self._backend.set_password(SERVICE, _slot(record.origin, record.principal_id), payload)

    def delete(self, origin: str, principal_id: str) -> None:
        try:
            self._backend.delete_password(SERVICE, _slot(origin, principal_id))
        except Exception:
            return


def ensure_key(store: CredentialStore, origin: str, principal_id: str) -> StoredPrincipal:
    origin = parse_origin(origin)
    record = store.load(origin, principal_id)
    if record is not None:
        return record
    record = StoredPrincipal(origin=origin, principal_id=principal_id, private_jwk=generate_private_jwk())
    store.save(record)
    return record


def public_material(record: StoredPrincipal) -> dict:
    return {"jwk": public_jwk(record.private_jwk), "jkt": jwk_thumbprint(record.private_jwk)}


def _key(origin: str, principal_id: str) -> tuple[str, str]:
    return parse_origin(origin), principal_id


def _slot(origin: str, principal_id: str) -> str:
    return f"{parse_origin(origin)}::{principal_id}"


def _assert_secure(backend) -> None:
    if _secure(backend):
        return
    raise KeystoreError(
        "no secure OS keystore backend is available; refusing a file or fail backend. "
        "Install a system keyring (Secret Service, macOS Keychain, or Windows Credential Manager)."
    )


def _secure(backend) -> bool:
    nested = getattr(backend, "backends", None) or getattr(backend, "_backends", None)
    if nested:
        return any(_secure(item) for item in nested)
    qual = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    if any(marker in qual for marker in _INSECURE):
        return False
    return any(marker in qual for marker in _SECURE)
