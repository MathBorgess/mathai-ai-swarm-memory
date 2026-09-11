# Agente B — S2 adaptador MCP local

Implemente pacote instalável `src/swarm-mcp/` no `MathBorgess/mathai-context-engine`
em branch própria baseada nos handoffs. Leia orientação do repo e
[contrato](2026-09-11-swarm-contract.md). Implementação solicitada pelo dono.

## Escopo

Python >=3.12, pacote separado, entrypoint `mathai-swarm-mcp`. Use o SDK MCP oficial
compatível e dependências declaradas. Consulte [build server](https://modelcontextprotocol.io/docs/develop/build-server)
e [Python SDK](https://github.com/modelcontextprotocol/python-sdk). Não copie um
servidor JSON-RPC artesanal. Só edite seu pacote e `docs/operations/swarm-mcp.md`.
Não altere broker, configuração MCP global do dono, credenciais reais ou Hermes.

## Implementação

1. Camadas mínimas: client HTTP, credential store, DPoP, MCP stdio e CLI de login.
   Interfaces internas injetáveis para teste, sem framework novo de orquestração.
2. `mathai-swarm-mcp login --origin ... --principal ...` usa Device Flow do contrato;
   exibe consentimento no fluxo do usuário, nunca como retorno de ferramenta do
   modelo. Instalação gera chave e informa apenas chave pública/thumbprint para
   cadastro pelo dono via A. Não completar login real nem cadastrar principal real.
3. Guardar chave e refresh no keystore OS com keyring; rejeitar backend de arquivo
   em texto claro. Sem backend seguro, falhar explicitamente. Access token só em
   memória. Sem segredos em stdout, stderr, exceptions ou respostas MCP.
4. Allowlist do origin HTTPS configurado; redirects desligados; discovery não muda
   endpoint/host de confiança. Validar URLs de consentimento GitHub. Teste HTTP
   local só por injeção de transporte, nunca flag que enfraqueça produção.
5. Renovação single-flight entre chamadas; tratar expiração proativamente. Provas
   novas em retry; uma renovação/retry limitado para 401, nunca retry infinito de
   403. Timeout após refresh rotacionado não permite reutilização cega; documentar
   recuperação. Invalid_grant pede login ao usuário, sem loop de navegador.
6. Tools `query`, `resolve`, `capabilities`, `propose` apenas conforme capabilities
   do servidor. `logout/revoke` fica CLI de usuário, sem administração de outros
   principais. Prompt MCP opcional explícito para carregar recorte inicial.
7. O adapter retorna contexto autorizado e receipts, encaminha handles em operações
   de resolução e nunca infere autoridade de texto. Propose exige chave idempotente
   preservada em retries. Não incluir APIs de dispatch, scheduler ou shell.

## Aceite e handoff

Tests de client contra HTTP fake obedecendo contrato: pending/slow_down, refresh,
revogação, corrida, origin errado, redirect, DPoP key binding, 401/403, sem segredo.
Teste real de processo stdio com MCP ClientSession: initialize → tools/list →
call_tool; verifique que stdout contém só protocolo e erros são sanitizados.
Verifique instalação em venv limpa e ambos entrypoints/help. Configs de exemplo
para Cursor/Claude Code/Codex devem conter binário e origin, jamais bearer.

Entregue README com setup e limites, suite executada, branch/commit/PR draft.
Testes com servidor fake comprovam adapter, não integração com A/C/D. Registrar
essa pendência e os contratos consumidos. Não mergear, publicar pacote nem deployar.
