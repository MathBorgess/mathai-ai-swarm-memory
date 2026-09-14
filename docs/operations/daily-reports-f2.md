# Daily reports — F2 (manhã)

F2 adds the morning runtime: planner input, wiki freeze in an isolated git worktree,
deterministic metrics in HTML, evening form seed (`EveningPayload`), and CLI wiring.

## Install (same venv as broker)

```bash
pip install -e src/auth-broker -e src/reports
```

`mathai-swarm-reports` is a separate entrypoint; `mathai-swarm report morning` delegates
to the reports package when installed. Broker admin commands still require `--store`.

## Config (absolute paths)

```json
{
  "wiki_dir": "/abs/mathai-wiki",
  "state_dir": "/var/mathai/reports-state",
  "weights_path": "/abs/mathai-wiki/brand/metrics/res-weights.json",
  "output_dir": "/var/mathai/reports-html",
  "owner_id": "mathborgess",
  "timezone": "America/Recife",
  "planner_provider": {
    "command": ["python3", "/abs/planner_adapter.py"],
    "timeout_seconds": 120
  }
}
```

Environment: `MATHAI_REPORTS_CONFIG=/abs/reports.json`

Secrets stay in `.env` / environment — never in config JSON or HTML.

## CLI

```bash
mathai-swarm-reports --config /abs/reports.json report morning --date 2026-09-14
mathai-swarm-reports --config /abs/reports.json report morning --plan /abs/plan.json --replay
mathai-swarm report morning --config /abs/reports.json   # no --store required
```

`--dry-run` / `--replay` do not mutate wiki or durable state.

## EveningPayload (F3/F4 seam)

Schema version `1` — see `swarm_reports.evening_schema.evening_payload_json_schema()`
and `parse_evening_payload()`. Copy-prompt and POST body must use the same canonical JSON.

## Tests

```bash
cd src/reports && python -m pip install -e '.[dev]' && python -m pytest -q
```
