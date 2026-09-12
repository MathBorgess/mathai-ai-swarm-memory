# Wiki — mathai-context-engine

Documentação mínima para agentes que precisam entender o repositório sem depender de uma sessão anterior.

## Arquitetura

- [[wiki/architecture/0001-agent-pairing-broker-v001]] — ADR histórica do pairing por chave/Access, preservada como proveniência; não usar para deploy atual.

## Roadmap

- [[wiki/roadmap/agent-pairing-broker-v001]] — roadmap histórico do pairing v0.0.1.

## Componentes

- [[src/hermes-identity/README]] — identidade sincronizada e compatibilidade do layout antigo.
- [[src/auth-broker/README]] — contrato OAuth atual, limites e client Device Flow.

## Componentes do swarm

- [[src/swarm-mcp/README]] — adaptador MCP local: Device Flow, DPoP, keystore do OS, tools `query`/`resolve`/`capabilities`/`propose`.

## Operação

- [[docs/operations/install-auth-broker-vps]] — reconstrução da VPS: origin único, GitHub Device Flow, peer Hermes, staging, smoke e recuperação.
- [[docs/handoffs/2026-09-11-swarm-contract]] — contrato de implementação do piloto: identidade, scopes, HTTP v1, fronteiras.
- [[docs/operations/swarm-auth]] — S1/S2 servidor: principals, grants, CLI `mathai-swarm`, refresh rotativo, DPoP, wiring dos routers C/D.
- [[docs/operations/swarm-context]] — S3: manifesto curado, filtro de grafo antes do ranking, handles opacos.
- [[docs/operations/swarm-proposals]] — S4: propose → branch → PR draft T2 em repo de inbox configurado.
- [[docs/operations/swarm-mcp]] — instalação do adaptador nos harnesses (Cursor, Claude Code, Codex).
