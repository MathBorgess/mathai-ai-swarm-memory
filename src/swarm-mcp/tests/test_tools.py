import json

import pytest
from mcp import Client
from mcp.types import TextContent

from mathai_swarm_mcp.errors import redact
from mathai_swarm_mcp.server import build_server
from tests.conftest import login, make_client


def _payload(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    for block in result.content:
        if isinstance(block, TextContent):
            data = json.loads(block.text)
            if isinstance(data, dict):
                return data
    raise AssertionError(result)


@pytest.mark.anyio
async def test_tools_query_resolve_propose_and_prompt():
    broker, store, client = make_client()
    login(client)
    mcp = build_server(client)
    async with Client(mcp) as session:
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == {"capabilities", "query", "resolve", "propose"}
        caps = await session.call_tool("capabilities", {})
        assert caps.is_error is False
        assert "query" in _payload(caps)["operations"]
        queried = await session.call_tool("query", {"query": "tcc", "limit": 5})
        assert queried.is_error is False
        assert _payload(queried)["capability_receipt"]["pass_as"] == "handle"
        resolved = await session.call_tool("resolve", {"handles": ["opaque-test-1"]})
        assert _payload(resolved)["items"][0]["handle"] == "opaque-test-1"
        proposed = await session.call_tool(
            "propose",
            {
                "namespace": "pesquisa.tcc",
                "title": "nota",
                "body_markdown": "corpo",
                "sources": [{"url": "https://example.com", "label": "ex"}],
                "idempotency_key": "k1",
            },
        )
        assert _payload(proposed)["status"] == "created"
        prompts = await session.list_prompts()
        assert any(prompt.name == "load_authorized_slice" for prompt in prompts.prompts)


@pytest.mark.anyio
async def test_tool_error_is_sanitized():
    broker, store, client = make_client()
    login(client)
    broker.secret_in_query_error = True
    mcp = build_server(client)
    async with Client(mcp) as session:
        result = await session.call_tool("query", {"query": "tcc"})
        assert result.is_error is True
        text = json.dumps(result.model_dump())
        assert "leak-access-token" not in text
        assert "leak-refresh-token" not in redact(text)
