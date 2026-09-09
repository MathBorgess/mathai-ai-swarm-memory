# Agent Pairing Broker v0.0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a VPS-hosted broker that admits an owner-approved agent and proxies one authenticated context request to Hermes without distributing Hermes's static bearer.

**Architecture:** A Python HTTP service owns pairing state in SQLite. An agent proves possession of an Ed25519 key over a broker challenge; the owner approves that key through a Cloudflare Access-protected UI. The broker exchanges that admission for its separate upstream Hermes credential.

**Tech Stack:** Python 3.12, FastAPI, `cryptography`, SQLite, `pytest`, Cloudflare Tunnel and Access.

**Spec:** `wiki/architecture/0001-agent-pairing-broker-v001.md`

## Global Constraints

- The database contains public keys, challenge hashes, statuses, expirations, revocations and audit events only.
- The broker signing key, Cloudflare Access configuration and Hermes bearer are environment secrets on the VPS and are never committed.
- Every caller token is audience-bound to the broker and is never forwarded to Hermes.
- V0.0.1 implements only the `owner-agent` profile; no document, project or tool scopes exist.
- GitHub, Google Drive and Cloudflare MCP sessions are never accepted as broker credentials.

---

### Task 1: Pairing domain and challenge lifecycle

**Files:**
- Create: `src/auth-broker/pyproject.toml`
- Create: `src/auth-broker/app/domain/pairing.py`
- Create: `src/auth-broker/tests/test_pairing.py`

**Interfaces:**
- Produces: `create_request(public_key: str, now: datetime) -> PairingRequest`
- Produces: `verify_proof(request: PairingRequest, signature: bytes, now: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_pairing_request_expires_after_ten_minutes():
    request = create_request(PUBLIC_KEY, now=NOW)
    with pytest.raises(PairingExpired):
        verify_proof(request, SIGNATURE, now=NOW + timedelta(minutes=10, seconds=1))
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `pytest src/auth-broker/tests/test_pairing.py -q`

Expected: FAIL because `create_request` is not defined.

- [ ] **Step 3: Implement the minimal challenge lifecycle**

```python
def create_request(public_key: str, now: datetime) -> PairingRequest:
    return PairingRequest(public_key=public_key, expires_at=now + timedelta(minutes=10))
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `pytest src/auth-broker/tests/test_pairing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth-broker
git commit -m "feat: add pairing challenge lifecycle"
```

### Task 2: SQLite persistence and revocation

**Files:**
- Create: `src/auth-broker/app/adapters/sqlite.py`
- Create: `src/auth-broker/tests/test_sqlite.py`

**Interfaces:**
- Consumes: `PairingRequest`
- Produces: `SqlitePairingStore.create(request: PairingRequest) -> None`
- Produces: `SqlitePairingStore.revoke(agent_id: str, at: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_revoked_agent_is_not_returned_as_active(tmp_path):
    store = SqlitePairingStore(tmp_path / "broker.sqlite3")
    store.create(APPROVED_REQUEST)
    store.revoke(APPROVED_REQUEST.agent_id, NOW)
    assert store.active_agent(APPROVED_REQUEST.agent_id) is None
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `pytest src/auth-broker/tests/test_sqlite.py -q`

Expected: FAIL because `SqlitePairingStore` is not defined.

- [ ] **Step 3: Implement the minimal transactional store**

```python
class SqlitePairingStore:
    def revoke(self, agent_id: str, at: datetime) -> None:
        self.connection.execute("UPDATE agents SET revoked_at = ? WHERE id = ?", (at.isoformat(), agent_id))
        self.connection.commit()
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `pytest src/auth-broker/tests/test_sqlite.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth-broker
git commit -m "feat: persist pairing state and revocation"
```

### Task 3: HTTP boundary, owner approval and Hermes adapter

**Files:**
- Create: `src/auth-broker/app/api.py`
- Create: `src/auth-broker/app/adapters/hermes.py`
- Create: `src/auth-broker/tests/test_api.py`

