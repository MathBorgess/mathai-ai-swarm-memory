# Daily reports — F6 core: read-only discovery of agents outside the flow

Read-only discovery since the last successful per-source checkpoint, for
agents the owner runs between morning/evening rounds. Package:
`src/reports/swarm_reports/discovery/`. Pure stdlib (`gh` CLI via subprocess,
`urllib` for Linear, filesystem for `wiki/log.md`) — no new dependency added
to `src/reports/pyproject.toml`.

**F6 owns discovery only.** Wiring "since the last round" into the morning
report, the "fora do plano — classificar?" evening prompt, and digest cards
for these agents' PRs is F2/F4/F5 runtime integration and is not implemented
here.

## Layout

| Module | Role |
|---|---|
| `discovery.models` | `DiscoveryItem`, `DiscoveryError`, `DiscoveryBatch` — JSON-serializable |
| `discovery.checkpoints` | `state_dir/discovery-state.json`: per-source `cursor` + bounded `seen_ids` dedup ring, atomic write (`0600`), `fcntl.flock` cross-process lock |
| `discovery.sources.github` | `gh api --method GET` (argv list, no shell), exact-match repo allowlist, manual bounded pagination, merge vs. update events kept separate |
| `discovery.sources.linear` | Hardcoded read-only GraphQL `query` (no mutation), token-from-env or configured read-only command adapter |
| `discovery.sources.wiki_log` | `wiki/log.md` table rows, content-hash dedup (rotation-safe), symlink-escape guard |
| `discovery.orchestrator` | `run_discovery(config)` — merges sources, isolates failures, excludes own ledger ids, deterministic ordering |

## Public API

```python
from pathlib import Path
from swarm_reports.discovery import DiscoveryConfig, run_discovery
from swarm_reports.discovery.sources.github import GithubConfig
from swarm_reports.discovery.sources.linear import LinearConfig
from swarm_reports.discovery.sources.wiki_log import WikiLogConfig

config = DiscoveryConfig(
    state_dir=Path("/var/lib/mathai-swarm/reports"),  # outside Git
    github=GithubConfig(repos=("MathBorgess/mathai-ai-swarm-memory",)),
    linear=LinearConfig(token_env="LINEAR_API_TOKEN"),
    wiki_log=WikiLogConfig(
        log_path=Path("~/github/mathai-wiki/wiki/log.md").expanduser(),
        wiki_root=Path("~/github/mathai-wiki/wiki").expanduser(),
    ),
    known_ledger_action_ids=frozenset(),  # this run's own already-ledgered ids
)
batch = run_discovery(config)  # DiscoveryBatch(items, errors, checkpoints)
```

`DiscoveryItem`: `kind, id, source, url, title, observed_at, classification (None default), meta`.
`kind` values: `pr_merged`, `pr_updated`, `commit`, `linear_issue`, `wiki_log_entry`.

## Contracts implemented

- **Read-only.** GitHub: `gh api --method GET` only. Linear: hardcoded `query`
  string, asserted at import time to contain no `mutation` (transport is
  still an HTTP POST — GraphQL requires it even for reads).
- **Repo allowlist.** `GithubConfig.repos` is the allowlist; every entry is
  regex-validated (`owner/name` only) before it reaches `gh api` argv, so a
  malformed/malicious entry (`; rm -rf`, `../../etc/passwd`) raises before any
  subprocess call.
- **Secrets.** Linear token is read from `os.environ[token_env]` and only
  ever placed in an HTTP header; never logged, returned, or included in an
  error message.
- **Bounded fetch.** `max_pages`, `per_page`/`first`, and `timeout_seconds`
  are all config fields with small defaults (3 pages, 30/50 rows, 20s).
- **Truncation.** If more pages remain after `max_pages`, the source result
  is `truncated=True` and the checkpoint `cursor` is left unchanged (the
  orchestrator only advances a source's cursor when its pass wasn't
  truncated) — the next run resumes from the same point instead of skipping
  unvisited pages.
- **Source failure isolation.** A `SourceError` (or any unexpected exception,
  defensively) becomes a `DiscoveryError` and that source's checkpoint bucket
  is left untouched; other sources still run and their checkpoints still
  advance.
- **Dedup / restart safety.** Each source checkpoint keeps a bounded
  (`MAX_SEEN_IDS=500`) ring of previously-emitted ids. GitHub additionally
  dedups within a single run (id-based) so overlapping/duplicate pages don't
  double-emit. `wiki_log` dedups by content hash of the row, not line number
  or byte offset, so log rotation or an append-only replay of already-seen
  rows does not resurface them; only genuinely new rows pass through.
