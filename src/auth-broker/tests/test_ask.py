"""Ask service: current envelope, thread binding, grant downgrade, citations, budget."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.ask import AskBudget, AskBusy, AskTimeout, IsolationUnavailable, MemoryThreadStore
from app.ask.service import AskService
from app.context.query import AuthError
from test_context_manifest import advisor, ingest_abc, owner

SENTINEL = "SENTINEL_HIDDEN_DO_NOT_LEAK"
READ = "ctx:read:pesquisa.tcc"


class RecordingWorker:
    def __init__(self, reply=None, delay=0, cited=None):
        self.payloads = []
        self.reply = reply
        self.delay = delay
        self.cited = cited
        self.calls = 0

    def generate(self, payload: dict) -> dict:
        import time

        self.calls += 1
        self.payloads.append(json.loads(json.dumps(payload)))
        if self.delay:
            time.sleep(self.delay)
        cited = self.cited
        if cited is None:
            cited = [item["handle"] for item in payload.get("envelope") or []]
        text = self.reply
        if text is None:
            text = "prose:" + payload["query"]
        return {"text": text, "cited_handles": cited}


def _inference(tmp_path, **extra):
    path = tmp_path / "ask-inference.json"
    payload = {"transport": "stub", "model": "stub", **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _isolation(tmp_path):
    from app.ask.isolation import IsolationConfig, worker_root

    root = worker_root()
    return IsolationConfig(
        image="mathai-ask-worker:test",
        launch_script=root / "launch.sh",
        worker_script=root / "worker_main.py",
        style_path=root / "style" / "SOUL.md",
        docker_bin=tmp_path / "docker-not-used",
        inference_config=_inference(tmp_path),
        network="none",
    )


def _ingest(tmp_path):
    return ingest_abc(
        tmp_path,
        extra_pages={
            "pesquisa/tcc/b.md": (
                f"# Hidden B\n\ntoken-b secret-bridge {SENTINEL}\n"
                "From [[Visible A]] to [[Visible C]]\n"
            )
        },
    )


def _service(tmp_path, store, worker=None, **kwargs):
    worker = worker or RecordingWorker()
    service = AskService(
        store=store,
        worker=worker,
        threads=kwargs.pop("threads", MemoryThreadStore()),
        isolation=kwargs.pop("isolation", _isolation(tmp_path)),
        budget=kwargs.pop("budget", AskBudget(timeout_seconds=2, max_concurrency=2)),
        clock=kwargs.pop("clock", lambda: datetime.now(timezone.utc)),
        **kwargs,
    )
    return service, worker


def test_hidden_sentinel_is_absent_from_worker_payload(tmp_path):
    store, _ = _ingest(tmp_path)
    service, worker = _service(tmp_path, store)
    result = service.ask(advisor(), "token-a")
    assert result["items"]
    blob = json.dumps(worker.payloads[0])
    assert SENTINEL not in blob
    assert "token-b" not in blob
    assert "Hidden B" not in blob
    envelope = worker.payloads[0]["envelope"]
    assert envelope and all("handle" in item and "text" in item for item in envelope)
    assert worker.payloads[0].get("credentials") is None
    assert "authorization" not in blob.lower()
    assert "HERMES_BROKER_TOKEN" not in blob
    store.close()


def test_empty_corpus_does_not_call_worker(tmp_path):
    store, _ = _ingest(tmp_path)
    service, worker = _service(tmp_path, store)
    result = service.ask(advisor(), "zzzz-not-indexed")
    assert result["items"] == []
    assert worker.payloads == []
    assert result["thread_id"]
    assert result["capability_receipt"]["scopes_used"] == [READ]
    store.close()


def test_cross_principal_thread_is_denied(tmp_path):
    store, _ = _ingest(tmp_path)
    service, worker = _service(tmp_path, store)
    first = service.ask(advisor(), "token-a")
    other = advisor(principal_id="advisor-02", family_id="fam-other")
    with pytest.raises(AuthError) as error:
        service.ask(other, "token-a", thread_id=first["thread_id"])
    assert error.value.status == 403
    assert worker.calls == 1
    store.close()


def test_cross_workspace_thread_is_denied(tmp_path):
    store, _ = _ingest(tmp_path)
    service, _ = _service(tmp_path, store)
    first = service.ask(advisor(), "token-a")
    with pytest.raises(AuthError) as error:
        service.ask(advisor(workspace_id="other-ws"), "token-a", thread_id=first["thread_id"])
    assert error.value.status == 403
    store.close()


def test_grant_downgrade_rebuilds_envelope_without_old_prose(tmp_path):
    store, _ = _ingest(tmp_path)
    service, worker = _service(tmp_path, store)
    worker.reply = f"owner-prose-contains-{SENTINEL}"
    first = service.ask(owner(), "secret-bridge")
    assert any(SENTINEL in json.dumps(item) for item in worker.payloads[0]["envelope"])
    worker.reply = "advisor-prose"
    second = service.ask(
        advisor(principal_id="owner-01", family_id="fam-owner"),
        "token-a",
        thread_id=first["thread_id"],
    )
    payload = worker.payloads[-1]
    blob = json.dumps(payload)
    assert SENTINEL not in blob
    assert "owner-prose-contains-" not in blob
    assert payload.get("prior_assistant_turns", []) == []
    assert "conversation_history" not in payload
    assert all(isinstance(item, str) for item in payload.get("prior_user_turns", []))
    assert SENTINEL not in json.dumps(second)
    store.close()


def test_changed_acl_drops_previously_visible_handle(tmp_path):
    store, _ = _ingest(tmp_path)
    service, worker = _service(tmp_path, store)
    first = service.ask(owner(), "token-b")
    hidden_handle = first["items"][0]["cited_handles"][0]
    service.ask(
        advisor(principal_id="owner-01", family_id="fam-owner"),
        "token-a",
        thread_id=first["thread_id"],
    )
    later_handles = {item["handle"] for item in worker.payloads[-1]["envelope"]}
    assert hidden_handle not in later_handles
    store.close()


def test_citations_are_intersected_with_current_envelope(tmp_path):
    store, _ = _ingest(tmp_path)
    worker = RecordingWorker(cited=["forged-handle", "also-fake"])
    service, _ = _service(tmp_path, store, worker=worker)
    result = service.ask(advisor(), "token-a")
    cited = result["items"][0]["cited_handles"]
    allowed = {item["handle"] for item in worker.payloads[0]["envelope"]}
    assert cited == []
    assert set(cited) <= allowed
    assert "forged-handle" not in cited
    store.close()


def test_empty_citations_are_not_filled_from_envelope(tmp_path):
    store, _ = _ingest(tmp_path)
    worker = RecordingWorker(cited=[])
    service, _ = _service(tmp_path, store, worker=worker)
    result = service.ask(advisor(), "token-a")
    assert result["items"][0]["cited_handles"] == []
    assert worker.payloads[0]["envelope"]
    store.close()


def test_valid_subset_of_citations_is_kept(tmp_path):
    store, _ = _ingest(tmp_path)
    captured = {}

    class CiteOne:
        def generate(self, payload):
            captured["envelope"] = payload["envelope"]
            handle = payload["envelope"][0]["handle"]
            return {"text": "ok", "cited_handles": [handle, "forged"]}

    service, _ = _service(tmp_path, store, worker=CiteOne())
    result = service.ask(advisor(), "token-a")
    assert result["items"][0]["cited_handles"] == [captured["envelope"][0]["handle"]]
    store.close()


def test_timeout_fails_closed_without_personal_hermes(tmp_path):
    import time

    store, _ = _ingest(tmp_path)
    worker = RecordingWorker(delay=0.4)
    service, _ = _service(tmp_path, store, worker=worker, budget=AskBudget(timeout_seconds=0.05, max_concurrency=1))
    started = time.monotonic()
    with pytest.raises(AskTimeout):
        service.ask(advisor(), "token-a")
    elapsed = time.monotonic() - started
    assert elapsed < 0.25
    source = inspect.getsource(AskService)
    assert "ThreadPoolExecutor" not in source
    assert "adapters.hermes" not in source
    assert "query_as_broker" not in source
    assert "HttpHermesClient" not in source
    store.close()


def test_missing_isolation_fails_closed(tmp_path):
    store, _ = _ingest(tmp_path)
    with pytest.raises(IsolationUnavailable):
        AskService(store=store, worker=RecordingWorker(), threads=MemoryThreadStore(), isolation=None)
    store.close()


def test_thread_retention_drops_oldest_user_turns(tmp_path):
    store, _ = _ingest(tmp_path)
    threads = MemoryThreadStore(max_turns=3)
    service, worker = _service(tmp_path, store, threads=threads)
    first = service.ask(advisor(), "token-a")
    for extra in ("token-notes", "token-c", "token-a"):
        service.ask(advisor(), extra, thread_id=first["thread_id"])
    prior = worker.payloads[-1]["prior_user_turns"]
    assert len(prior) <= 3
    assert prior[-1] == "token-a" or worker.payloads[-1]["query"] == "token-a"
    store.close()


def test_expired_thread_is_not_replayed(tmp_path):
    store, _ = _ingest(tmp_path)
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    state = {"t": now}

    def clock():
        return state["t"]

    service, _ = _service(
        tmp_path,
        store,
        threads=MemoryThreadStore(ttl_seconds=10),
        clock=clock,
        budget=AskBudget(timeout_seconds=2, max_concurrency=1, thread_ttl_seconds=10),
    )
    first = service.ask(advisor(), "token-a")
    state["t"] = now + timedelta(seconds=11)
    with pytest.raises(AuthError) as error:
        service.ask(advisor(), "token-notes", thread_id=first["thread_id"])
    assert error.value.status == 403
    store.close()


def test_concurrency_limit_fails_closed(tmp_path):
    store, _ = _ingest(tmp_path)
    started = []

    class BlockingWorker:
        def generate(self, payload):
            started.append(1)
            import time
            time.sleep(0.3)
            return {"text": "ok", "cited_handles": [payload["envelope"][0]["handle"]]}

    service = AskService(
        store=store,
        worker=BlockingWorker(),
        threads=MemoryThreadStore(),
        isolation=_isolation(tmp_path),
        budget=AskBudget(timeout_seconds=2, max_concurrency=1),
    )
    import threading

    errors = []

    def run():
        try:
            service.ask(advisor(), "token-a")
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    import time
    time.sleep(0.05)
    second.start()
    first.join()
    second.join()
    assert any(isinstance(item, (AskBusy, AskTimeout)) for item in errors)
    store.close()


def test_thread_store_bounds_and_sweeps_expired():
    from app.ask.threads import MemoryThreadStore

    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store = MemoryThreadStore(ttl_seconds=10, max_threads=3, max_threads_per_principal=2)
    first = store.create(principal_id="p1", workspace_id="w", now=now)
    store.create(principal_id="p1", workspace_id="w", now=now + timedelta(seconds=1))
    store.create(principal_id="p1", workspace_id="w", now=now + timedelta(seconds=2))
    owned = [row for row in store._rows.values() if row.principal_id == "p1"]
    assert len(owned) == 2
    assert first.thread_id not in store._rows
    store.create(principal_id="p2", workspace_id="w", now=now + timedelta(seconds=3))
    store.create(principal_id="p3", workspace_id="w", now=now + timedelta(seconds=4))
    assert len(store._rows) == 3
    later = now + timedelta(seconds=20)
    assert store.get(list(store._rows)[0], now=later) is None
    store.sweep(later)
    assert store._rows == {}


def test_same_thread_concurrent_update_is_rejected(tmp_path):
    import threading
    import time

    store, _ = _ingest(tmp_path)
    started = threading.Event()

    class BlockingWorker:
        def generate(self, payload):
            started.set()
            time.sleep(0.3)
            return {"text": "ok", "cited_handles": []}

    service = AskService(
        store=store,
        worker=BlockingWorker(),
        threads=MemoryThreadStore(),
        isolation=_isolation(tmp_path),
        budget=AskBudget(timeout_seconds=2, max_concurrency=2),
    )
    first = service.ask(advisor(), "token-a")
    errors = []

    def run():
        try:
            service.ask(advisor(), "token-notes", thread_id=first["thread_id"])
        except Exception as exc:
            errors.append(exc)

    a = threading.Thread(target=run)
    b = threading.Thread(target=run)
    a.start()
    started.wait(timeout=1)
    b.start()
    a.join()
    b.join()
    assert any(isinstance(item, AskBusy) for item in errors)
    store.close()


def test_ask_package_does_not_import_hermes_adapter():
    import app.ask.isolation as isolation
    import app.ask.router as router
    import app.ask.service as service
    import app.ask.threads as threads

    for module in (isolation, router, service, threads):
        source = inspect.getsource(module)
        assert "adapters.hermes" not in source
        assert "query_as_broker" not in source
        assert "message/send" not in source
