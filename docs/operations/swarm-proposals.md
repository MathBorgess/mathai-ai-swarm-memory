"""S4 T2 proposals — draft PR into a disposable inbox, never the personal wiki.

This package is the Agent D slice. `build_router` is not mounted on the live
broker until Agent A injects DPoP/authorize and Agent B calls propose from MCP.
Do not treat a green test with FakeGitHub as production integration.

## What it does

`POST /v1/context/propose` accepts only `{namespace, title, body_markdown, sources}`
plus `Idempotency-Key`. The server derives `pesquisa/tcc/inbox/YYYY-MM-DD-<id>.md`
and `swarm/proposal-<id>`. The GitHub adapter, using a dedicated write token,
creates that branch from a configured base ref, writes the single inbox file,
and opens a **draft** pull request. There is no merge, no promotion to wiki T1/T0,
no write to `fontes/`, and no commit on `main`.

Auth mapping (`principal_id`, `workspace_id`, `scopes`, `classifications`,
`family_id`, `expires_at`) comes from the injected `authorize(request)` callable.
JSON from the caller cannot populate it. Scope required: `ctx:propose:pesquisa.tcc`.

Retries with the same workspace, principal, key and payload hash resume the
outbox and return the same `proposal_id` / branch / `pr_url`. A different payload
with the same key is `409`. HTTP errors after a GitHub write are not treated as
proof of absence: `ensure_*` looks up existing refs, files and draft PRs first.

## Configuration (operator, VPS — not in Git)

Use a **discard** repository. Never point this adapter at `MathBorgess/mathai-wiki`
or any vault that holds T1/T0 notes.

| Item | Rule |
|---|---|
| Token | Fine-grained PAT **or** GitHub App installation token created for this inbox repo only |
| Permissions | Contents: Read and write. Pull requests: Read and write. Nothing else |
| Forbidden | `delete`, admin, workflows, wiki, repo creation, using the Device Flow / broker session token |
| Repo | `owner/name` of the disposable inbox; `base_ref` is the configured review base, not a promotion target |
| Prefix | Closed: `pesquisa/tcc/inbox`. The adapter rejects any other prefix |
| Host | `https://api.github.com` only. Redirects are denied |

Suggested env names for the later A wiring (not read by this package today):

```text
AUTH_BROKER_PROPOSAL_GITHUB_TOKEN
AUTH_BROKER_PROPOSAL_REPO=owner/discard-t2-inbox
AUTH_BROKER_PROPOSAL_BASE_REF=t2
AUTH_BROKER_PROPOSAL_SQLITE=/var/lib/auth-broker/proposals.sqlite3
```

If the token is missing, the factory receives `github=None` and propose returns
`503` without inventing a pull-request URL.

SQLite is local to the VPS, like the pairing store. Do not commit the database.

## Wiring still owned by A/B

1. Agent A registers `app.include_router(build_router(authorize=..., store=..., github=..., repository=...))`
   using the DPoP verifier. Legacy pairing sessions and `a2a:message` must not
   gain `ctx:propose:*`.
2. Agent B's MCP client calls this route with `Idempotency-Key` and must not
   send path, branch, repo, or GitHub credentials.
3. Until that wiring exists, the route is absent from `api.py` / `main.py` on
   purpose. Features stay disabled rather than falling back to Hermes.

## Smoke (opt-in, discard repo only)

Automated tests use FakeGitHub and httpx MockTransport. A live smoke is optional
and must target a throwaway repository whose default contents are empty inbox
scaffolding, never `mathai-wiki`.

```bash
# from src/auth-broker, after A has mounted the router in a staging process
# Do not export the token into the shell history; use a private env file.
curl -sS -X POST "$BROKER/v1/context/propose" \
  -H "Authorization: DPoP ..." \
  -H "Idempotency-Key: smoke-$(date +%s)" \
  -H "Content-Type: application/json" \
  --data '{"namespace":"pesquisa.tcc","title":"smoke","body_markdown":"unverified","sources":[]}'
```

Confirm the PR is draft, the file is under `pesquisa/tcc/inbox/`, and nothing
landed on `wiki/`, `fontes/`, or the base branch. Close the PR without merging.

## T2 is not promotion

The rendered note and the draft PR body state that claims are unverified and
that human confirmation is pending. Operators review on GitHub. Merging into a
curated layer, copying into the personal wiki, or running a scheduler remains a
human decision outside this package.
