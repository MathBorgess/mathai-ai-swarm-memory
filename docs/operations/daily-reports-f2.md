# Daily reports — F2 (manhã)

F2 is the morning runtime: plan intake, the wiki freeze in an isolated git worktree, the
deterministic metric band, the evening form seed (`EveningPayload`) and the CLI wiring.

## Install (same venv as the broker)

```bash
pip install -e src/auth-broker -e src/reports
```

Both entrypoints work and take `--config` in either position:

```bash
mathai-swarm-reports --config /abs/reports.json report morning --date 2026-09-14
mathai-swarm report morning --config /abs/reports.json --date 2026-09-14
```

`mathai-swarm report ...` is forwarded verbatim to the reports CLI and never requires
the broker's `--store`.

## Config (absolute paths, secrets only in the environment)

```json
{
  "wiki_dir": "/abs/mathai-wiki",
  "state_dir": "/var/mathai/reports-state",
  "weights_path": "/abs/mathai-wiki/brand/metrics/res-weights.json",
  "output_dir": "/var/mathai/reports-html",
  "owner_id": "mathborgess",
  "timezone": "America/Recife",
  "public_origin": "https://reports.mathai.com.br",
  "planner_provider": {
    "kind": "json-stdio",
    "command": ["python3", "/abs/planner_adapter.py"],
    "timeout_seconds": 120
  }
}
```

- `public_origin` only decides whether the HTML offers the **Enviar** button; the page
  itself uses same-origin relative URLs (`/evening`, `/evening/revision?day=...`) so it
  works behind any hostname. Without it the report is copy-prompt only.
- Environment: `MATHAI_REPORTS_CONFIG=/abs/reports.json`.
- Secrets stay in `.env` / the environment — never in this JSON, never in the HTML.

### `planner_provider` is a JSON contract, not a provider integration

`kind` must be `json-stdio`: your adapter reads the request JSON on stdin and writes a
`MorningPlan` JSON on stdout. Pointing `command` at `claude`, `codex` or `cursor-agent`
directly **does not work** — they emit prose, and the run fails with "planner provider
returned invalid JSON". Parsing real provider output and routing between them is F5
(`skills/handoff` routing table). Until then the supported paths are:

1. `--plan /abs/morning-plan.json` written by the `daily-plan` skill, or
2. no provider at all, with sources reporting `unavailable`, or
3. your own `json-stdio` adapter.

## Morning plan JSON

```json
{
  "day": "2026-09-14",
  "confirmed_empty": false,
  "checklist": [
    {"task_id": "MAT-193", "text": "fechar broker", "is_p0": true, "source_pointer": "linear:MAT-193"},
    {"task_id": "MAT-201", "text": "revisar nota", "first_planned": "2026-09-12"}
  ],
  "agenda": [{"title": "1:1", "when": "09:00", "source_pointer": "event:abc"}],
  "handoffs": [{"task_id": "MAT-193", "title": "...", "objective": "...", "context_links": ["https://..."], "copy_prompt": "...", "criteria": "...", "time_budget_minutes": 90}],
  "review_drafts": [{"draft_id": "d1", "kind": "post", "reason": "...", "content": "..."}],
  "ledger": [{"entry_id": "pr-42", "kind": "pr", "summary": "merged X", "link": "https://github.com/..."}],
  "lesson": {"link": "/lessons/aws-sap.html", "topic": "SAP"},
  "sources": [
    {"kind": "linear", "pointer": "assignee:me", "status": "ok"},
    {"kind": "calendar", "pointer": "primary", "status": "ok"}
  ]
}
```

`checklist` is the **whole** `## Hoje` list. `is_p0` marks the selected subset, capped at
3. P0 eligibility is priority Urgent/High **or** a calendar deadline within 7 days — the
design uses OR, so an Urgent issue with no date qualifies and so does a hard deadline
with no priority.

`status` values are `ok`, `unavailable` and `error`. Only `ok` can confirm an empty day:

- empty checklist + every source `ok` + `"confirmed_empty": true` → freezes an empty day;
- empty checklist + any source not `ok` → the run refuses with `refusing to freeze: ...`,
  because the freeze is irreversible for that date.

Lesson links are `http(s)://...` or an internal `/lessons/<safe-path>`; nothing else.

## Idempotency and crash recovery

One run per calendar day holds an exclusive lease (`state_dir/morning/<day>.run.lock`),
and `state_dir/morning/<day>.progress.json` checkpoints the phases:

| Phase | Written after |
|---|---|
| `planned` | the plan exists and validates; a provider result is cached *before* this |
| `frozen` | the wiki commit exists and `reports-state.json` records its sha |
| `completed` | the HTML has been atomically replaced on disk |

Consequences you can rely on:

