"""Real stdio process entry used by tests. Injects the fake broker; not a production flag."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_broker import ORIGIN, FakeBroker  # noqa: E402
from mathai_swarm_mcp.dpop import generate_private_jwk, jwk_thumbprint  # noqa: E402
from mathai_swarm_mcp.http import SwarmHttpClient  # noqa: E402
from mathai_swarm_mcp.keystore import MemoryStore, StoredPrincipal  # noqa: E402
from mathai_swarm_mcp.server import run_stdio  # noqa: E402

PRINCIPAL = "stdio-principal"


def main() -> None:
    broker = FakeBroker()
    store = MemoryStore()
    private_jwk = generate_private_jwk()
    jkt = jwk_thumbprint(private_jwk)
    tokens = broker._issue(PRINCIPAL, jkt, ["ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"])
    store.save(
        StoredPrincipal(
            origin=ORIGIN,
            principal_id=PRINCIPAL,
            private_jwk=private_jwk,
            refresh_token=tokens["refresh_token"],
            scope=tokens["scope"],
        )
    )
    if "--leak-error" in sys.argv:
        broker.secret_in_query_error = True
    client = SwarmHttpClient(ORIGIN, PRINCIPAL, store=store, transport=broker.transport())
    run_stdio(client)


if __name__ == "__main__":
    main()
