# Isolated ask — Hermes generator, not the owner A2A peer

This slice is the ask tool: a **dedicated, tools/memory-free Hermes `AIAgent`**
in a per-job generated profile, fed only the currently authorized envelope. It
is not wired into `app/api.py` or `app/main.py`. SQLite durability and the
`mathai-swarm` CLI are owned by other agents. Do not treat these tests as
production/model/harness proof.

Primary sources used for the generator:

- [Hermes Python library](https://hermes-agent.nousresearch.com/docs/guides/python-library) — `AIAgent(..., enabled_toolsets=[], skip_memory=True, skip_context_files=True)`
- [Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) — `memory_enabled`/`user_profile_enabled` false; `plugins.enabled: []`; `terminal.home_mode: profile`
- [Hermes `model_tools.get_tool_definitions`](https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/model_tools.py) — `enabled_toolsets is None` means all tools; `[]` means none
- [Hermes `agent/agent_init.py`](https://github.com/NousResearch/hermes-agent/blob/de2d6a1b93508463c31434c1ae067e204af81238/agent/agent_init.py) — `skip_memory` skips external memory providers; `plugins.enabled` is opt-in
- [Hermes Docker environment](https://github.com/NousResearch/hermes-agent/blob/main/tools/environments/docker.py) — `--cap-drop ALL`, `no-new-privileges`; we add `--read-only` and reject `host` network

Pinned Hermes commit (also in `ask-worker/HERMES_PIN`):
`de2d6a1b93508463c31434c1ae067e204af81238`.

Ask does **not** call `HttpHermesClient` / `message/send`. That path is the
owner's unrestricted A2A peer.

## Interface for the integration agent

Package: `app.ask` (under `src/auth-broker/`). Tests: `tests/test_ask*.py`.

```python
from pathlib import Path
from app.ask import (
    AskBudget, AskService, ContainerWorker, IsolationConfig, MemoryThreadStore, build_router, worker_root,
)

isolation = IsolationConfig(
    image="mathai-ask-worker:local",
    launch_script=worker_root() / "launch.sh",
    worker_script=worker_root() / "worker_main.py",
    style_path=worker_root() / "style" / "SOUL.md",  # only this public file
    docker_bin=Path("/usr/bin/docker"),
    inference_config=Path("/etc/auth-broker/ask-inference.json"),  # dedicated file, not ~/.hermes
    network="ask-egress",  # dedicated docker network; never "host"
)
isolation.validate()
worker = ContainerWorker(isolation)
threads = MemoryThreadStore(max_turns=8, ttl_seconds=3600)  # in-process ephemeral; lost on restart

result = AskService(store=context_store, worker=worker, threads=threads, isolation=isolation).ask(
    principal, query, thread_id=None,
)

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
`cited_handles` is only what the model listed and that still exists in this turn's
envelope. An empty list means no validated citation; the worker does not label
every envelope handle as cited.

Suggested wiring in `main.py` (do **not** land in this slice): only when
`AUTH_BROKER_ASK_INFERENCE_CONFIG`, `AUTH_BROKER_ASK_IMAGE`,
`AUTH_BROKER_ASK_NETWORK`, and `AUTH_BROKER_CONTEXT_SQLITE` are all set;
incomplete subset is a startup error. Missing the set leaves ask uninstalled
(503, capabilities omit `ask`). Delegate `POST /v1/context/ask` like query so
`authorize()` runs once. Never fall back to the legacy Hermes bearer.

Dedicated inference file (operator-created, mode 0400, **not** owner `~/.hermes`
or `~/.env`):

```json
{
  "model": "provider/model-id",
  "base_url": "https://example-inference.invalid/v1",
  "api_key": "<dedicated ask key, not an owner session>"
}
```

Tests may use `"transport": "stub"` in that file. There is no live provider call
in this repository's tests.

## Isolation (fail closed)

| Control | What it does |
|---|---|
| Per-job profile | `ContainerWorker` writes a fresh temp directory (config + public SOUL + empty bundled-plugins). It never mounts an existing Hermes home and never deletes user `memories/` |
| Container launch | `ask-worker/launch.sh`: `docker run --read-only --cap-drop ALL --security-opt no-new-privileges --network "$ASK_NETWORK" --name …` plus tmpfs scratch, numeric `--user`. Bind-mounts: generated profile, job JSON, worker script, public style, inference file. All read-only |
| Dockerfile | Clones pinned Hermes via `install_hermes.sh`, runs `verify_hermes.py` (import + sha256 + `AIAgent.chat -> str`). `ENTRYPOINT ["python3", "/opt/ask-worker/worker_main.py"]`; launch command is only `/job.json` |
| Hermes `AIAgent` | `enabled_toolsets=[]`, `skip_memory=True`, `skip_context_files=True`, `load_soul_identity=False`, `max_iterations=1`. After init the worker asserts `agent.tools == []`, empty `valid_tool_names`, no memory provider/store |
| Profile config | `ask-worker/config.yaml`: `memory_enabled: false`, `plugins.enabled: []`, `enabled_toolsets: []` |
| Style | `ask-worker/style/SOUL.md` only |
| Secrets | Launch unsets `HERMES_BROKER_TOKEN`, `HERMES_A2A_URL`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Job payload has excerpts/handles, never caller tokens. Inference credentials are the dedicated file |

Absent image, inference file, launch script, public style, docker binary, or
network → `IsolationUnavailable` (HTTP 503). Host network is rejected.

**Network / egress:** `ASK_NETWORK` is required. `host` is forbidden.
`none` has no egress: the worker cannot reach an inference provider (correct for
infrastructure tests). Production generation needs a dedicated non-host Docker
network that can reach only the inference `base_url`. This slice does not create
that network.

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
`MemoryThreadStore` is **single-process and ephemeral** (no database): max 64
threads, 8 per principal, TTL sweep on every mutation, concurrent updates to the
same `thread_id` are rejected (429). Each turn re-runs `app.context.query.search`
on the current principal. Previous assistant prose is not replayed.

Limits: body 16 KiB; query 2000 chars; envelope 10 items; output 8000 chars
(capped while reading the worker pipe, and during generation via stream callback);
worker timeout kills the process group and `docker rm -f` the named container
(504); concurrency 2 (429). No fallback to personal Hermes.

## Operator launch

```bash
# From src/auth-broker on the VPS — dedicated inference file, not ~/.hermes
install -d -m 0750 /var/lib/auth-broker
install -m 0400 /path/to/ask-inference.json /etc/auth-broker/ask-inference.json
docker network create ask-egress   # dedicated; not host. Limit egress to the inference endpoint.
cd ask-worker && bash install_hermes.sh   # or:
docker build -t mathai-ask-worker:local ask-worker
# verify_hermes.py runs during the image build (no model call).

ASK_IMAGE=mathai-ask-worker:local \
ASK_JOB=/var/lib/auth-broker/ask-job.json \
ASK_PROFILE=/tmp/ask-profile-example \
ASK_WORKER_SCRIPT=$PWD/ask-worker/worker_main.py \
ASK_STYLE=$PWD/ask-worker/style/SOUL.md \
ASK_INFERENCE=/etc/auth-broker/ask-inference.json \
ASK_NETWORK=ask-egress \
ASK_CONTAINER_NAME=ask-manual \
bash ask-worker/launch.sh
```

`ASK_NETWORK=none` is only for infrastructure tests that do not call a provider.

## Evidence this slice does and does not provide

Does: hidden-sentinel absence from the worker payload; host worker honestly
reports a readable owner sentinel when the process can open it (no fake
confinement helper); launch argv ENTRYPOINT+`/job.json`; timeout aborts without
waiting on `ThreadPoolExecutor` shutdown and removes the named container; output
cap during pipe read; citation parse/validation; thread bounds and same-thread
reject; pinned Hermes `AIAgent` import with empty tools and `chat() -> str` over
a fake `_interruptible_api_call` transport.

Does not: live paid/local model call; VPS deploy; MCP tool surface; durable
thread SQLite; wiring into `create_app`; **container proof** when `docker` is
missing (tests skip with that label). A Docker smoke using a slim image with the
same ENTRYPOINT is attempted only when the daemon is present.
