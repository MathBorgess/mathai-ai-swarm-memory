# Wiki — mathai-context-engine

Documentação mínima para agentes que precisam entender o repositório sem depender de uma sessão anterior.

## Arquitetura

- [[wiki/architecture/0001-agent-pairing-broker-v001]] — ADR histórica do pairing por chave/Access, preservada como proveniência; não usar para deploy atual.

## Roadmap

- [[wiki/roadmap/agent-pairing-broker-v001]] — roadmap histórico do pairing v0.0.1.

## Componentes

- [[src/hermes-identity/README]] — identidade sincronizada e compatibilidade do layout antigo.
- [[src/auth-broker/README]] — contrato OAuth atual, limites e client Device Flow.

## Operação

- [[docs/operations/install-auth-broker-vps]] — reconstrução da VPS: origin único, GitHub Device Flow, peer Hermes, staging, smoke e recuperação.
