# mathai-ai-swarm-memory

Base privada para a camada de contexto dos agentes do dono (`MathBorgess/mathai-ai-swarm-memory`). Ela mantém a identidade Hermes, o broker A2A e o connector MCP remoto. Não é o vault: o conhecimento compilado continua em `MathBorgess/mathai-wiki`.

O clone local pode permanecer em um diretório `mathai-context-engine`; só o remoto GitHub muda. `SOUL.md` e `memories/*` na raiz são links de compatibilidade — não os substitua por cópias.

## Comece aqui

1. Leia `CLAUDE.md` para o contrato de trabalho e segurança.
2. Leia `wiki/index.md` e a ADR relevante antes de alterar desenho ou operação.
3. Entre no componente: `src/hermes-identity/` ou `src/auth-broker/`.

## Layout

```text
src/hermes-identity/  identidade sincronizada e script de compatibilidade
src/auth-broker/      broker A2A com GitHub Device Flow e proxy privado para Hermes
wiki/                 decisões, arquitetura e roadmap deste repositório
docs/                 plano de implementação executável
```

## Estado atual

O sync de identidade funciona. O broker OAuth está implantado: o Agent Card público em `https://a2a.mathai.com.br` inicia GitHub Device Flow e o broker encaminha a mensagem autenticada a Hermes apenas pelo loopback privado. O fluxo antigo de pairing por Cloudflare Access está desativado; seus endpoints retornam `404` quando as variáveis de Access não estão configuradas.

Para repetir, auditar ou recuperar a instalação da VPS, comece por [docs/operations/install-auth-broker-vps.md](docs/operations/install-auth-broker-vps.md). O procedimento de operação já validado, incluindo staging, promoção, restart e cleanup, está na wiki privada: `MathBorgess/mathai-wiki` → `estudos/context-engineering/2026-09-09-a2a-oauth-broker-runbook-vps.md`. Nenhum dos dois documentos contém valores de segredos.

O remoto GitHub é `MathBorgess/mathai-ai-swarm-memory`. Clones e links locais podem manter o diretório histórico; o origin aponta ao novo nome. Um serviço vendável é meta futura: esta entrega não adiciona billing, multitenancy ou scheduler.

O connector público previsto é `https://a2a.mathai.com.br/mcp` (Streamable HTTP + OAuth no navegador). Device Flow+DPoP permanece o caminho do operador. Não trate esta árvore como prova de deploy, login real de harness ou qualidade do Hermes.

### Harnesses (URL + OAuth)

Documentação: [Cursor](https://cursor.com/docs/mcp), [Claude Code](https://code.claude.com/docs/en/mcp), [Codex CLI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Callbacks são redirects de browser, não fetches do servidor. DCR é compatibilidade de harness; CIMD não está implementado.

Cursor (`~/.cursor/mcp.json` ou project MCP):

```json
{
  "mcpServers": {
    "mathai-swarm": {
      "url": "https://a2a.mathai.com.br/mcp"
    }
  }
}
```

Redirects fixos: desktop `http://localhost:8787/callback`; cloud `https://www.cursor.com/agents/mcp/oauth/callback`. Sem bearer no JSON.

Claude Code:

```bash
claude mcp add --transport http mathai-swarm https://a2a.mathai.com.br/mcp
```

Na sessão: `/mcp` → Authenticate.

Codex:

```toml
[mcp_servers.mathai-swarm]
url = "https://a2a.mathai.com.br/mcp"
auth = "oauth"
```

```bash
codex mcp login mathai-swarm --oauth-client-registration dcr
```

Ask isolation pins Hermes `de2d6a1b93508463c31434c1ae067e204af81238` (`src/auth-broker/ask-worker/HERMES_PIN`). Operator must `docker build` that image and attach a non-`host` egress network; missing isolation leaves `ask` uninstalled (503). This repository does not deploy the VPS.

```bash
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

`SOUL.md` e `memories/*` na raiz são links de compatibilidade para clones que ainda usam o layout anterior. Não os substitua por cópias.
