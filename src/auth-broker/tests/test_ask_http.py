"""HTTP router for POST /v1/context/ask: injected authorize, bounds, no body policy."""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.ask import AskBudget, IsolationConfig, MemoryThreadStore, build_router, worker_root
from test_ask import RecordingWorker, _ingest
from test_context_manifest import advisor


def _isolation(tmp_path):
    home = tmp_path / "ask-runtime"
    home.mkdir()
    (home / "home").mkdir()
    root = worker_root()
    return IsolationConfig(
        runtime_home=home,
        image="mathai-ask-worker:test",
        launch_script=root / "launch.sh",
        worker_script=root / "worker_main.py",
        style_path=root / "style" / "SOUL.md",
        docker_bin=tmp_path / "docker-not-used",
        network="none",
    )


def app_client(tmp_path, store, authorize, worker=None, isolation="ok"):
    worker = worker or RecordingWorker()
    kwargs = {
        "authorize": authorize,
        "store": store,
        "worker": worker,
        "threads": MemoryThreadStore(),
        "budget": AskBudget(timeout_seconds=2, max_concurrency=2),
    }
    if isolation == "ok":
        kwargs["isolation"] = _isolation(tmp_path)
    else:
        kwargs["isolation"] = isolation
    app = FastAPI()
    app.include_router(build_router(**kwargs))
    return TestClient(app), worker


def test_ask_envelope_has_prose_and_receipt(tmp_path):
    store, _ = _ingest(tmp_path)
    client, worker = app_client(tmp_path, store, lambda request: advisor())
    response = client.post("/v1/context/ask", json={"query": "token-a"})
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == {"items", "capability_receipt", "thread_id"}
    assert payload["capability_receipt"]["pass_as"] == "handle"
    item = payload["items"][0]
    assert "text" in item and item["text"].startswith("prose:")
    assert "cited_handles" in item
    assert worker.payloads
    store.close()


def test_body_cannot_declare_principal_or_workspace(tmp_path):
    store, _ = _ingest(tmp_path)
    client, _ = app_client(tmp_path, store, lambda request: advisor())
    for body in (
        {"query": "token-a", "principal_id": "owner-01"},
        {"query": "token-a", "workspace_id": "other"},
        {"query": "token-a", "scopes": ["ctx:read:pesquisa.tcc"]},
        {"query": "token-a", "classifications": ["restricted"]},
    ):
        assert client.post("/v1/context/ask", json=body).status_code == 400
    store.close()


def test_missing_store_or_isolation_is_503_not_hermes(tmp_path):
    store, _ = _ingest(tmp_path)
    client, _ = app_client(tmp_path, None, lambda request: advisor())
    response = client.post("/v1/context/ask", json={"query": "token-a"})
    assert response.status_code == 503
    assert "hermes" not in response.text.lower()
    client, _ = app_client(tmp_path, store, lambda request: advisor(), isolation=None)
    response = client.post("/v1/context/ask", json={"query": "token-a"})
    assert response.status_code == 503
    assert "hermes" not in response.text.lower()
    store.close()


def test_propose_scope_is_403(tmp_path):
    store, _ = _ingest(tmp_path)
    client, worker = app_client(
        tmp_path, store, lambda request: advisor(scopes=("ctx:propose:pesquisa.tcc",))
    )
    assert client.post("/v1/context/ask", json={"query": "token-a"}).status_code == 403
    assert worker.payloads == []
    store.close()


def test_thread_round_trip(tmp_path):
    store, _ = _ingest(tmp_path)
    client, worker = app_client(tmp_path, store, lambda request: advisor())
    first = client.post("/v1/context/ask", json={"query": "token-a"}).json()
    second = client.post(
        "/v1/context/ask", json={"query": "token-notes", "thread_id": first["thread_id"]}
    )
    assert second.status_code == 200
    assert second.json()["thread_id"] == first["thread_id"]
    assert worker.payloads[-1]["prior_user_turns"]
    store.close()


def test_input_bounds(tmp_path):
    store, _ = _ingest(tmp_path)
    client, _ = app_client(tmp_path, store, lambda request: advisor())
    assert client.post("/v1/context/ask", json={"query": ""}).status_code == 400
    assert client.post("/v1/context/ask", json={"query": "x" * 2001}).status_code == 413
    assert client.post("/v1/context/ask", content=b"x" * 16385, headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/v1/context/ask", json=["token-a"]).status_code == 400
    store.close()


def test_expired_mapping_is_401(tmp_path):
    store, _ = _ingest(tmp_path)
    expired = advisor(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    client, _ = app_client(tmp_path, store, lambda request: expired)
    assert client.post("/v1/context/ask", json={"query": "token-a"}).status_code == 401
    store.close()


def test_invalid_authorize_is_401(tmp_path):
    store, _ = _ingest(tmp_path)

    def missing(request):
        raise HTTPException(401, "Invalid credential")

    client, _ = app_client(tmp_path, store, missing)
    assert client.post("/v1/context/ask", json={"query": "token-a"}).status_code == 401
    store.close()
