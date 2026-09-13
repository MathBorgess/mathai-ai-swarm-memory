# Swarm remote MCP OAuth (authorization code)

Pacote isolado `app/mcp_oauth/`. Não altera `api.py`, `main.py`, SQLite de pairing, CLI nem `pyproject.toml`. O agente de integração monta o router depois.

Isto não é o transporte Streamable HTTP em `/mcp`. Só o provedor OAuth e `authorize(request)` para o recurso `https://a2a.mathai.com.br/mcp`.

DCR é escolha de compatibilidade com harnesses (Cursor/Claude/Codex), não uma exigência do MCP 2025-11-25 (que prefere Client ID Metadata Documents). Metadata segue [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) e [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414). Fluxo: authorization code + PKCE S256 ([MCP Authorization 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)).

## Interface para o wiring

```python
from app.mcp_oauth import build_router
from app.mcp_oauth.github import HttpGitHubAuthorizationCode

github = HttpGitHubAuthorizationCode(
    client_id=...,
    client_secret=...,
    callback_url="https://a2a.mathai.com.br/mcp/oauth/callback",
)
oauth = build_router(
    database_path=database_path,
    github=github,
    signing_key=es256_pem,
    workspace_id="personal",
    public_url="https://a2a.mathai.com.br",
)
app.include_router(oauth.router)
principal = oauth.authorize(request)
# principal_id, workspace_id, scopes, classifications, family_id, expires_at
```

`authorize` recusa `DPoP` e sessões A2A opacas. Audience/resource exactos: `https://a2a.mathai.com.br/mcp`. Recheca grants e papel a cada chamada; família revogada ou grants vazios falham fechado. Sem auto-grant.

## Rotas

| Método | Caminho |
|---|---|
| GET | `/.well-known/oauth-protected-resource` e `.../mcp` |
| GET | `/.well-known/oauth-authorization-server` |
| POST | `/mcp/oauth/register` |
| GET | `/mcp/oauth/authorize` |
| POST | `/mcp/oauth/consent` |
| GET | `/mcp/oauth/callback` |
| POST | `/mcp/oauth/token` |
| POST | `/mcp/oauth/revoke` |

GitHub só nas URLs fixas `https://github.com/login/oauth/authorize`, `https://github.com/login/oauth/access_token` e `https://api.github.com/user`. Identidade = id numérico. O token GitHub não é persistido nem devolvido. Callback do OAuth App: `https://a2a.mathai.com.br/mcp/oauth/callback`. Não coloque Cloudflare Access neste hostname.

## Autoridade de grants

`SqlitePairingStore` continua a única tabela de principals/grants. O provedor resolve o sujeito GitHub com `list_principals()` (até existir lookup por `github_subject`). Sem principal ou sem interseção de grants → `access_denied`. JWK/DPoP não entram neste ingresso.

Tabelas próprias no mesmo ficheiro SQLite (`mcp_oauth_*`): clients DCR, transações CSRF, códigos, famílias de refresh. Códigos e refresh só como SHA-256. Rotação com revogação da família em replay de geração antiga. Códigos de uso único, atómicos.

## Limites de DCR e redirect

HTTPS sem fragmento/userinfo, ou HTTP só em loopback literal (`127.0.0.1`, `localhost`, `::1`). Comparação exacta com o registado. Corpo ≤ 16 KiB; parâmetros de formulário/query duplicados são rejeitados. Registos não autenticados: 32/hora (injectável nos testes), cliente expira em 30 dias, `token_endpoint_auth_method=none`.

Consentimento no browser identifica cliente, redirect e scopes; CSRF em cookie HttpOnly + campo hidden, `state` do GitHub = transação.

## Testes

```bash
cd src/auth-broker
python3 -m pytest tests/test_mcp_oauth.py tests/test_mcp_oauth_adversarial.py
```

Sem rede. Cobrem duas identidades GitHub, ausência de grants, replay, expiry, PKCE/client/resource/redirect, CSRF, revoke/downgrade, redirect malicioso e DCR limitado.

## Fora desta fatia

Wiring em `create_app`/`main`, transporte `/mcp`, prova com GitHub real ou harness, CIMD, rate-limit por IP, billing. Não faz deploy.
