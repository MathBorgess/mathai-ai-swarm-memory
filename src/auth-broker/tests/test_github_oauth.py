from datetime import datetime, timezone, timedelta
import hashlib

from fastapi.testclient import TestClient
from app.api import create_app

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)

class OAuth:
    def exchange_and_identify(self, code, redirect_uri):
        assert (code, redirect_uri) == ("github-code", "https://client.example/callback")
        return "12345"

class Hermes:
    def __init__(self): self.queries = []
    def query_as_broker(self, *, agent_id, query):
        self.queries.append((agent_id, query)); return {"answer": "ok"}

def test_github_code_becomes_short_lived_internal_session(tmp_path):
    hermes = Hermes()
    app = create_app(database_path=tmp_path / "db", audience="https://pair.example",
                     owner_verifier=object(), hermes=hermes, clock=lambda: NOW,
                     github_oauth=OAuth(), agent_lifetime=timedelta(hours=1), github_redirect_uri="https://client.example/callback", github_allowed_user_id="12345")
    with TestClient(app) as client:
        start = client.post("/v1/oauth/github/start", json={}).json()
        response = client.post("/v1/oauth/github/token", json={"code": "github-code", "state": start["state"], "redirect_uri": "https://client.example/callback", "scope": "a2a:discover a2a:message a2a:history"})
        assert response.status_code == 200
        token = response.json()["access_token"]
        assert response.json()["expires_in"] == 3600
        assert client.post("/v1/context/query", json={"query": "hello"}, headers={"Authorization": "Bearer " + token}).json() == {"answer": "ok"}
    raw = (tmp_path / "db").read_bytes()
    assert b"github-code" not in raw and b"access_token" not in raw
    assert hashlib.sha256(token.encode()).hexdigest().encode() in raw

def test_github_token_requires_exact_internal_scopes(tmp_path):
    app = create_app(database_path=tmp_path / "db", audience="https://pair.example", owner_verifier=object(), hermes=Hermes(), github_oauth=OAuth(), github_redirect_uri="https://client.example/callback", github_allowed_user_id="12345")
    with TestClient(app) as client:
        response = client.post("/v1/oauth/github/token", json={"code": "github-code", "state": "bad", "redirect_uri": "https://client.example/callback", "scope": "read:user"})
    assert response.status_code == 403
