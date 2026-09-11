import threading
from urllib.parse import urlsplit

import httpx
import jwt
import pytest

from mathai_swarm_mcp.dpop import generate_private_jwk, jwk_thumbprint
from mathai_swarm_mcp.errors import (
    DependencyUnavailable,
    ForbiddenError,
    LoginRequiredError,
    OriginError,
    RedirectRejected,
    SwarmMcpError,
)
from mathai_swarm_mcp.http import SwarmHttpClient
from mathai_swarm_mcp.keystore import MemoryStore
from tests.conftest import login, make_client
from tests.fake_broker import ORIGIN


def test_device_flow_pending_slow_down_then_tokens():
    broker, store, client = make_client()
    login(client)
    record = store.load(ORIGIN, "advisor-01")
    assert record is not None and record.refresh_token.startswith("refresh_")
    assert client._access is not None and client._access.value.startswith("access_")
    assert client._access.token_type == "DPoP"


def test_refresh_rotates_and_is_single_flight():
    broker, store, client = make_client()
    login(client)
    old_refresh = store.load(ORIGIN, "advisor-01").refresh_token
    broker.expire_access()
    client._access.expires_at = client._clock()
    errors = []

    def worker():
        try:
            client.query("tcc")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert broker.refresh_calls == 1
    assert store.load(ORIGIN, "advisor-01").refresh_token != old_refresh


def test_revocation_clears_local_refresh():
    broker, store, client = make_client()
    login(client)
    client.logout()
    assert store.load(ORIGIN, "advisor-01") is None
    with pytest.raises(LoginRequiredError):
        client.query("tcc")


def test_redirect_is_not_followed():
    broker, store, client = make_client()
    login(client)
    broker.redirect_paths.add("/v1/context/query")
    with pytest.raises(RedirectRejected):
        client.query("tcc")
    hosts = {urlsplit(str(req.url)).hostname for req in broker.requests}
    assert hosts == {"a2a.mathai.com.br"}


def test_wrong_origin_never_leaves_allowlist():
    broker, store, client = make_client()
    login(client)
    client.query("tcc")
    hosts = {urlsplit(str(req.url)).hostname for req in broker.requests}
    assert hosts == {"a2a.mathai.com.br"}
    schemes = {urlsplit(str(req.url)).scheme for req in broker.requests}
    assert schemes == {"https"}


def test_bad_github_consent_url_aborts_login():
    broker, store, client = make_client()
    broker.verification_uri = "https://evil.example/login/device"
    with pytest.raises(OriginError):
        client.start_device()


def test_dpop_key_binding_rejects_swapped_key():
    broker, store, client = make_client()
    login(client)
    record = store.load(ORIGIN, "advisor-01")
    record.private_jwk = generate_private_jwk()
    store.save(record)
    client._access = None
    with pytest.raises(LoginRequiredError):
        client.query("tcc")
    assert broker.refresh_calls == 1


def test_401_refreshes_once_403_does_not():
    broker, store, client = make_client()
    login(client)
    broker.expire_access()
    envelope = client.query("tcc")
    assert envelope["items"][0]["handle"] == "opaque-test-1"
    assert broker.refresh_calls == 1
    broker.forbidden = True
    before = broker.refresh_calls
    with pytest.raises(ForbiddenError):
        client.query("again")
    assert broker.refresh_calls == before


def test_propose_keeps_idempotency_key_on_401_retry():
    broker, store, client = make_client()
    login(client)
    broker.expire_access()
    payload = {
        "namespace": "pesquisa.tcc",
        "title": "nota",
        "body_markdown": "corpo",
        "sources": [{"url": "https://example.com", "label": "ex"}],
    }
    result = client.propose(payload, "idem-1")
    assert result["proposal_id"] == "prop-test-1"
    keys = [req.headers.get("Idempotency-Key") for req in broker.requests if urlsplit(str(req.url)).path == "/v1/context/propose"]
    assert keys and all(key == "idem-1" for key in keys)


def test_missing_operation_is_explicit():
    broker, store, client = make_client(operations=["capabilities"])
    login(client)
    with pytest.raises(DependencyUnavailable, match="query"):
        client.query("tcc")


def test_refresh_timeout_does_not_reuse_old_refresh():
    broker, store, client = make_client()
    login(client)
    inner = broker.transport()

    class TimeoutRefresh(httpx.BaseTransport):
        def handle_request(self, request):
            if urlsplit(str(request.url)).path == "/v1/oauth/token":
                raise httpx.TimeoutException("simulated")
            return inner.handle_request(request)

    client._http = httpx.Client(transport=TimeoutRefresh(), timeout=30, follow_redirects=False, trust_env=False)
    old = store.load(ORIGIN, "advisor-01").refresh_token
    assert old
    client._access.expires_at = client._clock()
    with pytest.raises(LoginRequiredError, match="must not be reused"):
        client.query("tcc")
    assert store.load(ORIGIN, "advisor-01").refresh_token is None
    token_requests = [req for req in broker.requests if urlsplit(str(req.url)).path == "/v1/oauth/token"]
    with pytest.raises(LoginRequiredError, match="must not be reused"):
        client.query("tcc")
    assert [req for req in broker.requests if urlsplit(str(req.url)).path == "/v1/oauth/token"] == token_requests


def test_errors_redact_secrets():
    broker, store, client = make_client()
    login(client)
    broker.secret_in_query_error = True
    with pytest.raises(SwarmMcpError) as exc:
        client.query("tcc")
    message = str(exc.value)
    assert "leak-access-token" not in message
    assert "leak-refresh-token" not in message
    assert "refresh_" not in message


def test_dpop_proofs_are_unique_and_bind_key():
    broker, store, client = make_client()
    login(client)
    client.query("tcc")
    proofs = [req.headers.get("DPoP") for req in broker.requests]
    assert len(proofs) == len(set(proofs))
    header = jwt.get_unverified_header(proofs[0])
    assert header["jwk"] and "d" not in header["jwk"]
    assert jwk_thumbprint(header["jwk"]) == jwk_thumbprint(store.load(ORIGIN, "advisor-01").private_jwk)


def test_discovery_does_not_change_origin():
    _, _, client = make_client()
    assert client.origin == ORIGIN
    # Adapter never fetches an agent card or relocates the trusted host.
    assert not hasattr(client, "discover")
