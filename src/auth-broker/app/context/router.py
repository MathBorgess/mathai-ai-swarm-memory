"""HTTP factory for POST /v1/context/query and /v1/context/resolve.

`authorize(request)` is injected by Agent A's wiring. This module never reads
principal, workspace, scopes or classifications from the JSON body. Missing
store/authorize is 503, not a Hermes fallback.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.context.query import AuthError, resolve_handles, search, validate_principal

MAX_BODY = 16384
MAX_QUERY = 2000
MAX_HANDLES = 50
DEFAULT_LIMIT = 10


def build_router(*, authorize, store):
    router = APIRouter()

    async def read_body(request: Request) -> bytes:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Expected application/json")
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_BODY:
                raise HTTPException(413, "Request body too large")
        return bytes(payload)

    def principal(request: Request) -> dict:
        if authorize is None or store is None or not callable(authorize):
            raise HTTPException(503, "Context store unavailable")
        try:
            mapping = authorize(request)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid credential") from None
        try:
            return validate_principal(mapping)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None

    @router.post("/v1/context/query")
    def query(request: Request, body: bytes = Depends(read_body)):
        auth = principal(request)
        payload = _object(body)
        if set(payload) - {"query", "limit"} or "query" not in payload:
            raise HTTPException(400, "Invalid input")
        query_text = payload["query"]
        if not isinstance(query_text, str) or not query_text.strip():
            raise HTTPException(400, "Invalid input")
        if len(query_text) > MAX_QUERY:
            raise HTTPException(413, "Request body too large")
        limit = payload.get("limit", DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise HTTPException(400, "Invalid input")
        try:
            return search(store, auth, query_text, limit)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None

    @router.post("/v1/context/resolve")
    def resolve(request: Request, body: bytes = Depends(read_body)):
        auth = principal(request)
        payload = _object(body)
        if set(payload) != {"handles"}:
            raise HTTPException(400, "Invalid input")
        handles = payload["handles"]
        if not isinstance(handles, list) or any(not isinstance(item, str) or not item for item in handles):
            raise HTTPException(400, "Invalid input")
        if len(handles) > MAX_HANDLES:
            raise HTTPException(413, "Request body too large")
        try:
            return resolve_handles(store, auth, handles)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None

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
