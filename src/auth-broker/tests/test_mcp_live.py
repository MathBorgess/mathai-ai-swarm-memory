"""Real MCP SDK client against create_app + uvicorn. Fake GitHub/model transports only."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from fastapi import HTTPException

from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from app.ask import (
    AskBusy,
    AskService,
    AskTimeout,
    IsolationConfig,
    IsolationUnavailable,
    MemoryThreadStore,
    ThreadBusy,
    worker_root,
)
from app.ask import build_router as build_ask_router
from app.context import ContextStore, ingest_manifest
from app.context import build_router as build_context_router
from app.context.query import AuthError, resolve_handles, search, validate_principal
from app.proposals import ProposalStore, submit_proposal, build_router as build_proposal_router
from app.proposals.schema import ProposalPayloadError, ProposalTooLarge, parse_proposal
from swarm_helpers import FakeHermes, es256_material
from test_ask import RecordingWorker
from test_context_manifest import abc_entries, abc_pages, manifest, write_pages
from test_mcp_oauth import READ, PROPOSE, FakeGitHub, csrf_token, pkce_pair
from test_proposal_router import REPO, FakeGitHub as ProposalGitHub
from test_remote_mcp import _payload

NOW = datetime.now(timezone.utc).replace(microsecond=0)
WORKSPACE = "personal"


def _mcp_query(store):
    def query(principal, query, limit=10):
        try:
            return search(store, validate_principal(principal), query, limit)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
    return query


def _mcp_resolve(store):
    def resolve(principal, handles):
        try:
            return resolve_handles(store, validate_principal(principal), handles)
        except AuthError as error:
            raise HTTPException(error.status, error.detail) from None
    return resolve


def _mcp_propose(store, github):
    def propose(principal, namespace, title, body_markdown, sources, *, idempotency_key):
        try:
            payload = parse_proposal({
                "namespace": namespace,
                "title": title,
                "body_markdown": body_markdown,
                "sources": sources,
            })
        except ProposalTooLarge:
            raise HTTPException(413, "Proposal markdown too large") from None
        except ProposalPayloadError:
            raise HTTPException(400, "Invalid proposal") from None
        return submit_proposal(
            store=store, github=github, principal=principal, payload=payload,
            idempotency_key=idempotency_key, now=datetime.now(timezone.utc),
        )
    return propose


def _mcp_ask(service):
    def ask(principal, query, thread_id=None):
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
    return ask


def _isolation(tmp_path):
    inference = tmp_path / "ask-inference.json"
    inference.write_text('{"transport":"stub","model":"stub"}', encoding="utf-8")
    root = worker_root()
    return IsolationConfig(
        image="mathai-ask-worker:test",
        launch_script=root / "launch.sh",
        worker_script=root / "worker_main.py",
        style_path=root / "style" / "SOUL.md",
        docker_bin=tmp_path / "docker-not-used",
        inference_config=inference,
        network="none",
    )


class _Bound:
    def __init__(self, app, sock):
        self._sock = sock
        self.port = sock.getsockname()[1]
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


def _context(tmp_path):
    root = tmp_path / "content"
    write_pages(root, abc_pages())
    store = ContextStore(tmp_path / "context.sqlite3")
    ingest_manifest(store, manifest(abc_entries()), root)
    return store


def _app(tmp_path, public_url, github):
    _, pem, jwk, _ = es256_material()
    path = tmp_path / "broker.sqlite3"
    context = _context(tmp_path)
    proposals = ProposalStore(tmp_path / "proposals.sqlite3")
    writer = ProposalGitHub()
    worker = RecordingWorker()
    isolation = _isolation(tmp_path)
    threads = MemoryThreadStore()
    ask_service = AskService(
        store=context, worker=worker, threads=threads, isolation=isolation,
    )
    with closing(SqlitePairingStore(path)) as store:
        store.add_principal("github:1001", github_subject="1001", jwk=jwk, role="advisor", now=NOW)
        store.set_grant("github:1001", READ, NOW, NOW + timedelta(days=30))
        store.set_grant("github:1001", PROPOSE, NOW, NOW + timedelta(days=30))
        store.add_principal("github:2002", github_subject="2002", jwk=jwk, role="advisor", now=NOW)
        store.set_grant("github:2002", READ, NOW, NOW + timedelta(days=30))
    app = create_app(
        database_path=path,
        audience=f"{public_url}/a2a",
        owner_verifier=object(),
        hermes=FakeHermes(),
        clock=lambda: datetime.now(timezone.utc),
        github_oauth=None,
        token_signing_key=pem,
        workspace_id=WORKSPACE,
        public_url=public_url,
        mcp_github=github,
        context_router_factory=lambda *, authorize: build_context_router(authorize=authorize, store=context),
        proposal_router_factory=lambda *, authorize: build_proposal_router(
            authorize=authorize, store=proposals, github=writer, repository=REPO,
        ),
        ask_router_factory=lambda *, authorize: build_ask_router(
            authorize=authorize, store=context, worker=worker, isolation=isolation,
            threads=threads,
        ),
        mcp_query=_mcp_query(context),
        mcp_resolve=_mcp_resolve(context),
        mcp_propose=_mcp_propose(proposals, writer),
        mcp_ask=_mcp_ask(ask_service),
    )
    return app, path, context, worker


def _oauth_tokens(base: str, github: FakeGitHub, *, subject: str, scope: str, redirect: str):
    github.subject = subject
    with httpx.Client(base_url=base, follow_redirects=False, timeout=10) as client:
        registered = client.post(
            "/mcp/oauth/register",
            json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none", "client_name": subject},
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        verifier, challenge = pkce_pair()
        page = client.get(
            "/mcp/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"{base}/mcp",
                "state": "st",
                "scope": scope,
            },
        )
        assert page.status_code == 200, page.text
        consent = client.post("/mcp/oauth/consent", data={"csrf": csrf_token(page.text), "decision": "allow"})
        assert consent.status_code == 302, consent.text
        state = parse_qs(urlparse(consent.headers["location"]).query)["state"][0]
        callback = client.get("/mcp/oauth/callback", params={"code": "ghs-test", "state": state})
        assert callback.status_code == 302, callback.text
        code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
        tokens = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code": code,
                "code_verifier": verifier,
                "resource": f"{base}/mcp",
            },
        )
        assert tokens.status_code == 200, tokens.text
        return client_id, tokens.json()


@asynccontextmanager
async def _session(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                yield session


@pytest.mark.anyio
async def test_sdk_client_uses_real_oauth_and_graph(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    public = f"http://127.0.0.1:{port}"
    github = FakeGitHub("1001")
    app, path, context, worker = _app(tmp_path, public, github)
    try:
        with _Bound(app, sock) as server:
            denied = httpx.post(
                server.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            )
            assert denied.status_code == 401
            ana_client, ana = _oauth_tokens(
                public, github, subject="1001", scope=f"{READ} {PROPOSE}", redirect=f"{public}/cb-ana",
            )
            _, bob = _oauth_tokens(public, github, subject="2002", scope=READ, redirect=f"{public}/cb-bob")
            async with _session(server.url, ana["access_token"]) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {"ask", "capabilities", "query", "resolve", "propose"}
                listed = _payload(await session.call_tool("capabilities", {}))
                assert set(listed["operations"]) == {"ask", "query", "resolve", "propose"}
                queried = _payload(await session.call_tool("query", {"query": "token-a"}))
                assert queried["items"]
                assert all("token-b" not in item.get("text", "") for item in queried["items"])
                hidden = _payload(await session.call_tool("query", {"query": "token-b"}))
                assert hidden["items"] == []
                empty = _payload(await session.call_tool("ask", {"query": "zzzz-not-indexed"}))
                assert empty["items"] == []
                assert worker.payloads == []
                asked = _payload(await session.call_tool("ask", {"query": "token-a"}))
                assert asked["items"]
                blob = json.dumps(worker.payloads)
                assert "token-b" not in blob
                assert "SENTINEL" not in blob
                assert worker.payloads[0].get("credentials") is None
                proposed = _payload(
                    await session.call_tool(
                        "propose",
                        {
                            "namespace": "pesquisa.tcc",
                            "title": "Nota",
                            "body_markdown": "Afirmação.",
                            "sources": [{"url": "https://example.test/p", "label": "paper"}],
                            "idempotency_key": "idem-live-1",
                        },
                    )
                )
                assert proposed["status"] == "created"
                replay = _payload(
                    await session.call_tool(
                        "propose",
                        {
                            "namespace": "pesquisa.tcc",
                            "title": "Nota",
                            "body_markdown": "Afirmação.",
                            "sources": [{"url": "https://example.test/p", "label": "paper"}],
                            "idempotency_key": "idem-live-1",
                        },
                    )
                )
                assert replay["proposal_id"] == proposed["proposal_id"]
            async with _session(server.url, bob["access_token"]) as session:
                await session.initialize()
                queried = _payload(await session.call_tool("query", {"query": "token-a"}))
                assert queried["capability_receipt"]["principal_id"] == "github:2002"
                denied_propose = await session.call_tool(
                    "propose",
                    {
                        "namespace": "pesquisa.tcc",
                        "title": "Nota",
                        "body_markdown": "Afirmação.",
                        "sources": [{"url": "https://example.test/p", "label": "paper"}],
                        "idempotency_key": "idem-bob",
                    },
                )
                assert denied_propose.is_error is True
            with closing(SqlitePairingStore(path)) as store:
                store.revoke_grant("github:1001", READ, datetime.now(timezone.utc))
            async with _session(server.url, ana["access_token"]) as session:
                await session.initialize()
                downgraded = await session.call_tool("query", {"query": "token-a"})
                assert downgraded.is_error is True
            rotated = httpx.post(
                f"{public}/mcp/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": ana_client,
                    "refresh_token": ana["refresh_token"],
                    "resource": f"{public}/mcp",
                },
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["scope"] == PROPOSE
            async with _session(server.url, rotated.json()["access_token"]) as session:
                await session.initialize()
                still_denied = await session.call_tool("query", {"query": "token-a"})
                assert still_denied.is_error is True
    finally:
        context.close()
