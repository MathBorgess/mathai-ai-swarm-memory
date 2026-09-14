---
name: daily-plan
description: Morning daily plan for mathai swarm reports — Linear/Calendar, P0 freeze, teach-me, post-voice draft.
---

# daily-plan (F2)

Run after reading the closed design note in mathai-wiki (`2026-09-13-daily-reports-ciclo-self-improvement.md`).

## Gather sources (live)

1. Linear — use harness MCP connectors; record exact issue id/pointer. Empty/error → `source-unavailable`, never invent `MAT-*`.
2. Google Calendar — same; use real `event_id` pointers only.
3. Priority + deadline ≤7d → choose up to **3 P0** items.

## Skills

- Study block → invoke **teach-me**; only put the returned lesson URL in the plan (`lesson.link`).
- Optional post → invoke **post-voice**; store draft in `optional_post_draft` (review tab, not published).

## Emit plan JSON

Write absolute-path JSON for offline replay:

```bash
mathai-swarm-reports --config "$MATHAI_REPORTS_CONFIG" report morning \
  --date "$(TZ=America/Recife date +%F)" \
  --plan /abs/path/morning-plan.json
```

Without `--plan`, configure `planner_provider.command` (argv JSON stdin/stdout) or accept `unavailable` sources.

## Freeze

Normal run (no `--replay`) freezes checklist in wiki worktree branch `codex/reports-freeze-YYYY-MM-DD`
and records snapshot commit in markers + `reports-state.json`. Re-running the same day is idempotent.
