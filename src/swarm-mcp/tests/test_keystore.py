import json

import pytest

from mathai_swarm_mcp.dpop import generate_private_jwk
from mathai_swarm_mcp.errors import KeystoreError
from mathai_swarm_mcp.keystore import KeyringStore, MemoryStore, StoredPrincipal, ensure_key, public_material


class _FileBackend:
    """Pretend keyring file backend; production must refuse this."""

    def __init__(self):
        self.module = "keyring.backends.file"
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


def test_memory_store_roundtrip():
    store = MemoryStore()
    record = ensure_key(store, "https://a2a.mathai.com.br", "advisor-01")
    record.refresh_token = "refresh_test"
    store.save(record)
    loaded = store.load("https://a2a.mathai.com.br", "advisor-01")
    assert loaded is not None and loaded.refresh_token == "refresh_test"
    material = public_material(loaded)
    assert "d" not in material["jwk"]
    assert material["jkt"]


def test_keyring_store_rejects_file_and_fail_backends():
    class FileKeyring(_FileBackend):
        pass

    FileKeyring.__module__ = "keyring.backends.file"
    FileKeyring.__name__ = "PlaintextKeyring"
    with pytest.raises(KeystoreError, match="secure OS keystore"):
        KeyringStore(backend=FileKeyring())

    class FailKeyring(_FileBackend):
        pass

    FailKeyring.__module__ = "keyring.backends.fail"
    FailKeyring.__name__ = "Keyring"
    with pytest.raises(KeystoreError, match="secure OS keystore"):
        KeyringStore(backend=FailKeyring())


def test_keyring_store_roundtrip_with_secret_service_shaped_backend():
    class SecretServiceKeyring(_FileBackend):
        pass

    SecretServiceKeyring.__module__ = "keyring.backends.SecretService"
    SecretServiceKeyring.__name__ = "Keyring"
    store = KeyringStore(backend=SecretServiceKeyring())
    record = StoredPrincipal(
        origin="https://a2a.mathai.com.br",
        principal_id="advisor-01",
        private_jwk=generate_private_jwk(),
        refresh_token="refresh_test",
    )
    store.save(record)
    loaded = store.load("https://a2a.mathai.com.br", "advisor-01")
    assert loaded is not None and loaded.refresh_token == "refresh_test"
    raw = store._backend.get_password("mathai-swarm-mcp", "https://a2a.mathai.com.br::advisor-01")
    data = json.loads(raw)
    assert "access_token" not in data
