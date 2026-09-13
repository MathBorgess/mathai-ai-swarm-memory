"""Live MCP SDK client against Streamable HTTP on local uvicorn.

Fake authorize/handlers prove the transport, not OAuth, ask isolation, or production.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from app.remote_mcp import attach_mcp, build_remote_mcp


def _principal(principal_id: str) -> dict:
    return {
        "principal_id": principal_id,
        "workspace_id": "personal",
        "scopes": ("ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"),
        "classifications": ("public", "shared"),
        "family_id": f"fam-{principal_id}",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }


def _authorize(request):
    header = request.headers.get("authorization", "")
    if header == "Bearer ana":
        return _principal("github:ana")
    if header == "Bearer bob":
        return _principal("github:bob")
    raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def _query(principal, query, limit=10):
    time.sleep(0.05)
    return {"seen": principal["principal_id"], "query": query, "limit": limit}


def _resolve(principal, handles):
    return {"seen": principal["principal_id"], "handles": handles}


def _ask(principal, query, thread_id=None):
    return {"seen": principal["principal_id"], "query": query, "thread_id": thread_id}


def _propose(principal, namespace, title, body_markdown, sources, *, idempotency_key):
    return {
        "seen": principal["principal_id"],
        "namespace": namespace,
        "title": title,
        "sources": sources,
        "idempotency_key": idempotency_key,
        "body_len": len(body_markdown),
    }


def _all_ops():
    return dict(query=_query, resolve=_resolve, ask=_ask, propose=_propose)


class _Uvicorn:
    def __init__(self, app):
        self._app = app
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self.port = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        config = uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.run(self._server.serve(sockets=[self._sock]))

    def __enter__(self):
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            time.sleep(0.025)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._sock.close()


def _fastapi(remote):
    app = FastAPI(lifespan=remote.lifespan, redirect_slashes=False)
    attach_mcp(app, remote)
    return app


def _payload(result):
    assert result.is_error is False, result.content[0].text if result.content else result
    text = result.content[0].text
    return json.loads(text)


@asynccontextmanager
async def _session(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                yield session


@pytest.fixture
def full_server():
    remote = build_remote_mcp(authorize=_authorize, **_all_ops())
    with _Uvicorn(_fastapi(remote)) as server:
        yield server


@pytest.mark.anyio
async def test_initialize_list_call_over_sdk_client(full_server):
    async with _session(full_server.url, "ana") as session:
        init = await session.initialize()
        assert init.server_info.name == "mathai-swarm"
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == {"capabilities", "query", "resolve", "ask", "propose"}
        listed = _payload(await session.call_tool("capabilities", {}))
        assert set(listed["operations"]) == {"ask", "query", "propose", "resolve"}
        queried = _payload(await session.call_tool("query", {"query": "tcc"}))
        assert queried["seen"] == "github:ana"
        assert queried["limit"] == 10
        resolved = _payload(await session.call_tool("resolve", {"handles": ["h1"]}))
        assert resolved["handles"] == ["h1"]
        asked = _payload(await session.call_tool("ask", {"query": "resumo", "thread_id": "thr-1"}))
        assert asked["thread_id"] == "thr-1"
        proposed = _payload(
            await session.call_tool(
                "propose",
                {
                    "namespace": "pesquisa.tcc",
                    "title": "Nota",
                    "body_markdown": "Afirmação.",
                    "sources": [{"url": "https://example.test/p", "label": "paper"}],
                    "idempotency_key": "idem-1",
                },
            )
        )
        assert proposed["idempotency_key"] == "idem-1"
        assert proposed["seen"] == "github:ana"


@pytest.mark.anyio
async def test_unauthenticated_http_never_creates_mcp_session(full_server):
    async with httpx2.AsyncClient() as client:
        response = await client.post(
            full_server.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
    assert response.status_code == 401
    assert "mcp-session-id" not in {k.lower() for k in response.headers}
    assert "invalid_token" in response.text


@pytest.mark.anyio
async def test_sdk_client_without_bearer_cannot_initialize(full_server):
    with pytest.raises(Exception):
        async with create_mcp_http_client() as http:
            async with streamable_http_client(full_server.url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()


@pytest.mark.anyio
async def test_invalid_arguments_and_caller_principal_are_rejected(full_server):
    async with _session(full_server.url, "ana") as session:
        await session.initialize()
        empty = await session.call_tool("query", {"query": ""})
        assert empty.is_error is True
        extra = await session.call_tool(
            "query",
            {"query": "tcc", "principal_id": "github:attacker", "workspace_id": "other"},
        )
        assert extra.is_error is True
        missing_key = await session.call_tool(
            "propose",
            {
                "namespace": "pesquisa.tcc",
                "title": "Nota",
                "body_markdown": "Afirmação.",
                "sources": [],
            },
        )
        assert missing_key.is_error is True
        too_long = await session.call_tool("query", {"query": "x" * 2001})
        assert too_long.is_error is True


@pytest.mark.anyio
async def test_unsupported_tool_is_error(full_server):
    async with _session(full_server.url, "ana") as session:
        await session.initialize()
        result = await session.call_tool("shell", {"cmd": "id"})
        assert result.is_error is True


@pytest.mark.anyio
async def test_missing_backend_tools_are_not_advertised():
    remote = build_remote_mcp(authorize=_authorize, query=_query)
    with _Uvicorn(_fastapi(remote)) as server:
        async with _session(server.url, "ana") as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"capabilities", "query"}
            listed = _payload(await session.call_tool("capabilities", {}))
            assert listed["operations"] == ["query"]
            missing = await session.call_tool("ask", {"query": "tcc"})
            assert missing.is_error is True
            queried = _payload(await session.call_tool("query", {"query": "tcc"}))
            assert queried["seen"] == "github:ana"


@pytest.mark.anyio
async def test_two_identities_do_not_leak_principal(full_server):
    async def once(token: str, expected: str) -> str:
        async with _session(full_server.url, token) as session:
            await session.initialize()
            payload = _payload(await session.call_tool("query", {"query": "tcc"}))
            assert payload["seen"] == expected
            return payload["seen"]

    seen = await asyncio.gather(once("ana", "github:ana"), once("bob", "github:bob"))
    assert seen == ["github:ana", "github:bob"]


@pytest.mark.anyio
async def test_standalone_app_serves_exact_mcp_path():
    remote = build_remote_mcp(authorize=_authorize, query=_query)
    with _Uvicorn(remote.app) as server:
        async with _session(server.url, "ana") as session:
            await session.initialize()
            payload = _payload(await session.call_tool("query", {"query": "tcc"}))
            assert payload["seen"] == "github:ana"


def test_transport_does_not_import_oauth_or_ask():
    import app.remote_mcp as pkg
    import app.remote_mcp.server as server

    for module in (pkg, server):
        source = inspect.getsource(module)
        assert "mcp_oauth" not in source
        assert "app.ask" not in source
        assert "adapters.hermes" not in source
        assert "adapters.sqlite" not in source
        assert "app.api" not in source
        assert "app.cli" not in source
