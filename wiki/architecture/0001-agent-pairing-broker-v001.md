# ADR 0001 — Agent Pairing Broker v0.0.1

**Status:** accepted for foundation

## Contexto

O gateway Hermes em `a2a.mathai.com.br` usa peers com bearer estático. Isso não permite a um agente cloud solicitar admissão sem distribuir um segredo duradouro. GitHub, Google Drive e Cloudflare MCP dão acesso autenticado ao processo do agente, mas não emitem uma prova assinada e transferível para o broker.

## Decisão

Criar um broker em `pair.a2a.mathai.com.br`, executando na VPS pública atrás do Tunnel Cloudflare. O agente descobre o endpoint, gera ou apresenta uma chave pública e abre um pedido. O dono aprova na UI protegida por Cloudflare Access. O broker emite uma credencial curta vinculada à chave e mantém, apenas no ambiente privado, a credencial que usa para chegar ao Hermes.

SQLite será o armazenamento transacional inicial. A aplicação grava estados de pedido, fingerprints de chave, expiração, revogação e auditoria; não grava tokens de MCP, OAuth, Access ou Hermes.

## Consequências

- Um agente pode iniciar o pareamento sem workflow GitHub e sem receber token fixo do Hermes.
- A aprovação do dono continua sendo a prova de posse e a barreira para agentes de terceiros.
- O broker torna-se fronteira de confiança e precisa validar assinatura, audiência, expiração, revogação e correlação antes de encaminhar uma chamada.
- V0.0.1 usa um único perfil de acesso `owner-agent`; escopos finos são trabalho de v0.1, não um atalho escondido.

## Alternativas rejeitadas

- Usar um arquivo privado lido via MCP como senha: transforma conteúdo em bearer estático e não prova quem leu.
- Encaminhar token de GitHub/Drive/Cloudflare MCP: amplia vazamento, mistura audiências e não cria prova portátil.
- GitHub Actions OIDC: tecnicamente útil, mas fora deste bootstrap por consumir execução de Actions.

## Relações

- [[src/auth-broker/README]]
- [[wiki/roadmap/agent-pairing-broker-v001]]
- [[src/hermes-identity/README]]
