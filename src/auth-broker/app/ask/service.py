"""AskService.ask(principal, query, thread_id) — current envelope, isolated worker."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from app.ask.isolation import IsolationConfig, IsolationUnavailable, WorkerTimeout
from app.ask.threads import MemoryThreadStore, ThreadBusy, ThreadRecord
from app.context.query import AuthError, search, validate_principal


class AskTimeout(RuntimeError):
    status = 504


class AskBusy(RuntimeError):
    status = 429


@dataclass
class AskBudget:
    timeout_seconds: float = 30
    max_concurrency: int = 2
    max_output_chars: int = 8000
    envelope_limit: int = 10
    max_turns: int = 8
    thread_ttl_seconds: int = 3600
    max_query: int = 2000


class AskService:
    def __init__(
        self,
        *,
        store,
        worker,
        threads: MemoryThreadStore | None,
        isolation: IsolationConfig | None,
        budget: AskBudget | None = None,
        clock=None,
    ):
        if isolation is None or worker is None or store is None:
            raise IsolationUnavailable("Ask isolation is not configured")
        isolation.validate()
        self.store = store
        self.worker = worker
        self.threads = threads or MemoryThreadStore()
        self.isolation = isolation
        self.budget = budget or AskBudget()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._slots = threading.BoundedSemaphore(self.budget.max_concurrency)

    def ask(self, principal: Mapping, query: str, thread_id: str | None = None) -> dict:
        auth = validate_principal(principal)
        if not isinstance(query, str) or not query.strip():
            raise AuthError(400, "Invalid input")
        query = query.strip()
        if len(query) > self.budget.max_query:
            raise AuthError(413, "Request body too large")
        now = self.clock()
        record = self._thread(auth, thread_id, now)
        try:
            self.threads.acquire(record.thread_id)
        except ThreadBusy as error:
            raise AskBusy("Ask thread is busy") from error
        try:
            prior = list(record.user_turns)
            record.user_turns = (record.user_turns + [query])[-self.threads.max_turns :]
            record.last_used = now
            self.threads.save(record)
            envelope, receipt = self._envelope(auth, query, prior)
            if not envelope:
                return {"items": [], "capability_receipt": receipt, "thread_id": record.thread_id}
            payload = {
                "query": query,
                "prior_user_turns": prior,
                "envelope": envelope,
                "budget": {
                    "max_output_chars": self.budget.max_output_chars,
                    "timeout_seconds": self.budget.timeout_seconds,
                },
            }
            if not self._slots.acquire(blocking=False):
                raise AskBusy("Ask concurrency limit")
            try:
                result = self._call_worker(payload)
            finally:
                self._slots.release()
            return {
                "items": [self._answer(result, envelope)],
                "capability_receipt": receipt,
                "thread_id": record.thread_id,
            }
        finally:
            self.threads.release(record.thread_id)

    def _thread(self, auth: dict, thread_id: str | None, now: datetime) -> ThreadRecord:
        if thread_id is None or thread_id == "":
            return self.threads.create(
                principal_id=auth["principal_id"], workspace_id=auth["workspace_id"], now=now
            )
        record = self.threads.get(thread_id, now=now)
        if (
            record is None
            or record.principal_id != auth["principal_id"]
            or record.workspace_id != auth["workspace_id"]
        ):
            raise AuthError(403, "Thread not available")
        return record

    def _envelope(self, auth: dict, query: str, prior: list[str]) -> tuple[list[dict], dict]:
        current = search(self.store, auth, query, self.budget.envelope_limit)
        by_handle = {item["handle"]: item for item in current["items"]}
        for text in prior:
            extra = search(self.store, auth, text, self.budget.envelope_limit)
            for item in extra["items"]:
                by_handle.setdefault(item["handle"], item)
        items = list(by_handle.values())[: self.budget.envelope_limit]
        return items, current["capability_receipt"]

    def _call_worker(self, payload: dict) -> dict:
        box: dict = {}
        done = threading.Event()
        factory = getattr(self.worker, "invocation", None)
        handle = factory() if callable(factory) else self.worker

        def run() -> None:
            try:
                box["result"] = handle.generate(payload)
            except BaseException as error:
                box["error"] = error
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(timeout=self.budget.timeout_seconds):
            abort = getattr(handle, "abort", None)
            if callable(abort):
                abort()
            raise AskTimeout("Ask worker timed out")
        error = box.get("error")
        if isinstance(error, WorkerTimeout):
            raise AskTimeout("Ask worker timed out") from error
        if isinstance(error, AskTimeout):
            raise error
        if isinstance(error, IsolationUnavailable):
            raise IsolationUnavailable("Ask worker failed closed") from None
        if error is not None:
            raise error
        result = box.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise IsolationUnavailable("Ask worker returned invalid output")
        return result

    def _answer(self, result: dict, envelope: list[dict]) -> dict:
        allowed = {item["handle"]: item for item in envelope}
        raw = result.get("cited_handles")
        cited = []
        if isinstance(raw, list):
            for handle in raw:
                if isinstance(handle, str) and handle in allowed and handle not in cited:
                    cited.append(handle)
        text = result["text"][: self.budget.max_output_chars]
        revision = allowed[cited[0]]["source_revision"] if cited else envelope[0]["source_revision"]
        return {"text": text, "cited_handles": cited, "source_revision": revision}
