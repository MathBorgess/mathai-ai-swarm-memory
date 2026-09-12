"""Integration round: C/D routers mounted behind the real DPoP verifier of A.

Every other swarm test injects a fake `authorize`. This one does not: the token
comes from the Device Flow, the proof is verified once per request, and the
filter is the real one. It is the smoke the handoff contract asks for —
two principals, two harnesses, private boundary, handle re-authorization,
idempotent propose, legacy Bearer untouched.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from app.context import ContextStore, build_router as build_context_router
from app.context.store import ingest_manifest
from app.proposals import ProposalStore, build_router as build_proposal_router
from swarm_helpers import (
    AUDIENCE,
    PUBLIC_URL,
    WORKSPACE,
    FakeHermes,
    GitHubOAuth,
    dpop_headers,
    es256_material,
)
from test_context_manifest import abc_entries, abc_pages, manifest, write_pages
from test_proposal_router import REPO, FakeGitHub

READ = "ctx:read:pesquisa.tcc"
PROPOSE = "ctx:propose:pesquisa.tcc"
# app/context/query.py compares expires_at against the wall clock, so this smoke
# runs on a live clock instead of the frozen NOW the auth-only tests use.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def signing():
    return es256_material()


def _context_store(tmp_path):
    root = tmp_path / "content"
    write_pages(root, abc_pages())
    store = ContextStore(tmp_path / "context.sqlite3")
    ingest_manifest(store, manifest(abc_entries()), root)
    return store


def _swarm(tmp_path, principals, *, github=None, hermes=None, signing_pem):
    """Broker with both routers installed, exactly as main.py wires them."""
    path = tmp_path / "broker.sqlite3"
    with closing(SqlitePairingStore(path)) as store:
        for principal_id, role, jwk, scopes in principals:
            store.add_principal(principal_id, github_subject="12345", jwk=jwk, role=role, now=NOW)
            for scope in scopes:
                store.set_grant(principal_id, scope, NOW, NOW + timedelta(days=30))
    context_store = _context_store(tmp_path)
    proposal_store = ProposalStore(tmp_path / "proposals.sqlite3")
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(),
        hermes=hermes or FakeHermes(), clock=lambda: NOW, github_oauth=GitHubOAuth(),
        github_allowed_user_id="12345", token_signing_key=signing_pem,
        workspace_id=WORKSPACE, public_url=PUBLIC_URL,
        context_router_factory=lambda *, authorize: build_context_router(
            authorize=authorize, store=context_store,
        ),
        proposal_router_factory=lambda *, authorize: build_proposal_router(
            authorize=authorize, store=proposal_store, github=github or FakeGitHub(),
            repository=REPO, clock=lambda: NOW,
        ),
    )
    return TestClient(app), context_store, proposal_store


def _token(client, key, principal_id, scopes):
    start = client.post(
        "/v1/oauth/github/device/start",
        json={"principal_id": principal_id, "scopes": list(scopes)},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start", now=NOW),
    ).json()
    return client.post(
        "/v1/oauth/github/device/poll",
        json={"device_code": start["device_code"],
              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/poll", now=NOW),
    ).json()["access_token"]


def _post(client, key, token, path, payload, **kwargs):
    return client.post(
        path, json=payload,
        headers=dpop_headers(key, method="POST", path=path, access_token=token, now=NOW) | kwargs.pop("headers", {}),
        **kwargs,
    )


def test_capabilities_announce_only_installed_operations(tmp_path, signing):
    key, pem, jwk, _ = signing
    client, ctx, _ = _swarm(tmp_path, [("advisor-01", "advisor", jwk, [READ, PROPOSE])], signing_pem=pem)
    with client:
        token = _token(client, key, "advisor-01", [READ, PROPOSE])
        caps = client.get(
            "/v1/context/capabilities",
            headers=dpop_headers(key, method="GET", path="/v1/context/capabilities", access_token=token, now=NOW),
        ).json()
        assert caps["operations"] == ["capabilities", "query", "resolve", "propose"]
        assert caps["principal_id"] == "advisor-01"
    ctx.close()


def test_advisor_query_is_filtered_and_one_proof_per_request(tmp_path, signing):
    key, pem, jwk, _ = signing
    client, ctx, _ = _swarm(tmp_path, [("advisor-01", "advisor", jwk, [READ])], signing_pem=pem)
    with client:
        token = _token(client, key, "advisor-01", [READ])
        visible = _post(client, key, token, "/v1/context/query", {"query": "token-a"})
        assert visible.status_code == 200, visible.text
        payload = visible.json()
        assert payload["capability_receipt"]["principal_id"] == "advisor-01"
        assert payload["capability_receipt"]["scopes_used"] == [READ]
        assert payload["items"], "the advisor must see the shared node"
        assert all("Hidden B" not in item["text"] for item in payload["items"])

        # private:true never reaches the advisor, and the boundary is silent.
        assert _post(client, key, token, "/v1/context/query", {"query": "token-b"}).json()["items"] == []

        # A fresh request needs a fresh proof: the mounted router consumed the jti once.
        replay = client.post(
            "/v1/context/query", json={"query": "token-a"},
            headers=dpop_headers(key, method="POST", path="/v1/context/query",
                                 access_token=token, jti="fixed-jti", now=NOW),
        )
        assert replay.status_code == 200
        again = client.post(
            "/v1/context/query", json={"query": "token-a"},
            headers=dpop_headers(key, method="POST", path="/v1/context/query",
                                 access_token=token, jti="fixed-jti", now=NOW),
        )
        assert again.status_code == 401
    ctx.close()


def test_handle_is_reauthorized_for_a_second_principal(tmp_path, signing):
    key_advisor, pem, jwk_advisor, _ = signing
    key_other, _, jwk_other, _ = es256_material()
    client, ctx, _ = _swarm(
        tmp_path,
        [("advisor-01", "advisor", jwk_advisor, [READ]),
         ("advisor-02", "advisor", jwk_other, [PROPOSE])],
        signing_pem=pem,
    )
    with client:
        reader = _token(client, key_advisor, "advisor-01", [READ])
        handle = _post(client, key_advisor, reader, "/v1/context/query",
                       {"query": "token-a"}).json()["items"][0]["handle"]
        assert _post(client, key_advisor, reader, "/v1/context/resolve",
                     {"handles": [handle]}).json()["items"], "owner of the grant resolves it"

        # Same handle, principal without ctx:read: possession is not authority.
        propose_only = _token(client, key_other, "advisor-02", [PROPOSE])
        denied = _post(client, key_other, propose_only, "/v1/context/resolve", {"handles": [handle]})
        assert denied.status_code == 403
    ctx.close()


def test_propose_reaches_github_once_per_idempotency_key(tmp_path, signing):
    key, pem, jwk, _ = signing
    github = FakeGitHub()
    client, ctx, _ = _swarm(
        tmp_path, [("advisor-01", "advisor", jwk, [PROPOSE])], github=github, signing_pem=pem,
    )
    body = {
        "namespace": "pesquisa.tcc",
        "title": "Nota de leitura",
        "body_markdown": "Afirmação não verificada.",
        "sources": [{"url": "https://example.test/paper", "label": "paper"}],
    }
    with client:
        token = _token(client, key, "advisor-01", [PROPOSE])
        first = _post(client, key, token, "/v1/context/propose", body,
                      headers={"Idempotency-Key": "key-1"})
        assert first.status_code in (200, 201), first.text
        created = first.json()
        assert created["pr_url"].startswith("https://github.com/acme/discard-t2-inbox/pull/")
        repeat = _post(client, key, token, "/v1/context/propose", body,
                       headers={"Idempotency-Key": "key-1"})
        assert repeat.json()["proposal_id"] == created["proposal_id"]
        assert len(github.prs) == 1
        assert all(path.startswith("pesquisa/tcc/inbox/") for _, path in github.files)
    ctx.close()


def test_installed_routers_do_not_shadow_the_legacy_bearer_path(tmp_path, signing):
    key, pem, jwk, _ = signing
    hermes = FakeHermes()
    client, ctx, _ = _swarm(
        tmp_path, [("advisor-01", "advisor", jwk, [READ])], hermes=hermes, signing_pem=pem,
    )
    with client:
        legacy = client.post("/v1/oauth/github/device/start", json={}).json()
        session = client.post(
            "/v1/oauth/github/device/poll",
            json={"device_code": legacy["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
        ).json()
        assert session["token_type"] == "Bearer"
        answer = client.post(
            "/v1/context/query", json={"query": "hello"},
            headers={"Authorization": "Bearer " + session["access_token"]},
        )
        assert answer.json() == {"answer": "hermes-only"}
        # The a2a session still cannot enter the swarm surface.
        assert client.get(
            "/v1/context/capabilities",
            headers={"Authorization": "Bearer " + session["access_token"]},
        ).status_code == 401
    assert hermes.queries == [("github:12345", "hello")]
    ctx.close()
