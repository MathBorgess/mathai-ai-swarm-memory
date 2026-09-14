# Daily reports — F4 (noite)

F4 closes the day: one stored evening revision in, durable state + vault edits + ledger
rows out, then an optional push/PR. F3 only stores revisions and enqueues jobs; F4 is the
only implementation of "what the night does".

## Single engine

`swarm_reports.evening.session.run_evening_session` is invoked from:

| Entry | How |
|---|---|
| `POST /evening` | revision saved → outbox → in-process dispatcher (default `serve`) or `dispatch_command` |
| `mathai-swarm report evening --input FILE` | `submit_evening` then the same engine |
| `mathai-swarm report evening --job-stdin` | reads one outbox job JSON from stdin; loads revision by number/hash |

There is no second night implementation. Stale revisions (`revision < applied_revision`) return
`superseded` without touching the vault.

## Config (`evening` block)

```json
{
  "evening": {
    "publish": {
      "mode": "gh",
      "remote": "origin",
      "base_branch": "main",
      "gh_bin": "gh",
      "draft": false,
      "repo": "MathBorgess/mathai-wiki",
      "timeout_seconds": 120
    },
    "wiki_local_only": false,
    "lint_command": ["python3", "scripts/lint.py"],
    "max_p0_tomorrow": 3,
    "reflection": {
      "command": ["/abs/reflection-adapter"],
      "timeout_seconds": 120
    }
  }
}
```

| `publish.mode` | Behaviour |
|---|---|
| `none` | Local commit only (tests, demo) |
| `queue` | Record intent under `wiki-publish-queue/` (F2 seam) |
| `push` | `git push` branch |
| `gh` | push + `gh pr create` with expected-head gate |

Production operators should use `gh` (or an external handler via `server.dispatch_command`);
`--no-publish` is for demos, not the only working path.

## State (additions)

```
<state_dir>/evening/<day>/session.json     applied revision + per-run checkpoints
<state_dir>/ledger/<day>.json              autonomous actions (outside git)
<state_dir>/plans/<day>.proposed.json        tomorrow input for the next morning (not a freeze)
<state_dir>/wiki-worktrees/evening-<day>/    when no freeze branch remains
```

Session phases: `state` → `wiki` → `publish` → `completed`. A crash after the git commit
replays identical bytes and reuses HEAD.

## Ledger

Every effect gets a deterministic `entry_id` from `(day, revision, kind, target)`. Status
flow:

- written `pending` before the attempt;
- `completed` or `failed` after;
- `pending-review` for contests and suggestions (never auto-applied).

The next morning's HTML reads yesterday's ledger via `evening.ledger.morning_view` — failed
PRs show as `[FALHOU]`, not as done.

## Wiki writeback

`swarm_reports.wiki.night.apply_night_edits` runs in an isolated worktree:

- stacks on `codex/reports-freeze-YYYY-MM-DD` when that branch still exists;
- otherwise `codex/reports-evening-YYYY-MM-DD` off fetched `main`.

Preserves owner prose, `external:` pointers, and swarm markers; updates checkboxes,
classifications, unplanned block, evolution block (code-generated lines), and tomorrow
proposal markers.

## CLI

```bash
# Close from pasted JSON (same as the form POST body)
mathai-swarm report evening --config /abs/reports.json --input /abs/evening.json

# Submit only (leave job pending)
mathai-swarm report evening --config /abs/reports.json --input /abs/evening.json --submit-only

# Default serve: in-process night handler + background outbox drain
mathai-swarm report serve --config /abs/reports.json

# Recover after crash without binding a socket
mathai-swarm report serve --config /abs/reports.json --drain-outbox
```

## Demo (synthetic, no vault PR)

```bash
REPO="/path/to/mathai-ai-swarm-memory/worktree"
DEMO="/tmp/mathai-f4-demo"
rm -rf "$DEMO"
mkdir -p "$DEMO"/{wiki/daily,wiki/brand/posts,out,state}
cp "$REPO/src/reports/tests/fixtures/metrics/res-weights.json" "$DEMO/res-weights.json"

# minimal git vault
git -C "$DEMO/wiki.parent" init -b main "$DEMO/wiki" 2>/dev/null || true
# … see tests/conftest.py `git_wiki` for the exact bootstrap

cat > "$DEMO/reports.json" <<'JSON'
{
  "wiki_dir": "/tmp/mathai-f4-demo/wiki",
  "state_dir": "/tmp/mathai-f4-demo/state",
  "weights_path": "/tmp/mathai-f4-demo/res-weights.json",
  "output_dir": "/tmp/mathai-f4-demo/out",
  "owner_id": "owner",
  "timezone": "America/Recife",
  "public_origin": "http://127.0.0.1:8787",
  "evening": { "publish": { "mode": "none" }, "wiki_local_only": true }
}
JSON

cd "$REPO/src/reports"
PYTHONPATH=. python3 -m pytest -q tests/test_evening_full_cycle.py -k full_day
```

## Tests

```bash
cd src/reports && PYTHONPATH=. python3 -m pytest -q
```

F4-focused modules:

- `tests/test_evening_session.py` — ordering, idempotency, ledger honesty, contests
- `tests/test_evening_ledger.py` — morning view, classifications
- `tests/test_evening_publish.py` — `gh` transport and expected-head gate
- `tests/test_wiki_notes.py` — pure markdown writeback rules
- `tests/test_evening_full_cycle.py` — morning → HTTP POST → drain → next morning ledger

## Not in F4

- F5 dispatch, quota pacing, merge policy, digest cards
- F6 discovery modules
- Cron, tunnel, Access, or live provider calls in tests