- Re-running the morning **regenerates the HTML** from the frozen snapshot with a fresh
  metric band, agenda and ledger. It does not re-invoke the planner and does not touch
  the frozen checklist, even if the incoming plan now says something different.
- A run that dies mid-freeze leaves the day retryable. The next run resumes from the
  cached plan; it never relaunches a provider whose effects are unknown.
- `--force-replan` is the escape hatch when the cached planner output was garbage.

Inspect or reset a day:

```bash
cat /var/mathai/reports-state/morning/2026-09-14.progress.json
mathai-swarm-reports --config /abs/reports.json report morning --date 2026-09-14 --force-replan
```

## Wiki freeze

```
branch    codex/reports-freeze-YYYY-MM-DD
worktree  <state_dir>/wiki-worktrees/freeze-YYYY-MM-DD
base      origin/main, after an explicit fetch
```

- The live vault checkout is never read into the worktree, so uncommitted owner text
  stays private. A fetch failure aborts the run instead of freezing a stale base.
- One worktree per day, and the branch is verified after checkout.
- A missing `daily/YYYY-MM-DD.md` is created from `daily/_template.md`, keeping its
  sections; a morning before the owner opens the day is normal.
- The marker records `snapshot=<id>` only. The commit sha is external state in
  `reports-state.json` (`frozen_at_commit`); the commit is never amended.
- Push + PR go through the `WikiPublisher` seam. F2 ships `QueuedPublisher`, which writes
  the intent to `<state_dir>/wiki-publish-queue/`, and `GitPushPublisher`, which also
  pushes the branch. Creating the PR needs the VPS GitHub credential and lands in F4/F5;
  the queue exists so nothing depends on the owner remembering.

`--wiki-local-only` freezes against local `HEAD` instead of `origin/main`. It exists for
tests and for a vault clone with no remote; do not use it on the VPS.

## Metric band

| Tile | Definition |
|---|---|
| Ontem | conclusion for `day-1`: validated ids ÷ frozen ids, `sem validação` with no closed evening |
| 7d conclusão | mean conclusion over the closed days in `[day-7, day-1]`, plus `closed/planned` |
| Drift | days-open summed over every frozen item never validated, `first_planned` from carryover |
| RES guiado 7d | mean RES of guided posts in the window, with posts/week |
| RES espontâneo 7d | same for spontaneous posts, kept separate on purpose |

A day with no evening is excluded from the 7-day mean (a missing report is not a zero)
but counted in the denominator, which is the adherence signal the design asks for. Posts
in `brand/posts/` need `posted_on` (or a `YYYY-MM-DD-` filename) to enter the window and
`guided: true|false` to be attributed; unattributed posts are counted in a separate tile.

## EveningPayload (F3/F4 seam)

Schema version `1`. `swarm_reports.evening_schema.evening_payload_json_schema()` is the
complete document, embedded in the page as `<script id="evening-schema">`.

```json
{
  "schema_version": 1,
  "day": "2026-09-14",
  "owner_id": "mathborgess",
  "checklist": [{"task_id": "MAT-193", "done": true, "classification": null}],
  "unplanned": [{"task_id": "hash:...", "text": "consertei o CI", "done": true, "classification": "oportunidade"}],
  "posts": [{"url": "https://...", "platform": "linkedin", "guided": false, "checkpoint": "48h",
             "metrics": {"reach": 320, "outside_fraction": 0.77, "signals": {"comments": 4}}}],
  "ledger_classifications": [{"entry_id": "pr-42", "classification": null}],
  "review_actions": [{"draft_id": "d1", "action": "edit", "content": "texto editado"}],
  "pending_checkpoint_ids": ["post:2026-09-12"],
  "notes": ""
}
```

Validation rules F3 and F4 both depend on:

- Types are strict. `"false"`, `0` and `1` are not booleans; `"1"` is not an integer.
- Unknown keys are rejected at every level. `lesson_completed` is gone: the design says
  the form does not interact with the lesson.
- Numbers must be finite and non-negative; `NaN` and `Infinity` are rejected.
- Bounds raise; nothing is silently truncated.
- `notes: ""` is valid. A blank classification is `null` and carries no penalty.
- `owner_id` is a consistency field only. The F3 server takes identity from its own
  config and never from the body.
- `client_saved_at` is transport metadata, excluded from the revision content hash, so a
  resend that differs only by clock creates no revision.

`readForm()` in the page is the single producer of this body: **Copiar prompt** puts its
canonical JSON on the clipboard and **Enviar** POSTs the same content plus
`client_saved_at`. Drafts in `localStorage` are versioned by revision number, never by
clock, so restoring one cannot lose an edit.

## Tests

```bash
cd src/reports && python -m pip install -e '.[dev]' && python -m pytest -q
```

A synthetic 375px page for visual inspection:

```bash
python -m pytest -q tests/test_render_escape.py -k synthetic
```
