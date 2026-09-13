# Isolated ask — Hermes generator, not the owner A2A peer

This slice is the ask tool: a **dedicated, tools/memory-free Hermes `AIAgent`**
in an isolated runtime home, fed only the currently authorized envelope. It is
not wired into `app/api.py` or `app/main.py`. SQLite durability and the
`mathai-swarm` CLI are owned by other agents. Do not treat these tests as
production/model/harness proof.

Primary sources used for the generator:

- [Hermes Python library](https://hermes-agent.nousresearch.com/docs/guides/python-library) — `AIAgent(..., enabled_toolsets=[], skip_memory=True, skip_context_files=True)`
- [Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) — `memory_enabled`/`user_profile_enabled` false; `terminal.home_mode: profile`
- [Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) — `HERMES_HOME` is the profile boundary; a profile is **not** a sandbox
- [Hermes `agent/agent_init.py`](https://github.com/NousResearch/hermes-agent/blob/main/agent/agent_init.py) — `enabled_toolsets=[]` gates memory-provider and context-engine injection ([#30177](https://github.com/NousResearch/hermes-agent/pull/30177))
- [Hermes Docker environment](https://github.com/NousResearch/hermes-agent/blob/main/tools/environments/docker.py) — `--cap-drop ALL`, `no-new-privileges`; we add `--read-only` and default `--network none`

Ask does **not** call `HttpHermesClient` / `message/send`. That path is the
owner's unrestricted A2A peer.

## Interface for the integration agent

Package: `app.ask` (under `src/auth-broker/`). Tests: `tests/test_ask*.py`.

```python
from app.ask import (
    AskBudget, AskService, ContainerWorker, IsolationConfig, MemoryThreadStore, build_router, worker_root,
)

isolation = IsolationConfig(
    runtime_home=Path("/var/lib/auth-broker/ask-hermes-home"),  # not ~/.hermes, not $HERMES_HOME
    image="mathai-ask-worker:local",
    launch_script=worker_root() / "launch.sh",
    worker_script=worker_root() / "worker_main.py",
    style_path=worker_root() / "style" / "SOUL.md",
    docker_bin=Path("/usr/bin/docker"),
    network="none",  # or a dedicated egress network; never "host"
)
isolation.validate()
worker = ContainerWorker(isolation)
threads = MemoryThreadStore(max_turns=8, ttl_seconds=3600)  # replace with durable store later

# Direct call (MCP/tool layer):
result = AskService(store=context_store, worker=worker, threads=threads, isolation=isolation).ask(
    principal, query, thread_id=None,
)

# HTTP factory — same authorize(request) mapping as query/propose:
router = build_router(authorize=authorize, store=context_store, worker=worker, isolation=isolation, threads=threads)
# POST /v1/context/ask  body: {"query": "...", "thread_id"?: "..."}
```

`authorize(request)` must return the validated mapping
`principal_id`, `workspace_id`, `scopes`, `classifications`, `family_id`,
`expires_at`. The JSON body cannot supply those fields.

`AskService.ask(principal, query, thread_id=None)` →

```json
{
  "items": [{"text": "<isolated-generator prose>", "cited_handles": ["opaque"], "source_revision": "rev"}],
  "capability_receipt": {
    "principal_id": "...", "workspace_id": "...",
    "scopes_used": ["ctx:read:pesquisa.tcc"],
    "pass_as": "handle", "policy_version": "v1"
  },
  "thread_id": "opaque"
}
```

Empty authorized corpus → `items: []`, **no worker/model call**, `thread_id` still issued.

Suggested wiring in `main.py` (do **not** land in this slice): only when
`AUTH_BROKER_ASK_RUNTIME_HOME`, `AUTH_BROKER_ASK_IMAGE`, and
`AUTH_BROKER_CONTEXT_SQLITE` are all set; incomplete subset is a startup error.
Missing all three leaves ask uninstalled (503, capabilities omit `ask`).
Delegate `POST /v1/context/ask` like query so `authorize()` runs once. Never
fall back to the legacy Hermes bearer.

## Isolation (fail closed)

| Control | What it does |
|---|---|
| Dedicated `HERMES_HOME` | `IsolationConfig.runtime_home` must exist and must not be `$HOME`, `$HOME/.hermes`, or the broker process `HERMES_HOME` |
| Container launch | `ask-worker/launch.sh`: `docker run --read-only --cap-drop ALL --security-opt no-new-privileges --network ${ASK_NETWORK:-none}` plus tmpfs scratch, numeric `--user`, bind-mounts only for runtime home, job JSON, worker script, public style |
| Hermes `AIAgent` | `enabled_toolsets=[]`, `skip_memory=True`, `skip_context_files=True`, `load_soul_identity=False`, `max_iterations=1` |
| Profile config | `ask-worker/config.yaml`: `memory_enabled: false`, `user_profile_enabled: false`, `enabled_toolsets: []`, `terminal.home_mode: profile` |
| Style | `ask-worker/style/SOUL.md` only. Owner `SOUL.md` / `memories/*` are not mounted |
| Secrets | Launch unsets `HERMES_BROKER_TOKEN`, `HERMES_A2A_URL`, `HERMES_REAL_HOME`. Job payload has excerpts/handles, never caller tokens |

Absent image, runtime home, launch script, style, or docker binary →
`IsolationUnavailable` (HTTP 503). Host network is rejected. This is not
prompt-only isolation.

Worker payload (broker → container, no credentials):

```json
{
  "query": "...",
  "prior_user_turns": ["..."],
  "envelope": [{"handle": "...", "text": "...", "source_revision": "..."}],
  "budget": {"max_output_chars": 8000, "timeout_seconds": 30}
}
```

Threads store **user questions only**, keyed by `principal_id` + `workspace_id`.
Each turn re-runs `app.context.query.search` on the **current** principal.
Previous assistant prose and previously authorized excerpts are not replayed, so
a grant/ACL downgrade cannot keep private content in the worker input.
Cross-principal or expired thread → 403 `"Thread not available"` (same as
unknown). Bounded to 8 turns / 1 hour (configurable via `AskBudget` /
`MemoryThreadStore`). Citations are intersected with the current envelope
handles.

Limits: body 16 KiB; query 2000 chars; envelope 10 items; output 8000 chars;
worker timeout 30s (504); concurrency 2 (429). No fallback to personal Hermes.

## Operator launch

```bash
# From src/auth-broker, on the VPS — dedicated directory, not ~/.hermes
install -d -m 0750 /var/lib/auth-broker/ask-hermes-home/home
docker build -t mathai-ask-worker:local ask-worker
# Vendor hermes-agent into the image before production use (operator checkout).
# Tests never pull or call a model.

ASK_RUNTIME_HOME=/var/lib/auth-broker/ask-hermes-home \
ASK_IMAGE=mathai-ask-worker:local \
ASK_JOB=/var/lib/auth-broker/ask-job.json \
ASK_WORKER_SCRIPT=$PWD/ask-worker/worker_main.py \
ASK_STYLE=$PWD/ask-worker/style/SOUL.md \
ASK_NETWORK=none \
bash ask-worker/launch.sh
```

`ASK_NETWORK=none` is correct for infrastructure tests. Production model egress
needs a dedicated network that is still not `host`. Distinguish that from this
repo's tests, which use a fake docker and `HERMES_ASK_STUB=1` and never call a
provider.

## Evidence this slice does and does not provide

Does: hidden-sentinel absence in the exact worker payload; cross-principal
thread denial; grant/ACL change rebuilds the envelope; citation filtering;
timeout and missing isolation fail closed; launch argv includes the container
flags; worker process refuses owner `HERMES_HOME` / broker token.

Does not: live `AIAgent` against a paid/local model; VPS deploy; MCP tool
surface; durable thread SQLite; wiring into `create_app`.
