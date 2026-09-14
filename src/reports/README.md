# mathai-swarm-reports (F1 + F2 + F3)

Deterministic metrics (F1), morning HTML + wiki freeze runtime (F2), and the server that
puts the report on the phone and receives the evening (F3).

## Layout

| Module | Role |
|--------|------|
| `swarm_reports.metrics.daily` | Parse `daily/YYYY-MM-DD.md` checklists + swarm HTML markers |
| `swarm_reports.metrics.posts` | Parse `brand/posts/*.md` YAML (`guided`, `url`, checkpoints, metrics) |
| `swarm_reports.metrics.res` | RES formula + wiki weight loader |
| `swarm_reports.metrics.execution` | Completion, drift, scope penalty |
| `swarm_reports.metrics.state` | `state_dir/reports-state.json` freeze + evening flags |

## Wiki markers (additive)

```markdown
<!-- swarm:frozen-checklist-begin snapshot=2026-09-14T08:00:00-03:00 -->
<!-- swarm:task-meta id=MAT-193 first_planned=2026-09-14 frozen=true -->
- [ ] item
<!-- swarm:frozen-checklist-end -->
<!-- swarm:added-after-freeze -->
- [ ] unplanned item
<!-- swarm:p0 --> or `P0` in text
<!-- swarm:class procrastinação|devaneio|oportunidade -->
```

The marker carries the snapshot id only. The freeze commit sha lives in
`reports-state.json` (`frozen_at_commit`): a marker cannot name the commit that contains
it without an amend, and an amend makes the recorded sha unreachable.

Daily YAML flags: `swarm_evening_validated`, `swarm_evening_absent`.

## RES weights (wiki, not bundled)

Proposed canonical path: `brand/metrics/res-weights.json` in `mathai-wiki`.
Tests use `tests/fixtures/metrics/res-weights.json` only.

## State freeze semantics

- `apply_morning_freeze` writes `frozen_checklist` once per day (idempotent).
- Completion denominator = frozen snapshot ids (state file or external list), not
  lines added after freeze.
- Absent evening → completion metric is `None` for that day; date drift still
  accumulates for open items (unchecked boxes alone do not close drift; pass
  `validated_complete_ids` to `compute_date_drift`).
- `save_state` uses atomic replace and mode `0600`; callers must hold F2 file
  locks across read-modify-write.

## Tests

```bash
cd src/reports && python -m pip install -e '.[dev]' && python -m pytest -q
```

## F2 morning

| Module | Role |
|--------|------|
| `swarm_reports.morning` | Dispatch-once / refresh-every-run orchestration |
| `swarm_reports.plan` | `MorningPlan`: full checklist, P0 subset, freeze gate |
| `swarm_reports.sources` | Linear/Calendar intake and P0 eligibility (priority **or** deadline) |
| `swarm_reports.tiles` | The metric band, computed from persisted state |
| `swarm_reports.storage` | Day lease + `MorningProgress` phase checkpoints |
| `swarm_reports.wiki.freeze` | Per-day isolated worktree freeze |
| `swarm_reports.wiki.publish` | Push + PR seam (`QueuedPublisher`, `GitPushPublisher`) |
| `swarm_reports.evening_schema` | Canonical `EveningPayload` for F3/F4 |
| `swarm_reports.cli` | `report morning` / `report evening --input` |

`mathai-swarm report ...` forwards verbatim to this CLI when both packages share a venv;
`--config` parses before or after the subcommand. See `docs/operations/daily-reports-f2.md`.

## F3 server

| Module | Role |
|--------|------|
| `swarm_reports.server.app` | Routes, security headers, origin check, `ReportServer` |
| `swarm_reports.server.config` | Auth profiles and the bind gate that refuses a public start without Access |
| `swarm_reports.server.access` | Cloudflare Access RS256 verification, stdlib only |
| `swarm_reports.server.submit` | The single submission path shared by POST and `--input` |
| `swarm_reports.server.revisions` | Immutable numbered revisions, content-hash dedup |
| `swarm_reports.server.outbox` | Durable jobs for the F4 night session, one claim each |

Three routes and nothing else: `/YYYY-MM-DD.html`, `POST /evening`,
`GET /evening/revision?day=...` (plus `/healthz` on loopback). `server.auth_mode` is
required, and only `cloudflare-access-jwt` may bind a non-loopback address. The night
session is F4: jobs accumulate in `<state_dir>/outbox/pending/` and
`report serve --drain-outbox` shows them without opening a socket. See
`docs/operations/daily-reports-f3.md`.
