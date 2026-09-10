from datetime import datetime, timezone, timedelta
import hashlib

from fastapi.testclient import TestClient
from app.api import create_app

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)

class OAuth:
    def start_device(self):
        return {"device_code": "upstream-secret", "user_code": "ABCD", "verification_uri": "https://github.com/device", "interval": 0}

    def poll_device(self, code):
        return "12345"

class Hermes:
    def __init__(self): self.queries = []
    def query_as_broker(self, *, agent_id, query):
        self.queries.append((agent_id, query)); return {"answer": "ok"}

def test_authorization_code_routes_are_removed(tmp_path):
    app = create_app(database_path=tmp_path / "db", audience="https://pair.example", owner_verifier=object(), hermes=Hermes())
    with TestClient(app) as client:
        assert client.post("/v1/oauth/github/start", json={}).status_code == 404
        assert client.post("/v1/oauth/github/token", json={}).status_code == 404

def test_device_flow_returns_broker_code_and_issues_token(tmp_path):
    app = create_app(database_path=tmp_path / "db", audience="https://pair.example", owner_verifier=object(), hermes=Hermes(), github_oauth=OAuth(), github_allowed_user_id="12345")
    with TestClient(app) as client:
        start = client.post("/v1/oauth/github/device/start", json={}).json()
        assert set(start) >= {"device_code", "user_code", "verification_uri", "interval"}
        assert start["interval"] == 5
        result = client.post("/v1/oauth/github/device/poll", json={"device_code": start["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        assert result.status_code == 200 and "access_token" in result.json()
    assert b"upstream-secret" not in (tmp_path / "db").read_bytes()

def test_device_flow_rejects_replay_and_wrong_grant(tmp_path):
    app = create_app(database_path=tmp_path / "db", audience="https://pair.example", owner_verifier=object(), hermes=Hermes(), github_oauth=OAuth(), github_allowed_user_id="12345")
    with TestClient(app) as client:
        start = client.post("/v1/oauth/github/device/start", json={}).json()
        body = {"device_code": start["device_code"], "grant_type": "wrong"}
        assert client.post("/v1/oauth/github/device/poll", json=body).status_code == 403
        body["grant_type"] = "urn:ietf:params:oauth:grant-type:device_code"
        assert client.post("/v1/oauth/github/device/poll", json=body).status_code == 200
        assert client.post("/v1/oauth/github/device/poll", json=body).status_code == 403
