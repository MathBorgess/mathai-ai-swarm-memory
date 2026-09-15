---
name: daily-plan
description: Morning daily plan for mathai swarm reports — Linear/Calendar intake, full checklist with up to 3 P0, teach-me lesson link, post-voice draft, then freeze via the reports CLI.
---

# daily-plan (F2)

## Mandatory orientation, before anything else

Read in this order and do not skip:

1. `mathai-wiki/CLAUDE.md` — vault rules: tiers T0–T3, branch + PR, wikilinks from the
   root, `external:` as a pointer.
2. `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`
   — the closed design. Every decision there belongs to the owner. Do not reopen one; if
   you find a contradiction, report it instead of choosing.
3. `mathai-ai-swarm-memory/AGENTS.md` and `CLAUDE.md`.
4. `docs/operations/daily-reports-f2.md` — the config, the plan JSON and the freeze rules
   this skill produces input for.
5. `mathai-wiki/daily/_template.md` and the most recent `daily/YYYY-MM-DD.md`.

## Gather sources (live, read-only)

1. **Linear** — via the harness MCP connector. Record the exact issue id as
   `source_pointer` (`linear:MAT-193`). Never invent a `MAT-*`.
2. **Google Calendar** — same, with real `event_id` pointers only.
3. Mark each source with an honest `status`:
   - `ok` — you reached it and the result (including empty) is trustworthy;
   - `unavailable` — no connector, no auth, or you did not query it;
   - `error` — the query failed.

   Only `ok` can confirm an empty day. With anything else the morning run refuses to
   freeze an empty checklist, on purpose.

## Choose the checklist and the P0 subset

- `checklist` is the **whole** `## Hoje` list, not only the P0 items.
- `is_p0` marks at most **3** items. Eligibility is priority Urgent/High **or** a calendar
  deadline within 7 days — OR, not AND.
- An item carried from a previous day keeps its original `first_planned`, so date drift
  survives the rollover.

## Skills

- Study block present → invoke **teach-me** and put only the returned link in
  `lesson.link` (`https://...` or an internal `/lessons/<safe-path>`). The lesson flow
  belongs to `teach-me`; the evening form does not interact with it.
- Optional post → invoke **post-voice** and store the draft in `optional_post_draft`.
  It lands in the Revisar tab. Approving marks it ready; publishing is always the owner's.

## Emit the plan JSON

```json
{
  "day": "2026-09-14",
  "confirmed_empty": false,
  "checklist": [
    {"task_id": "MAT-193", "text": "fechar broker", "is_p0": true, "source_pointer": "linear:MAT-193"},
    {"task_id": "MAT-201", "text": "revisar nota", "first_planned": "2026-09-12", "source_pointer": "linear:MAT-201"}
  ],
  "agenda": [{"title": "1:1", "when": "09:00", "source_pointer": "event:abc123"}],
  "handoffs": [
    {
      "task_id": "MAT-193",
      "title": "Fechar o broker",
      "objective": "Subir o grant curto com audience /mcp",
      "context_links": ["https://github.com/MathBorgess/mathai-ai-swarm-memory/pull/14"],
      "copy_prompt": "Leia src/auth-broker/README.md e ...",
      "criteria": "testes de protocolo verdes",
      "time_budget_minutes": 90
    }
  ],
  "review_drafts": [{"draft_id": "d1", "kind": "post", "reason": "tese do dia", "content": "..."}],
  "ledger": [{"entry_id": "pr-42", "kind": "pr", "summary": "merged X", "link": "https://github.com/..."}],
  "lesson": {"link": "/lessons/aws-sap.html", "topic": "SAP"},
  "sources": [
    {"kind": "linear", "pointer": "assignee:me state:started", "status": "ok"},
    {"kind": "calendar", "pointer": "primary", "status": "ok"}
  ]
}
```

## Ready commands

```bash
DAY="$(TZ=America/Recife date +%F)"
PLAN="/abs/tmp/morning-plan-$DAY.json"     # absolute path required

# 1. render without touching wiki or state, to eyeball the page first
mathai-swarm report morning --config "$MATHAI_REPORTS_CONFIG" --date "$DAY" --plan "$PLAN" --replay

# 2. the real run: freeze + commit + HTML
mathai-swarm report morning --config "$MATHAI_REPORTS_CONFIG" --date "$DAY" --plan "$PLAN"

# 3. rerun later in the day: refreshes metrics/agenda/ledger, never the frozen checklist
mathai-swarm report morning --config "$MATHAI_REPORTS_CONFIG" --date "$DAY" --plan "$PLAN"
```

`--config` works before or after the subcommand. Without `--plan`, either configure a
`planner_provider` of kind `json-stdio` (your own adapter) or accept `unavailable`
sources — a provider CLI that emits prose is F5 work, not this seam.

## Freeze

A normal run freezes the checklist on `codex/reports-freeze-YYYY-MM-DD` in an isolated
worktree started from a fetched `origin/main`, records the snapshot in the note marker and
the commit sha in `reports-state.json`, and queues the push + PR intent. Re-running the
same day is idempotent for the checklist and always regenerates the HTML.
