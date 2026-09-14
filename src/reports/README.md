# mathai-swarm-reports (F1 + F2)

Deterministic metrics (F1) and morning HTML + wiki freeze runtime (F2).

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
<!-- swarm:frozen-checklist-begin snapshot=2026-09-14T08:00:00-03:00 commit=<git-sha> -->
- [ ] item
<!-- swarm:task-meta id=MAT-193 first_planned=2026-09-14 frozen=true -->
<!-- swarm:frozen-checklist-end -->
<!-- swarm:added-after-freeze -->
- [ ] unplanned item
<!-- swarm:p0 --> or `P0` in text
<!-- swarm:class procrastinação|devaneio|oportunidade -->
```

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
| `swarm_reports.morning` | Freeze + metrics + HTML |
| `swarm_reports.cli` | `mathai-swarm-reports report morning` |
| `swarm_reports.evening_schema` | `EveningPayload` for F3/F4 |
| `swarm_reports.wiki.freeze` | Isolated worktree checklist freeze |

`mathai-swarm report morning` delegates from `src/auth-broker` when both packages share a venv.
See `docs/operations/daily-reports-f2.md`.
