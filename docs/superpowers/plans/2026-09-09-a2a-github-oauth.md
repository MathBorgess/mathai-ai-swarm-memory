# A2A external-agent OAuth implementation plan

> **Status:** implementado e validado na VPS em 09/09. Este plano registra a execução inicial; para reinstalação, promoção e recuperação, use `docs/operations/install-auth-broker-vps.md` e `MathBorgess/mathai-wiki` → `estudos/context-engineering/2026-09-09-a2a-oauth-broker-runbook-vps.md`.

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
- [x] Run the broker suite and shell verification, then commit the coherent change.

## Explicit limits

GitHub OAuth is not an OIDC issuer and does not provide a generic ID-token/JWKS validation path for these custom A2A scopes. The broker validates identity through the authenticated GitHub `/user` call and mints its own grant. The GitHub App, VM, DNS/Tunnel and deployment are owner-controlled manual operations; the broker-side Device Flow adapter and the client contract were implemented and exercised. A generic A2A harness still needs its own Device Flow adapter.
