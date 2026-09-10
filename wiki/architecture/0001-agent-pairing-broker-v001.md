# ADR 0001 — Agent Pairing Broker v0.0.1

**Status:** accepted for foundation

## Contexto

O gateway Hermes em `a2a.mathai.com.br` usa peers com bearer estático. Isso não permite a um agente cloud solicitar admissão sem distribuir um segredo duradouro. GitHub, Google Drive e Cloudflare MCP dão acesso autenticado ao processo do agente, mas não emitem uma prova assinada e transferível para o broker.

## Decisão

Executar o broker na VPS atrás do Tunnel Cloudflare no origin público `a2a.mathai.com.br`; Hermes permanece somente em `127.0.0.1:9900` e o broker em `127.0.0.1:9910`. O agente descobre o endpoint, usa Device Flow GitHub e recebe sessão curta. O dono aprova pareamentos na rota protegida por Cloudflare Access. O broker mantém, apenas no ambiente privado, a credencial que usa para chegar ao Hermes.

SQLite será o armazenamento transacional inicial. A aplicação grava estados de pedido, fingerprints de chave, expiração, revogação e auditoria; não grava tokens de MCP, OAuth, Access ou Hermes.

## Consequências

- Um agente pode iniciar o pareamento sem workflow GitHub e sem receber token fixo do Hermes.
- A aprovação do dono continua sendo a prova de posse e a barreira para agentes de terceiros.
- O broker torna-se fronteira de confiança e precisa validar assinatura, audiência, expiração, revogação e correlação antes de encaminhar uma chamada.
- A sessão declara somente `a2a:discover`, `a2a:message` e `a2a:history`; escopos de ferramenta, projeto e documento continuam fora do contrato.

## Alternativas rejeitadas

- Usar um arquivo privado lido via MCP como senha: transforma conteúdo em bearer estático e não prova quem leu.
- Encaminhar token de GitHub/Drive/Cloudflare MCP: amplia vazamento, mistura audiências e não cria prova portátil.
- GitHub Actions OIDC: tecnicamente útil, mas fora deste bootstrap por consumir execução de Actions.

## Relações

- [[src/auth-broker/README]]
- [[wiki/roadmap/agent-pairing-broker-v001]]
- [[src/hermes-identity/README]]