**Interfaces:**
- Consumes: `Cf-Access-Jwt-Assertion`, pairing request id and signature.
- Produces: `POST /v1/pairing-requests`, `POST /v1/pairing-requests/{id}/approve`, `POST /v1/context/query`.

- [ ] **Step 1: Write the failing tests**

```python
def test_unapproved_request_cannot_query_context(client):
    response = client.post("/v1/context/query", headers=UNAPPROVED_AGENT_HEADERS, json={"query": "status"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `pytest src/auth-broker/tests/test_api.py -q`

Expected: FAIL because the API application is not defined.

- [ ] **Step 3: Implement the minimal closed-by-default endpoint**

```python
@app.post("/v1/context/query")
def query_context(agent: ActiveAgent = Depends(require_active_agent)):
    return hermes_client.query_as_broker(agent_id=agent.id)
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `pytest src/auth-broker/tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth-broker
git commit -m "feat: enforce approved pairing at HTTP boundary"
```

### Task 4: Production composition and hostile JSON handling

**Files:**
- Create: `src/auth-broker/app/main.py`
- Create: `src/auth-broker/tests/test_main.py`
- Modify: `src/auth-broker/app/api.py`
- Modify: `src/auth-broker/tests/test_api.py`

**Interfaces:**
- Produces: `app.main:app`, the Uvicorn application factory composed from environment configuration.
- Consumes: `AUTH_BROKER_DATABASE_PATH`, `AUTH_BROKER_AUDIENCE`, `AUTH_BROKER_CF_ACCESS_ISSUER`, `AUTH_BROKER_CF_ACCESS_AUDIENCE`, `AUTH_BROKER_OWNER_EMAIL`, `HERMES_A2A_URL`, `HERMES_BROKER_TOKEN`.

- [ ] **Step 1: Write the failing tests**

```python
def test_missing_hermes_broker_token_fails_at_startup(monkeypatch):
    monkeypatch.delenv("HERMES_BROKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HERMES_BROKER_TOKEN"):
        build_app_from_environment()

def test_deep_json_is_a_controlled_client_error(client):
    response = client.post("/v1/pairing-requests", content=deep_json, headers={"content-type": "application/json"})
    assert response.status_code == 400
```

- [ ] **Step 2: Run tests and verify expected failures**

Run: `pytest src/auth-broker/tests/test_main.py src/auth-broker/tests/test_api.py -q`

Expected: FAIL because `build_app_from_environment` and controlled deep-JSON handling do not exist.

- [ ] **Step 3: Implement minimal production composition**

```python
def build_app_from_environment() -> FastAPI:
    return create_app(
        database_path=require_env("AUTH_BROKER_DATABASE_PATH"),
        audience=require_env("AUTH_BROKER_AUDIENCE"),
        owner_verifier=CloudflareAccessVerifier(
            issuer=require_env("AUTH_BROKER_CF_ACCESS_ISSUER"),
            audience=require_env("AUTH_BROKER_CF_ACCESS_AUDIENCE"),
            owner_email=require_env("AUTH_BROKER_OWNER_EMAIL"),
        ),
        hermes=HttpHermesClient(
            url=require_env("HERMES_A2A_URL"),
            bearer=require_env("HERMES_BROKER_TOKEN"),
        ),
    )
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `pytest src/auth-broker/tests -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/auth-broker
git commit -m "feat: add broker production composition"
```

## Self-review

- The plan covers autonomous request, owner approval, key proof, SQLite persistence, revocation and separate upstream authentication.
- It deliberately excludes scopes and third-party sharing, which are v0.1 work.
- All production interfaces named in later tasks are introduced by an earlier task or the same task.

## Execution record

- Task 1 completed in `77bd0ca` after the pairing lifecycle tests passed.
- Task 2 completed in `a7b7317`, with the audit-state correction in `fd5cf71` after review.
- Task 3 completed in `493675a`; the deep-JSON client-error review finding was addressed in Task 4.
- Task 4 completed in `b106263`; it adds fail-closed production composition and the controlled deep-JSON response. The complete broker suite passed locally (81 tests; two dependency deprecation warnings).
