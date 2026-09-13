# Swarm auth, grants and DPoP (broker)

This is the Agente A delivery: S1 grants/CLI plus the server half of S2. It does
not install memory, proposals, MCP, ACL graphs, SaaS, or a supervisor. It does
not change VPS, DNS or secrets.

## What is implemented

- Additive SQLite v2 on the existing pairing/session store: principals, grants,
  grant audit, token families, refresh hashes and DPoP replay. Version 3 adds a
  cached GitHub login and unkeyed principals (empty JWK columns, never a
  generated dummy key). Reopening a migrated database does not duplicate audit
  rows. Legacy `broker_sessions`, nonces and pairing audit stay intact.
- CLI `mathai-swarm` (store path required, no default file):
  `allow @login`, `principal add|list`, `grant set|list|revoke`,
  `token revoke --family`. No HTTP or MCP route administers policy.
- GitHub Device Flow bound to a registered `principal_id` + JWK thumbprint.
  Effective scopes = requested ∩ active grants ∩ role eligibility. Grammar in
  this slice: `ctx:read:pesquisa.tcc` and `ctx:propose:pesquisa.tcc` only.
- Access tokens are ES256 JWTs with `iss`, `aud`, `sub` (principal_id),
  `workspace_id`, `scope`, `classifications`, `iat`, `nbf`, `exp`, `jti`,
  `sid` (family) and `cnf.jkt`. Default lifetime 5 minutes, cap 1 hour.
  Classifications are computed from role; clients cannot set them.
