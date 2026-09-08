# AGENTS — hermes-identity

## Purpose
Keep Hermes persona + memory identical across machines without syncing sessions or secrets.

## Rules
1. Only edit/commit: `SOUL.md`, `memories/MEMORY.md`, `memories/USER.md`.
2. Never commit `~/.hermes/.env`, `auth.json`, session DBs, cache, or API keys.
3. On each machine, identity files are symlinks into a local clone of this repo.
4. After Hermes rewrites MEMORY/USER, run `hermes-sync-identity.sh push` before switching machines.
5. Before starting Hermes on a cold machine, run `hermes-sync-identity.sh pull`.

## Layout
```
~/src/hermes-identity/          # git clone
~/.hermes/SOUL.md               → ~/src/hermes-identity/SOUL.md
~/.hermes/memories/MEMORY.md    → ~/src/hermes-identity/memories/MEMORY.md
~/.hermes/memories/USER.md      → ~/src/hermes-identity/memories/USER.md
```

## Autenticar novo agente (A2A → ailla-hermes)

Onboarding de um peer ao Hermes A2A na box da Ailla. **Zero secrets neste repo** — tokens só em `~/.hermes/.env` de cada máquina / canal privado.

Hostname estável: `https://a2a.mathai.com.br` (Agent Card `ailla-hermes`).

### Na box (servidor) — Ailla / dono

1. Em `~/.hermes/.env` (nunca neste repo):
   - `A2A_PUBLIC_URL=https://a2a.mathai.com.br`
   - `A2A_PEER_TOKENS=nome:token,…` (um token por peer; preferido)
   - opcional: `A2A_TRUSTED_PEERS=nome1,nome2`
2. Novo peer: `openssl rand -base64 24` → acrescenta `nome:<token>` em `A2A_PEER_TOKENS` → reinicia `hermes gateway run`.
3. Entrega o token **só** por DM / password manager (não Linear, não PR, não SOUL/USER).

### No peer (cliente)

```yaml
a2a_agents:
  ailla-hermes:
    url: "https://a2a.mathai.com.br"
    auth: { type: bearer, token: "PEER_TOKEN_ENTREGUE_FORA_DESTE_FICHEIRO" }
    timeout: 120
```

Ferramentas: `a2a_discover`, `a2a_call`, `a2a_list`, `a2a_history`, `a2a_orchestrate`.

### Smoke

```bash
curl -sS https://a2a.mathai.com.br/.well-known/agent-card.json
# expect 200, name ailla-hermes, url https://a2a.mathai.com.br/
# pedidos A2A autenticados: Authorization: Bearer <PEER_TOKEN>
```

### Anti-padrões

- Tokens no vault / Linear / este repo
- URL `*.trycloudflare.com` como estável
- Tunnel Cloudflare numa conta diferente da zona DNS (erro 1033)
- Dois pollers Telegram no mesmo bot
- A2A remoto sem bearer

