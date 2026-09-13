# Operação — connector MCP remoto (`/mcp`)

URL pública prevista: `https://a2a.mathai.com.br/mcp` (Streamable HTTP).
OAuth authorization code + PKCE é outro pacote (`app/mcp_oauth/`); este guia
só descreve o transporte MCP e como os harnesses apontam a URL.

## O que esta fatia entrega

- Pacote `app/remote_mcp/`: ASGI Streamable HTTP do SDK `mcp>=2.2,<3`.
- Tools `query`, `resolve`, `ask`, `propose` só se o callable correspondente
  for injetado; `capabilities` existe sempre.
- `authorize(request)` injetado; principal confiável via contextvar por
  request. O caller não escolhe `principal_id` / `workspace_id`.
- `propose` exige `idempotency_key` no argumento da tool.

Não liga `api.py` / `main.py`. Testes usam authorize e handlers fake com o
cliente SDK real (`ClientSession` + `streamable_http_client` + uvicorn).
Isso prova o protocolo MCP, não OAuth GitHub, ask isolado, Hermes ou deploy.

## Wiring FastAPI (interface exata)

`Mount("/mcp", …)` não serve: `app.mount("/mcp", remote.app)` vira `/mcp/mcp`;
`app.mount("/mcp", remote.mount_app)` responde **307** em `POST /mcp` →
`/mcp/` e o cliente MCP quebra.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.remote_mcp import TransportSecuritySettings, attach_mcp, build_remote_mcp

remote = build_remote_mcp(
    authorize=oauth.authorize,  # mapping já existente; não importar OAuth daqui
    query=...,
    resolve=...,
    ask=...,
    propose=...,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["a2a.mathai.com.br", "a2a.mathai.com.br:*"],
        allowed_origins=["https://a2a.mathai.com.br"],
    ),
    host="a2a.mathai.com.br",
)

@asynccontextmanager
async def lifespan(app):
    async with remote.lifespan(app):
        # async with existing_lifespan(app):  # se o broker já tiver lifespan
        yield

app = FastAPI(lifespan=lifespan, redirect_slashes=False)
attach_mcp(app, remote)
```

O lifespan **do host** tem de entrar em `remote.session_manager.run()`
(`remote.lifespan`). Lifespan de sub-app montado não corre. Servir só o
transporte: `uvicorn` em `remote.app` (path público `/mcp`).

Handlers: `fn(principal, …)` — principal injetado, nunca argumentado pelo
cliente. `propose(..., idempotency_key=…)`.

## Harnesses (URL + OAuth)

Cursor, Claude Code e Codex aceitam MCP remoto por URL e OAuth no browser.
Consentimento não é tool.

### Cursor

Docs: https://cursor.com/docs/mcp

```json
{
  "mcpServers": {
    "mathai-swarm": {
      "url": "https://a2a.mathai.com.br/mcp"
    }
  }
}
```

OAuth no browser. Redirects fixos do Cursor a registrar no AS (quando o
cliente não for DCR):

- Desktop: `http://localhost:8787/callback`
- Web / Agents: `https://www.cursor.com/agents/mcp/oauth/callback`

Sem bearer no `mcp.json`.

### Claude Code

Docs: https://code.claude.com/docs/en/mcp

```bash
claude mcp add --transport http mathai-swarm https://a2a.mathai.com.br/mcp
```

OAuth: `/mcp` na sessão → Authenticate. JSON precisa de `"type": "http"`
(alias `streamable-http`). Callback default em porta efêmera
(`http://localhost:PORT/callback`); `--callback-port` se o AS exigir URI fixa.

### Codex

Docs: https://learn.chatgpt.com/docs/extend/mcp?surface=cli

```toml
[mcp_servers.mathai-swarm]
url = "https://a2a.mathai.com.br/mcp"
auth = "oauth"
```

`codex mcp login mathai-swarm --oauth-client-registration dcr`. Callback típico `http://127.0.0.1/callback` (porta efémera ou `oauth.callback_port`). Não há Client ID Metadata Documents.

## Testes

```bash
cd src/auth-broker
python3 -m pip install -e ".[test]"
python3 -m pytest tests/test_remote_mcp.py
```

## O que não fazer

- JSON-RPC artesanal; login como tool; principal/workspace no argumento.
- Encaminhar o bearer do harness ao Hermes.
- Tratar estes testes como prova de produção, GitHub real ou ask isolado.