- Refresh rotation with hashed tokens, one successor, skip-generation reuse
  detection ([RFC 9700](https://www.rfc-editor.org/rfc/rfc9700) family policy).
  Replaying any prior refresh, including the immediate predecessor, revokes the
  family. DPoP per [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449). Canonical
  `htu` comes from `public_url`, never from forwarded Host headers.
- `GET /v1/context/capabilities` reflects installed operations only. This
  delivery advertises `capabilities`. Query/resolve/propose authenticate, then
  return **503** until C/D routers are registered. Ctx grants never open the
  Hermes proxy.

## Legacy mode

Empty Device Flow (`{}`, no DPoP) still issues an opaque `Bearer` session with
the three `a2a:*` scopes and may call Hermes. That path is documented and
tested. It does not gain ctx scopes by migration and cannot call
`/v1/context/capabilities`. Do not use it as a fallback when a swarm token
fails.

## Configuration (optional; VPS keeps working without it)

Existing required variables are unchanged. Swarm issuance also needs:

```dotenv
AUTH_BROKER_WORKSPACE_ID=personal
AUTH_BROKER_JWT_SIGNING_KEY_PATH=/absolute/path/to/es256-private.pem
# or AUTH_BROKER_JWT_SIGNING_KEY with the PEM contents
AUTH_BROKER_PUBLIC_URL=https://a2a.mathai.com.br
# optional, default 300, maximum 3600:
# AUTH_BROKER_ACCESS_TOKEN_LIFETIME_SECONDS=300
```

There is no default signing key. Without workspace id and key, legacy a2a
Device Flow still starts; the DPoP/ctx flow returns 503.

## CLI

```bash
mathai-swarm --store /absolute/path/broker.sqlite3 allow @calegario \
  --role advisor --scope ctx:read:pesquisa.tcc \
  --scope ctx:propose:pesquisa.tcc --ttl 30
mathai-swarm --store /absolute/path/broker.sqlite3 principal add \
  --id advisor-01 --subject 12345 --role advisor --jwk '{"kty":"EC",...}'
mathai-swarm --store /absolute/path/broker.sqlite3 grant set \
  --principal advisor-01 --scope ctx:read:pesquisa.tcc --days 30
mathai-swarm --store /absolute/path/broker.sqlite3 token revoke --family <sid>
```

`allow` resolves `GET https://api.github.com/users/{login}` (the `@` is
optional), stores the numeric account id as identity, caches the current
login, and creates principal `github:{id}` without a user JWK. Role, scopes
and TTL are validated before the GitHub call; principal and grants are written
in one transaction. Cached login is not an authorization key:
`SqlitePairingStore.get_principal_by_github_subject` is the stable lookup for
OAuth and Device Flow. It prefers the canonical `github:{id}` row when that
id exists, including after DPoP key enrollment, so grants do not silently
switch to an older keyed harness for the same GitHub account. A single
non-canonical row is selected explicitly; several active legacy rows for the
same subject fail closed. If the username later belongs to a different
account id, a new `allow` grants that new id and does not move the old row.

Unkeyed principals cannot mint DPoP families until the operator enrolls a
public JWK (`enroll_principal_key`). `principal add --jwk` and existing keyed
Device Flow stay as they are. HTTP Device Flow still lives in `api.py`.
DPoP Device Flow accepts any granted principal whose GitHub subject and
enrolled JWK match the presented proof; it is not limited to
`GITHUB_ALLOWED_USER_ID`. Unkeyed DPoP starts are `access_denied`. Legacy
empty Device Flow (`{}`, no DPoP) remains owner-id-only and issues `a2a:*`
Bearer sessions.

When workspace id and a JWT signing key are configured, `create_app` also
mounts the remote MCP OAuth provider (`/.well-known/oauth-*` and
`/mcp/oauth/*`). Resource/audience is `{public_url}/mcp`. The GitHub OAuth
App must allow the web callback `{public_url}/mcp/oauth/callback` in addition
to Device Flow. DCR permits exact `http://localhost` (no subdomains), numeric
loopback, and `https` redirects; credentials, fragments and wildcards are
rejected. MCP Bearer tokens are a distinct audience from DPoP/A2A.

Roles: `owner`, `self-harness`, `advisor`. Role limits eligibility only.
Advisor (and this slice of owner/self-harness) may receive only the two ctx
scopes above. TTL is 1–90 days, validated before SQL. `ctx:write:wiki`,
wildcards and `a2a:message` grants are rejected.

## HTTP contract used by Agente B

| Route | Notes |
|---|---|
| POST `/v1/oauth/github/device/start` | `{principal_id, scopes}` + `DPoP` |
| POST `/v1/oauth/github/device/poll` | same key; tokens use `token_type: DPoP` |
| POST `/v1/oauth/token` | `grant_type=refresh_token` + DPoP, no `ath` |
| POST `/v1/oauth/revoke` | `Authorization: DPoP` + proof with `ath` |
| GET `/v1/context/capabilities` | DPoP access token |

OAuth errors use a stable `error` field: `authorization_pending`, `slow_down`,
`access_denied`, `invalid_grant`, `invalid_dpop_proof`. Resource 401 is missing
or invalid DPoP/JWT; 403 is a valid token outside scope (do not refresh-loop);
503 means C/D is not wired.

## Revocation round-trip

Access tokens are JWTs, not fully stateless. Every resource request loads
`sid` and checks `token_families.revoked_at`. Family revocation is therefore
immediate. Grant changes apply on the next mint/refresh. An already-issued
access token keeps its snapshot scopes until `exp`, unless the family is
revoked.

Refresh never extends family/grant expiry. Clients must serialize refresh per
credential. Presenting the current refresh token issues one successor.
Presenting any prior generation, including the immediate predecessor,
revokes the family (`invalid_grant`). Concurrent refresh of the same
current token is serialized: one successor, the other is reuse.

## Wiring of the C/D routers

`create_app(..., context_router_factory=, proposal_router_factory=)` takes the
C/D factories, each called once as `factory(authorize=authorize)`.
`authorize(request)` returns `principal_id`, `workspace_id`, `scopes`,
`classifications`, `family_id`, `expires_at` from a verified DPoP access token,
never from the caller's JSON.

`api.py` delegates to the mounted endpoints instead of `include_router`:
`/v1/context/query` has to keep dispatching legacy Bearer sessions to Hermes,
and a mounted route would shadow that branch. Delegation also keeps
`authorize()` running exactly once per request — calling it in `api.py` and
again inside the router would consume the same DPoP `jti` twice and self-reject
as a replay.

`/v1/context/capabilities` lists `query`/`resolve` only with a context factory
and `propose` only with a proposal factory. Without them the routes answer 503,
and no ctx grant ever opens the Hermes proxy.

`main.py` installs each package from configuration, never by default:

```text
AUTH_BROKER_CONTEXT_SQLITE=/var/lib/auth-broker/context.sqlite3

AUTH_BROKER_PROPOSAL_SQLITE=/var/lib/auth-broker/proposals.sqlite3
AUTH_BROKER_PROPOSAL_REPO=owner/discard-t2-inbox
AUTH_BROKER_PROPOSAL_BASE_REF=t2
AUTH_BROKER_PROPOSAL_GITHUB_TOKEN=<dedicated write credential>
```

The context manifest is ingested out of band by the operator; the broker
process never indexes a vault. A partially configured proposal block is a
startup error, not a permissive fallback.

`app/context/query.py` checks `expires_at` against the wall clock, so the
broker's injected `clock` does not reach it. That only matters to tests: the
integration smoke runs on a live clock instead of a frozen one.

## Limits

- One workspace per broker process, configured by the operator.
- Remote MCP Streamable HTTP is mounted at `/mcp` when `mcp_github` is set
  together with a signing key and workspace. OAuth metadata and `/mcp/oauth/*`
  share that issuance configuration.
- No Graphiti, no KA Builder, no SaaS.
- Do not commit `.env`, PEMs, SQLite files or refresh tokens.
