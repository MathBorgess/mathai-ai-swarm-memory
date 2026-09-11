"""HTTP router: injected authorize, input bounds, envelope, no body-declared policy."""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.context import build_router
from test_context_manifest import advisor, ingest_abc, owner, titles


def app_client(store, authorize):
    app = FastAPI()
    app.include_router(build_router(authorize=authorize, store=store))
    return TestClient(app)


def test_query_envelope_and_opaque_handles(tmp_path):
    store, _ = ingest_abc(tmp_path)
    client = app_client(store, lambda request: advisor())
    response = client.post("/v1/context/query", json={"query": "token-a"})
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == {"items", "capability_receipt"}
    assert set(payload["capability_receipt"]) == {
        "principal_id",
        "workspace_id",
        "scopes_used",
        "pass_as",
        "policy_version",
    }
    item = payload["items"][0]
    assert set(item) == {"handle", "text", "source_revision"}
    assert "/" not in item["handle"] and "pesquisa" not in item["handle"]
    assert "Hidden B" not in item["text"]
    store.close()


def test_body_cannot_declare_principal_or_policy(tmp_path):
    store, _ = ingest_abc(tmp_path)
    client = app_client(store, lambda request: advisor())
    for body in (
        {
            "query": "token-b",
            "principal_id": "owner-01",
            "scopes": ["ctx:read:pesquisa.tcc"],
            "classifications": ["restricted"],
        },
        {"query": "token-a", "workspace_id": "other"},
        {"query": "token-a", "namespaces_omitted": []},
    ):
        assert client.post("/v1/context/query", json=body).status_code == 400
    assert client.post("/v1/context/query", json={"query": "token-b"}).json()["items"] == []
    store.close()


def test_missing_or_invalid_authorize_is_401(tmp_path):
    store, _ = ingest_abc(tmp_path)

    def missing(request):
        raise HTTPException(401, "Invalid credential")

    client = app_client(store, missing)
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 401
    store.close()


def test_propose_scope_is_403_not_empty_success(tmp_path):
    store, _ = ingest_abc(tmp_path)
    client = app_client(store, lambda request: advisor(scopes=("ctx:propose:pesquisa.tcc",)))
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 403
    assert client.post("/v1/context/resolve", json={"handles": ["x"]}).status_code == 403
    store.close()


def test_unknown_read_scope_is_403(tmp_path):
    store, _ = ingest_abc(tmp_path)
    client = app_client(store, lambda request: advisor(scopes=("ctx:read:wiki", "ctx:write:wiki")))
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 403
    store.close()


def test_revocation_via_fake_authorize(tmp_path):
    store, _ = ingest_abc(tmp_path)
    state = {"status": None}

    def authorize(request):
        if state["status"]:
            raise HTTPException(state["status"], "Invalid credential")
        return advisor()

    client = app_client(store, authorize)
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 200
    state["status"] = 401
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 401
    store.close()


def test_input_bounds(tmp_path):
    store, _ = ingest_abc(tmp_path)
    client = app_client(store, lambda request: advisor())
    assert client.post("/v1/context/query", json={"query": ""}).status_code == 400
    assert client.post("/v1/context/query", json={"query": "token-a", "limit": 0}).status_code == 400
    assert client.post("/v1/context/query", json={"query": "token-a", "limit": 51}).status_code == 400
    assert client.post("/v1/context/query", json={"query": "x" * 2001}).status_code == 413
    assert client.post("/v1/context/query", content=b"x" * 16385, headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/v1/context/resolve", json={"handles": ["h"] * 51}).status_code == 413
    assert client.post("/v1/context/resolve", json={"handles": [""]}).status_code == 400
    assert client.post("/v1/context/query", json=["token-a"]).status_code == 400
    store.close()


def test_default_limit_is_ten(tmp_path):
    extra_pages = {f"pesquisa/tcc/v{i}.md": f"# Grid {i}\n\ngrid-token\n" for i in range(12)}
    extra_entries = [
        {"path": f"pesquisa/tcc/v{i}.md", "namespace": "pesquisa.tcc", "classification": "shared", "layer": "notes"}
        for i in range(12)
    ]
    store, _ = ingest_abc(tmp_path, extra_entries=extra_entries, extra_pages=extra_pages)
    client = app_client(store, lambda request: advisor())
    payload = client.post("/v1/context/query", json={"query": "grid-token"}).json()
    assert len(payload["items"]) == 10
    limited = client.post("/v1/context/query", json={"query": "grid-token", "limit": 1}).json()
    assert len(limited["items"]) == 1
    store.close()


def test_resolve_endpoint_reauthorizes(tmp_path):
    store, _ = ingest_abc(tmp_path)
    hidden = titles(store)["Hidden B"].handle
    visible = titles(store)["Visible A"].handle
    client = app_client(store, lambda request: advisor())
    denied = client.post("/v1/context/resolve", json={"handles": [hidden, visible, "missing"]}).json()
    assert [item["handle"] for item in denied["items"]] == [visible]
    owner_client = app_client(store, lambda request: owner())
    allowed = owner_client.post("/v1/context/resolve", json={"handles": [hidden]}).json()
    assert allowed["items"][0]["handle"] == hidden
    store.close()


def test_missing_store_is_503_not_hermes(tmp_path):
    client = app_client(None, lambda request: advisor())
    response = client.post("/v1/context/query", json={"query": "token-a"})
    assert response.status_code == 503
    assert "hermes" not in response.text.lower()


def test_expired_mapping_is_401(tmp_path):
    store, _ = ingest_abc(tmp_path)
    expired = advisor(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    client = app_client(store, lambda request: expired)
    assert client.post("/v1/context/query", json={"query": "token-a"}).status_code == 401
    store.close()


def test_context_package_does_not_import_hermes():
    import inspect

    import app.context.query as query
    import app.context.router as router
    import app.context.store as store

    for module in (query, router, store):
        source = inspect.getsource(module)
        assert "adapters.hermes" not in source
        assert "query_as_broker" not in source
        assert "Graphiti" not in source
        assert "graphiti" not in source
