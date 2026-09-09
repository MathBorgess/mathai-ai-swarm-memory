# Wiki — mathai-context-engine

Documentação mínima para agentes que precisam entender o repositório sem depender de uma sessão anterior.

## Arquitetura

- [[wiki/architecture/0001-agent-pairing-broker-v001]] — decisão de v0.0.1: aprovação do dono, chave por agente, SQLite na VPS e proxy com credenciais separadas.
- [[wiki/architecture/0002-auth-broker-hostname-cert-scope]] — amenda: hostname do broker trocado para `auth-broker.mathai.com.br` (Universal SSL não cobre wildcard de dois níveis).

## Roadmap

- [[wiki/roadmap/agent-pairing-broker-v001]] — entregas, gates e critérios de saída para a primeira versão.

## Componentes

- [[src/hermes-identity/README]] — identidade sincronizada e compatibilidade do layout antigo.
- [[src/auth-broker/README]] — contrato do broker e limites deliberados da v0.0.1.

## Operação

- [[docs/operations/install-auth-broker-vps]] — instalação proposta na VPS, hostname `auth-broker.mathai.com.br` (trocado de `pair.a2a.mathai.com.br` por escopo de certificado, ver [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]) e limite da política Cloudflare Access.
