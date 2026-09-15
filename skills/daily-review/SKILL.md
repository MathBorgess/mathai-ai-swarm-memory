---
name: daily-review
description: Evening daily review for mathai swarm reports — read the stored revision, update daily/ and brand/posts/, propose tomorrow, record the ledger, open vault PRs via the reports CLI.
---

# daily-review (F4)

## Mandatory orientation, before anything else

Read in this order and do not skip:

1. `mathai-wiki/CLAUDE.md` — vault rules: tiers T0–T3, branch + PR, wikilinks from the
   root, `external:` as a pointer.
2. `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`
   — the closed design. Do not reopen decisions; report contradictions.
3. `mathai-ai-swarm-memory/AGENTS.md` and `CLAUDE.md`.
4. `docs/operations/daily-reports-f4.md` — config, ledger, publish modes, and the single
   night engine (`run_evening_session`).
5. `skills/daily-plan/SKILL.md` — what the morning froze; the evening only validates
   against that freeze.

## What you are closing

The owner already answered in the report form (or pasted the same JSON locally). Your job
is **not** to reinterpret the day in prose metrics. F1 code computes every number that
lands in `## Evolução` and in the metric band the next morning.

You may run an optional **reflection** adapter (`evening.reflection.command`) for qualitative
notes and improvement *suggestions*. Suggestions become ledger rows in `pending-review`;
they never merge skills, change weights, or publish posts by themselves.

## Inputs

| Source | Use |
|---|---|
| Stored revision | `RevisionStore.read_revision(day, revision)` — the only payload |
| Freeze state | `reports-state.json` frozen checklist for the day |
| Wiki | `daily/YYYY-MM-DD.md` on the morning's isolated branch |
| Posts | `brand/posts/*.md` matched by `url:` |

Never invent `MAT-*`, `event_id`, or checkpoint numbers. Empty search means "not found".

## Effects (order is fixed)

1. **State** — mark `evening_validated` and validated ids in `reports-state.json`.
2. **Vault** — commit on the day's branch (freeze branch if it still exists): checkboxes,
   classifications, unplanned block, evolution lines, tomorrow proposal markers, post notes.
3. **Ledger** — one row per effect (`wiki-commit`, `wiki-pr`, `post-note`, `tomorrow-plan`,
   `contest`, draft acknowledgements). Status is honest: `failed` if `gh pr create` failed.
4. **Publish** — push + PR when `evening.publish.mode` is `gh` (production default path).

Re-running the **same** revision after a crash must not create a second commit (identical
bytes → reuse HEAD). A **newer** revision stacks one more commit; an **older** revision is
`superseded` and ignored.

## Evening payload rules

Same schema as `POST /evening` and `report evening --input`:

- Checklist ids must be a subset of the frozen ids; `done` drives validated completion.
- `unplanned` items are recorded separately; blank `classification` carries no scope penalty.
- `posts` update metrics/checkpoints on existing notes or create spontaneous notes with
  `guided: false` on a stable filename derived from the URL.
- `review_actions` only acknowledge drafts (`approve` / `reject`); nothing is published.
- `ledger_classifications` apply owner labels to past ledger rows by `entry_id`; unknown ids
  are ignored.

## Ready commands

```bash
CONFIG="/abs/reports.json"   # absolute path required

# Phone path: form POST is already stored; drain the outbox (default serve does this in-process)
mathai-swarm report serve --config "$CONFIG" --drain-outbox

# Local path: paste the copied JSON (same bytes as POST)
mathai-swarm report evening --config "$CONFIG" --input /abs/evening.json

# Operator job stdin (outbox worker): revision/hash/owner only — re-read content from disk
mathai-swarm report evening --config "$CONFIG" --job-stdin

# Demo without network PR
mathai-swarm report evening --config "$CONFIG" --input /abs/evening.json --no-publish
```

`--config` works before or after the subcommand. Exit code `1` when the night closed the
day but the vault PR failed — metrics are still durable.

## Autonomy (until F5)

- Wiki T2/T3: branch + PR; lint may run via `evening.lint_command` but **no auto-merge**.
- Skill/weight suggestions: ledger `pending-review` only; draft PRs are human work.
- T0: text suggestions only.
- At most **three** "Decisões que merecem pergunta" items in your own code PRs.

## Do not

- Do not publish posts, send email, or merge wiki PRs from this skill.
- Do not execute paths or shell commands returned by a model adapter.
- Do not freeze tomorrow: only propose up to three P0 in state and in the daily note marker.
