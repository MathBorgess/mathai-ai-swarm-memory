import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "stdio_harness.py"
TEE = ROOT / "tests" / "stdio_tee.py"


def _params(log_path: Path, leak: bool = False) -> StdioServerParameters:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["SWARM_MCP_STDOUT_LOG"] = str(log_path)
    env["SWARM_MCP_HARNESS"] = str(HARNESS)
    env["SWARM_MCP_HARNESS_ARGS"] = "--leak-error" if leak else ""
    return StdioServerParameters(command=sys.executable, args=[str(TEE)], env=env, cwd=str(ROOT))


@pytest.mark.anyio
async def test_stdio_process_initialize_list_call(tmp_path):
    log_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    params = _params(log_path)
    with err_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "mathai-swarm"
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"capabilities", "query", "resolve", "propose"} <= names
                result = await session.call_tool("capabilities", {})
                assert result.is_error is False
    raw = log_path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        assert message.get("jsonrpc") == "2.0"
    assert "access_" not in raw
    assert "refresh_" not in raw
    stderr = err_path.read_text(encoding="utf-8")
    assert "access_" not in stderr
    assert "refresh_" not in stderr
    assert "leak-" not in stderr


@pytest.mark.anyio
async def test_stdio_process_sanitizes_tool_errors(tmp_path):
    log_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    params = _params(log_path, leak=True)
    with err_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("query", {"query": "tcc"})
                assert result.is_error is True
                dumped = result.model_dump_json()
                assert "leak-access-token" not in dumped
                assert "leak-refresh-token" not in dumped
    combined = log_path.read_text(encoding="utf-8") + err_path.read_text(encoding="utf-8")
    assert "leak-access-token" not in combined
    assert "leak-refresh-token" not in combined
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert json.loads(line).get("jsonrpc") == "2.0"
