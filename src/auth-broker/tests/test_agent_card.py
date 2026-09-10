import json
from fastapi.testclient import TestClient

from app.agent_card import OAUTH_SCOPES, build_agent_card
from app.api import create_app


class Owner:
    def verify(self, assertion):
        return None


class Hermes:
    def query_as_broker(self, *, agent_id, query):
        return {"ok": True}


def test_card_is_structured_github_oauth_contract():
    card = build_agent_card()
    scheme = card["securitySchemes"]["githubOAuth"]
    flow = scheme["flows"]["authorizationCode"]
    assert card["url"] == "https://a2a.mathai.com.br"
    assert scheme["type"] == "oauth2"
    assert scheme["x-provider"] == "github"
    assert flow["authorizationUrl"] == "https://a2a.mathai.com.br/v1/oauth/github/start"
    assert flow["tokenUrl"] == "https://a2a.mathai.com.br/v1/oauth/github/token"
    assert flow["scopes"] == OAUTH_SCOPES
    assert card["security"] == [{"githubOAuth": list(OAUTH_SCOPES)}]


def test_card_has_no_auth_instructions_or_secret_material():
    rendered = json.dumps(build_agent_card()).lower()
    assert "auth-broker" not in rendered
    assert "client_secret" not in rendered
    assert "bearer" not in rendered


def test_card_endpoint_is_public_and_uses_configured_origin(tmp_path):
    app = create_app(database_path=tmp_path / "broker.sqlite3", audience="https://pair.example.test",
                     owner_verifier=Owner(), hermes=Hermes())
    with TestClient(app) as client:
        response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    assert response.json()["url"] == "https://a2a.mathai.com.br"
