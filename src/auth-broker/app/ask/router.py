"""HTTP factory for POST /v1/context/ask. Auth mapping is injected; wiring is not."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.ask.isolation import IsolationUnavailable
from app.ask.service import AskBudget, AskBusy, AskService, AskTimeout
from app.ask.threads import MemoryThreadStore, ThreadBusy
from app.context.query import AuthError, validate_principal

MAX_BODY = 16384
MAX_QUERY = 2000


def build_router(
    *,
    authorize,
    store,
    worker=None,
    isolation=None,
    threads: MemoryThreadStore | None = None,
    budget: AskBudget | None = None,
    clock=None,
):
    router = APIRouter()
    threads = threads or MemoryThreadStore()
    budget = budget or AskBudget()
    clock = clock or (lambda: datetime.now(timezone.utc))
    service = None
    if authorize is not None and store is not None and worker is not None and isolation is not None:
        try:
            service = AskService(
                store=store, worker=worker, threads=threads, isolation=isolation, budget=budget, clock=clock
            )
        except IsolationUnavailable:
            service = None

    async def read_body(request: Request) -> bytes:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Expected application/json")
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_BODY:
                raise HTTPException(413, "Request body too large")
        return bytes(payload)

    @router.post("/v1/context/ask")
    def ask(request: Request, body: bytes = Depends(read_body)):
        if service is None or authorize is None or store is None:
            raise HTTPException(503, "Ask generator unavailable")
        try:
            mapping = authorize(request)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid credential") from None
        try:
            principal = validate_principal(mapping)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
        payload = _object(body)
        extra = set(payload) - {"query", "thread_id"}
        if extra or "query" not in payload:
            raise HTTPException(400, "Invalid input")
        query = payload["query"]
        if not isinstance(query, str) or not query.strip():
            raise HTTPException(400, "Invalid input")
        if len(query) > MAX_QUERY:
            raise HTTPException(413, "Request body too large")
        thread_id = payload.get("thread_id")
        if thread_id is not None and not isinstance(thread_id, str):
            raise HTTPException(400, "Invalid input")
        try:
            return service.ask(principal, query, thread_id)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
        except (AskBusy, ThreadBusy) as error:
            raise HTTPException(getattr(error, "status", 429), "Ask concurrency limit") from None
        except AskTimeout as error:
            raise HTTPException(error.status, "Ask worker timed out") from None
        except IsolationUnavailable:
            raise HTTPException(503, "Ask generator unavailable") from None

    return router


def _object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "Invalid input")
    return value
