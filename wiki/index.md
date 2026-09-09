# Wiki — mathai-context-engine

Documentação mínima para agentes que precisam entender o repositório sem depender de uma sessão anterior.

## Arquitetura

- [[wiki/architecture/0001-agent-pairing-broker-v001]] — decisão de v0.0.1: aprovação do dono, chave por agente, SQLite na VPS e proxy com credenciais separadas.
- [[wiki/architecture/0002-auth-broker-hostname-cert-scope]] — amenda: hostname do broker trocado para `auth-broker.mathai.com.br` (Universal SSL não cobre wildcard de dois níveis).
- [[wiki/architecture/0003-agent-card-discovery-instructions]] — descoberta do broker embutida no Agent Card do Hermes A2A, fechando a lacuna de descoberta manual da v0.0.1.

## Roadmap

- [[wiki/roadmap/agent-pairing-broker-v001]] — entregas, gates e critérios de saída para a primeira versão.

## Componentes

- [[src/hermes-identity/README]] — identidade sincronizada e compatibilidade do layout antigo.
- [[src/auth-broker/README]] — contrato do broker e limites deliberados da v0.0.1.

## Operação

- [[docs/operations/install-auth-broker-vps]] — instalação proposta na VPS, hostname `auth-broker.mathai.com.br` (trocado de `pair.a2a.mathai.com.br` por escopo de certificado, ver [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]) e limite da política Cloudflare Access.
- [[docs/operations/agent-card-discovery-instructions]] — configuração do texto de descoberta do broker no Agent Card público do Hermes A2A (`A2A_AGENT_DESCRIPTION`).
