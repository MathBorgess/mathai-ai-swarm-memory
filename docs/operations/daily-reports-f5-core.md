# Daily reports — F5 core (dispatcher, quota, merge policy, digest)

Status: standalone core only, not wired into the CLI or F4's evening skill
yet. That integration is F5-integration, after F4 lands. No PR opened for
this commit — see the handoff brief for why (F5 must integrate after F4).

Design source: [[estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement]]
in `mathai-wiki`, section "Providers e quota" and "Autonomia".

## Scope

Owns `src/reports/swarm_reports/dispatch/` only. Does not touch `src/reports/pyproject.toml`
or `src/reports/swarm_reports/__init__.py` — those belong to F1. `swarm_reports` is
an implicit namespace package (PEP 420) until F1 adds its `__init__.py`; that is
why `src/reports/swarm_reports/` has no root `__init__.py` in this change while
`dispatch/` does.

## Modules

| Module | Responsibility |
|---|---|
| `quota.py` | `ProviderQuota` (5h/7d/30d typed snapshots), `daily_budget()` — tighter of 7d/30d, per day remaining; unknown buckets fall back to a local capped `UsageLedger`, never treated as zero or unlimited |
| `routing.py` | `classify()` buckets a probe reading (`ok`/`low`/`unknown`/`empty`), distinguishing a stale reading from a failed probe; `route()` is the weighted round-robin assignment from skills-catalog's `handoff` skill, ported so the CLI and the skill agree |
| `claims.py` | `ClaimStore` — one durable claim per `(date, task_id)`, exclusive-create lock file so concurrent callers race safely; running claims older than the timeout are resumable with `attempt += 1`; `done` is terminal |
| `priority.py` | `fit_to_budget()` — P0 → draft → improvement order against a daily budget; whatever does not fit comes back as `deferred`, never dropped |
| `providers.py` | `Runner` protocol + `SubprocessRunner` (argv-only, `shell=False`, timeout, output redaction); `probe_model()` never hardcodes a live model id — falls back to `"default"` (omit `--model`) if the probe fails |
| `merge_policy.py` | `evaluate_merge()` — fail-closed autonomy table: wiki T2/T3 auto-merges on green lint, skills/RES-weights are draft-only, T0 paths (`CLAUDE.md`, `AGENTS.md`, `wiki/principles/`) are suggestion-only, `tau-intent` blocks merge for its first 2 weeks even with green CI, protected paths and path traversal are normalized and blocked |
| `digest.py` | `parse_declared_cards()` reads the PR body's "Decisões que merecem pergunta" section; `independent_cards()` calls an injected `DiffReviewer`; `merge_cards()` dedups declared+independent by `(kind, location)`, caps at 3; `top_n_global()` picks a deterministic top 5 across PRs |
| `gh_adapter.py` | Maps a `MergeDecision` to one `GhRequest` (`merge` / `convert_to_draft` / `comment`) sent through an injected `GhTransport`; `block` sends nothing |

## Public API (import paths)

```python
from swarm_reports.dispatch.quota import ProviderQuota, QuotaWindow, UsageLedger, daily_budget
from swarm_reports.dispatch.routing import ProviderState, classify, eligible, route
from swarm_reports.dispatch.claims import ClaimStore, DispatchClaim, DuplicateLaunch
from swarm_reports.dispatch.priority import Job, fit_to_budget
from swarm_reports.dispatch.providers import ProviderCLI, Runner, SubprocessRunner, probe_model, redact
from swarm_reports.dispatch.merge_policy import RepoPolicy, PullRequest, MergeDecision, evaluate_merge, classify_path
from swarm_reports.dispatch.digest import DecisionCard, parse_declared_cards, independent_cards, merge_cards, top_n_global
from swarm_reports.dispatch.gh_adapter import GhRequest, GhTransport, prepare_request, apply_decision
```

Every provider/network/subprocess boundary is a `Protocol` (`Runner`,
`GhTransport`, `DiffReviewer`) so tests inject a fake and no test spawns a
real CLI or makes a network call.

## Config

`config/reports-policy.example.json` — repo allowlist (exact paths, glob
patterns), protected paths, `tau-intent` flag, quota thresholds, routing
order. No real credentials; the real file lives outside Git, path supplied
by the caller at runtime (`state_dir` pattern, same as the rest of
`src/reports`).

## Run the tests

```bash
PYTHONPATH=src/reports python3 -m pytest src/reports/tests/test_dispatch*.py -q
```

248 dispatch tests (`test_dispatch_*.py`), all against fakes/tmp_path — no
network, no subprocess to a real provider CLI, no GitHub call. Optional
mutation harness: `cd src/reports && python3 mutation_check.py`.

## Unresolved / decisions for the owner

See the PR-shaped "Decisões que merecem pergunta" below (this module has no
PR yet, per the brief, but the digest format is prepared here since this
module *is* the digest's own consumer):

1. **heuristic_with_ceiling** — `src/reports/swarm_reports/dispatch/claims.py:29` — `DEFAULT_RUNNING_TIMEOUT = timedelta(hours=2)`: is 2h the right ceiling before a "running" claim is considered abandoned and resumable? Too short double-launches a slow provider job; too long stalls a real failure until the next morning run.
2. **heuristic_with_ceiling** — `src/reports/swarm_reports/dispatch/quota.py:24` — `DEFAULT_UNKNOWN_BUDGET_PCT = 10.0`: the daily budget assumed for a provider that reports no quota at all. No source for this number beyond "conservative and nonzero" — needs a real anchor once Cursor/Codex quota probing (F0) reports back.
3. **spec_deviation** — `src/reports/swarm_reports/dispatch/routing.py:19` — `DEFAULT_STALE_AFTER = timedelta(hours=6)`: the design note does not specify a staleness window for a quota probe; 6h is a guess (covers one sleep cycle). Affects whether a provider silently keeps routing on an old reading.

## Not done here (owned by later phases)

- Wiring into `mathai-swarm report morning|evening` (F2/F4) and the root
  `swarm_reports` package/`pyproject.toml` (F1).
- Real `SubprocessRunner` invocation of an actual provider CLI end-to-end
  (needs F0's CLI inventory).
- Real `GhTransport` (`gh` CLI or GitHub API) implementation.
- Hermes cron wiring (explicitly out of scope, stays manual for two weeks).
