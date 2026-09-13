import importlib
import sys
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from test_api import owner_keys, pair, signed


@pytest.fixture
def configured(monkeypatch, tmp_path):
    values = {
        "AUTH_BROKER_DATABASE_PATH": str(tmp_path / "production.sqlite3"),
        "AUTH_BROKER_AUDIENCE": "https://pair.example.test",
        "HERMES_A2A_URL": "https://hermes.example.test/",
        "HERMES_BROKER_TOKEN": "synthetic-server-only",
        "GITHUB_OAUTH_CLIENT_ID": "synthetic-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "GITHUB_ALLOWED_USER_ID": "12345",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    sys.modules.pop("app.main", None)
    yield values
    sys.modules.pop("app.main", None)


@pytest.mark.parametrize("name", [
    "AUTH_BROKER_DATABASE_PATH", "AUTH_BROKER_AUDIENCE",
    "HERMES_A2A_URL", "HERMES_BROKER_TOKEN", "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET", "GITHUB_ALLOWED_USER_ID",
])
@pytest.mark.parametrize("value", [None, "", " \t"])
def test_required_environment_fails_closed(configured, monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        importlib.import_module("app.main")


def test_production_composition_verifies_owner_and_uses_private_bearer(configured, monkeypatch, owner_keys):
    monkeypatch.setenv("AUTH_BROKER_CF_ACCESS_ISSUER", "https://team.cloudflareaccess.com")
    monkeypatch.setenv("AUTH_BROKER_CF_ACCESS_AUDIENCE", "access-app")
    monkeypatch.setenv("AUTH_BROKER_OWNER_EMAIL", "owner@example.test")
    private, jwk = owner_keys
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    captured = []
    def handle(self, request):
        import json
        captured.append(request)
        payload = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    main = importlib.import_module("app.main")
    now = datetime.now(timezone.utc)
    claims = {"iss": "https://team.cloudflareaccess.com", "aud": "access-app",
              "email": "owner@example.test", "sub": "owner", "iat": now,
              "exp": now + timedelta(minutes=5)}
    with TestClient(main.app) as client:
        key, request_id = pair(client, approve=False)
        endpoint = f"/v1/pairing-requests/{request_id}/approve"
        assert client.post(endpoint, json={}, headers={"Cf-Access-Jwt-Assertion": "test-owner-assertion"}).status_code == 403
        token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"})
        approved = client.post(endpoint, json={}, headers={"Cf-Access-Jwt-Assertion": token})
        assert approved.status_code == 200
        body, headers = signed(key, approved.json()["agent_id"], timestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        headers["Authorization"] = "Bearer synthetic-caller-only"
        assert client.post("/v1/context/query", content=body, headers=headers).json() == {"ok": True}
    assert len(captured) == 1
    assert str(captured[0].url) == "https://hermes.example.test/"
    assert captured[0].headers["Authorization"] == "Bearer synthetic-server-only"
    with pytest.raises(TypeError):
        main.build_app_from_environment(hermes=object())


def test_factory_rechecks_environment(configured, monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.delenv("HERMES_BROKER_TOKEN")
    with pytest.raises(RuntimeError, match="HERMES_BROKER_TOKEN"):
        main.build_app_from_environment()


def test_without_cloudflare_access_starts_and_disables_legacy_pairing(configured):
    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        assert client.post("/v1/pairing-requests", json={"public_key": "ignored"}).status_code == 404
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_mcp_oauth_mounted_when_workspace_and_signing_key_are_set(configured, monkeypatch, tmp_path):
    from swarm_helpers import es256_material

    _, pem, _, _ = es256_material()
    key_path = tmp_path / "signing.pem"
    key_path.write_bytes(pem)
    monkeypatch.setenv("AUTH_BROKER_WORKSPACE_ID", "personal")
    monkeypatch.setenv("AUTH_BROKER_JWT_SIGNING_KEY_PATH", str(key_path))
    monkeypatch.setenv("AUTH_BROKER_PUBLIC_URL", "https://a2a.mathai.com.br")
    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        resource = client.get("/.well-known/oauth-protected-resource")
        assert resource.status_code == 200
        assert resource.json()["resource"] == "https://a2a.mathai.com.br/mcp"
        nested = client.get("/.well-known/oauth-protected-resource/mcp")
        assert nested.status_code == 200
        assert nested.json()["resource"] == "https://a2a.mathai.com.br/mcp"


def test_incomplete_ask_configuration_fails_closed(configured, monkeypatch):
    monkeypatch.setenv("AUTH_BROKER_ASK_IMAGE", "mathai-ask-worker:local")
    with pytest.raises(RuntimeError, match="AUTH_BROKER_ASK_INFERENCE_CONFIG"):
        importlib.import_module("app.main")


def test_ask_without_context_store_fails_closed(configured, monkeypatch, tmp_path):
    inference = tmp_path / "ask-inference.json"
    inference.write_text('{"model":"stub","base_url":"http://127.0.0.1:9","api_key":"x","transport":"stub"}')
    monkeypatch.setenv("AUTH_BROKER_ASK_INFERENCE_CONFIG", str(inference))
    monkeypatch.setenv("AUTH_BROKER_ASK_IMAGE", "mathai-ask-worker:local")
    monkeypatch.setenv("AUTH_BROKER_ASK_NETWORK", "ask-egress")
    with pytest.raises(RuntimeError, match="AUTH_BROKER_CONTEXT_SQLITE"):
        importlib.import_module("app.main")


def test_ask_host_network_fails_closed(configured, monkeypatch, tmp_path):
    inference = tmp_path / "ask-inference.json"
    inference.write_text('{"model":"stub","base_url":"http://127.0.0.1:9","api_key":"x","transport":"stub"}')
    monkeypatch.setenv("AUTH_BROKER_ASK_INFERENCE_CONFIG", str(inference))
    monkeypatch.setenv("AUTH_BROKER_ASK_IMAGE", "mathai-ask-worker:local")
    monkeypatch.setenv("AUTH_BROKER_ASK_NETWORK", "host")
    monkeypatch.setenv("AUTH_BROKER_CONTEXT_SQLITE", str(tmp_path / "context.sqlite3"))
    with pytest.raises(RuntimeError, match="host"):
        importlib.import_module("app.main")
