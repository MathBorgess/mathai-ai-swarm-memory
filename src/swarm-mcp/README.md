# mathai-swarm-mcp

Adaptador MCP **local** (stdio) para o origin swarm `https://a2a.mathai.com.br`.
É a metade cliente de S2: Device Flow + DPoP + keystore + tools `capabilities` /
`query` / `resolve` / `propose`. Não é o broker, não é o grafo e não escreve na wiki.

Pacote Python >=3.12, entrypoints `mathai-swarm-mcp` e `python -m mathai_swarm_mcp`.
Usa o [SDK MCP oficial](https://github.com/modelcontextprotocol/python-sdk) (`mcp>=2.2`).
Não publique este pacote; não faça login real nem cadastre principal real nesta entrega.

## O que este pacote prova

Testes contra um **HTTP fake** do contrato v1. Isso valida o adapter, **não** a
integração com A (servidor OAuth/DPoP), C (query/resolve) ou D (propose). Essa
integração ainda falta: o origin real ainda não instala as rotas ctx, e o
adapter responde erro explícito quando `operations` não inclui a ferramenta.

## Setup (venv local, sem MCP global)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/mathai-swarm-mcp --help
.venv/bin/python -m mathai_swarm_mcp --help
```

Ordem operacional (depois que A existir; **não executar agora contra produção**):

1. `mathai-swarm-mcp show-key --origin https://a2a.mathai.com.br --principal <id>`
   imprime JWK pública e thumbprint (`jkt`) para o dono cadastrar o principal no CLI de A.
2. `mathai-swarm-mcp login --origin https://a2a.mathai.com.br --principal <id>`
   mostra `verification_uri` e `user_code` **neste terminal**. Consentimento nunca
   volta como ferramenta do modelo.
3. Configurar o harness com o **binário** e o origin. Jamais um bearer.

## Configuração de exemplo (binário + origin, sem bearer)

Cursor `mcp.json`:

```json
{
  "mcpServers": {
    "mathai-swarm": {
      "command": "/absolute/path/to/.venv/bin/mathai-swarm-mcp",
      "args": [
        "serve",
        "--origin",
        "https://a2a.mathai.com.br",
        "--principal",
        "advisor-01"
      ]
    }
  }
}
```

Claude Code:

```json
{
  "mcpServers": {
    "mathai-swarm": {
      "command": "/absolute/path/to/.venv/bin/mathai-swarm-mcp",
      "args": ["serve", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"]
    }
  }
}
```

Codex:

```toml
[mcp_servers.mathai-swarm]
command = "/absolute/path/to/.venv/bin/mathai-swarm-mcp"
args = ["serve", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"]
```

Não cole access token, refresh token, DPoP nem `Authorization` nesses arquivos.
Não rode `mcp install` contra a configuração global do dono nesta fatia.

## Limites

- Origin HTTPS allowlist do valor de `--origin`. Redirects desligados. Sem discovery
  que mude o host de confiança. URLs de consentimento só `https://github.com/login/device`.
- HTTP de teste local só por injeção de `httpx` transport nos testes — não há flag
  de produção que aceite `http://`.
- Chave e refresh no keystore OS (`keyring`). Backend de arquivo/fail é recusado.
  Access token só em memória. Sem backend seguro, o CLI falha de forma explícita.
- Refresh single-flight por credencial. Um retry após 401; 403 não entra em loop.
  Timeout no refresh rotacionado **não** reutiliza o refresh anterior; é preciso
  `login` de novo (ver o guia operacional).
- `invalid_grant` pede login no terminal, sem abrir navegador em loop.
- Tools só executam operações listadas em `GET /v1/context/capabilities`.
  `logout`/`revoke` são CLI. Prompt opcional `load_authorized_slice`.
- Envelope e receipt são dados já autorizados. Resolve encaminha handles.
  Propose envia `Idempotency-Key` e a preserva no retry HTTP.
- Sem APIs de dispatch, scheduler, shell, ou administração de outros principais.

Contratos consumidos: `docs/handoffs/2026-09-11-swarm-contract.md` (HTTP v1, DPoP
RFC 9449, refresh RFC 9700, envelope S3, propose S4) e
`docs/handoffs/2026-09-11-agent-b-mcp.md`.

Operação: [docs/operations/swarm-mcp.md](../../docs/operations/swarm-mcp.md).
