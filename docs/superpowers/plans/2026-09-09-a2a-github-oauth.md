# A2A external-agent OAuth implementation plan

## Goal

Allow a compatible external A2A client to start with only `https://a2a.mathai.com.br`, discover a structured OAuth contract, and exchange messages through the broker without receiving the Hermes bearer.

## Decisions

- Authorization provider: GitHub OAuth (`https://github.com/login/oauth`); no GitHub token is forwarded to Hermes. GitHub is not an OIDC issuer: identity is validated through GitHub API and the broker mints its own session.
- Declared permissions are exactly `a2a:discover`, `a2a:message`, and `a2a:history`.
- Default short lifetime is the existing broker default: one hour maximum for an approved agent.
- The public card is data-only: OAuth metadata is machine-readable and contains no auth-broker instructions.
- Broker remains the policy/proxy point and translates the external authorization into its private Hermes credential.

## Work

- [x] Add a versioned public Agent Card model and `/.well-known/agent-card.json` response.
- [x] Expose OAuth metadata and exact scope contract without changing the existing pairing boundary.
- [x] Test card shape, origin, scopes, and absence of natural-language broker instructions.
- [x] Document GitHub OAuth and harness limitations, including why arbitrary Python POSTs to external URLs are not a safe authentication primitive.
- [ ] Run the broker suite and shell verification, then commit the coherent change.

## Explicit limits

GitHub OAuth is authorization-code based and does not provide a generic OIDC issuer for these custom A2A scopes. This change publishes the discovery contract; deployment still needs a broker-side GitHub token exchange/introspection adapter and client support for the advertised flow. Cloudflare, VM, DNS, OAuth application registration, and deployment remain manual operations.