- **No fabricated ids.** Linear items pass through the `identifier` returned
  by the API/command verbatim; an empty result is "found nothing", never
  invented.
- **Merge vs. changed events kept separate.** A merged PR emits only
  `pr_merged` (id suffix `:merged`), never also a `pr_updated` for the same
  underlying change, so F5's digest doesn't get two cards for one PR.
- **`classification` defaults to `None`.** Discovery never classifies;
  `None` means "info only, no scope penalty" until F4's evening form assigns
  one.
- **Own-ledger exclusion is explicit only.** `known_ledger_action_ids` is a
  set of exact item ids to drop; there is no author-name heuristic anywhere
  in this package — outside activity is never attributed by name matching.
- **wiki/log.md is read outside Git and validated against path escape.**
  `WikiLogConfig.log_path` must resolve inside `WikiLogConfig.wiki_root`
  (checked via `Path.resolve()`); a symlink pointing outside the root raises
  `SourceError` before the file is read. Reads are bounded to `max_bytes`
  (default 2 MB).
- **Deterministic ordering.** `DiscoveryBatch.items` is sorted by
  `(observed_at, source, kind, id)`, so same-timestamp ties across sources
  are stable and reproducible.
- **State store.** `state_dir` is an explicit parameter (never assumed to be
  inside the repo), atomic replace (`tempfile` + `os.replace`) at mode
  `0600`, and a cross-process `fcntl.flock` exclusive lock around the whole
  read-modify-write in `run_discovery`.

## GitHub adapter shape

Manual bounded pagination (not `gh api --paginate`, which has no page cap):
`gh api -i --method GET repos/{repo}/pulls -f state=all -f sort=updated
-f direction=desc -f per_page=N -f page=P`, `-i` to read the `Link` response
header and detect `rel="next"`. Same shape for `repos/{repo}/commits` with
`-f since=<cursor>` when a cursor exists.

## Linear adapter shape

Token adapter POSTs `{"query": DISCOVER_QUERY, "variables": {"after", "since"}}`
to `https://api.linear.app/graphql` with `Authorization: <token>`. Command
adapter runs a configured read-only argv (no shell) that must print a JSON
array of `{identifier, title, url, updatedAt, state}`. Both are injectable
(`opener` / `command_runner`) for offline testing — no real HTTP call is made
in tests.

## Tests

```bash
cd src/reports && python3 -m pytest -q
```

75 tests pass (48 pre-existing F1 metrics + 27 new discovery tests), fully
offline: `gh` and Linear HTTP calls are replaced with injected fake
runners/openers, never a real subprocess or socket. Coverage: duplicate
overlapping pages (in-run dedup), truncated/incomplete fetch (cursor does not
advance), restart with an unchanged log (nothing re-reported), log rotation
with replayed rows (only the genuinely new row surfaces), source isolation
(one source's `SourceError` or unexpected exception does not stop the run or
touch its own checkpoint), malicious repo input (regex rejection before any
subprocess call, including path traversal), read-only API shape (query has
no `mutation`, GitHub calls are all `--method GET`), and unknown
classification (defaults to `None`).

## Known gaps / open questions

- **No live-API integration test.** Everything is exercised through injected
  fakes; the first real `gh api` / Linear HTTP call happens at F6 runtime
  wiring, not in this package. Recommend a manual smoke run against a
  throwaway low-traffic repo before relying on this in the morning report.
- **Truncation can stall if genuinely stuck.** If a repo has more unvisited
  pages than `max_pages` on every run (e.g. `max_pages` set too low for a
  very active period), the cursor never advances and the same truncated
  window is re-fetched (deduped, so no duplicate cards, but no progress
  either). Acceptable for a bounded MVP; raise `max_pages` or add
  alerting-on-repeated-truncation later if this becomes a real repo's
  pattern.
- **Linear GraphQL shape is unverified against the live API.** The query
  (`DiscoverChangedIssues`, `filter: { updatedAt: { gt: $since } }`,
  `orderBy: updatedAt`) is written from the public Linear GraphQL docs
  conventions, not confirmed by an actual authenticated call in this session
  — no token was available. Verify field/argument names against
  `https://api.linear.app/graphql`'s schema before first production use.
- **`wiki_log` id/date extraction assumes the current table shape**
  (`| Date | Op | Slug | Pages | Note |`). If the table's column order or
  header text changes, `_is_table_row`'s header-skip check
  (`cells[0].lower() == "date"`) and the `observed_at`/`title` extraction
  need updating together.
- **No `WikiLogConfig` default path.** F2/F6-runtime integration must supply
  the owner's actual `mathai-wiki` clone path; this package does not guess
  or hardcode it (per "own only `src/reports/...`", no vault path assumed).
