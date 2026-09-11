from __future__ import annotations

import pytest

from mathai_swarm_mcp.http import SwarmHttpClient
from mathai_swarm_mcp.keystore import MemoryStore
from tests.fake_broker import ORIGIN, FakeBroker


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_client(principal: str = "advisor-01", *, broker=None, store=None, operations=None):
    broker = broker or FakeBroker(operations=operations)
    store = store or MemoryStore()
    client = SwarmHttpClient(ORIGIN, principal, store=store, transport=broker.transport())
    return broker, store, client


def login(client: SwarmHttpClient) -> None:
    started = client.start_device()
    assert client.poll_device(started["device_code"]) == {"error": "authorization_pending"}
    assert client.poll_device(started["device_code"]) == {"error": "slow_down"}
    assert client.poll_device(started["device_code"]) == {"status": "authorized"}
