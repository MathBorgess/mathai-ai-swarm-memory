# Agent Pairing Broker — v0.0.1

O broker admite agentes do dono no gateway A2A sem entregar a eles o bearer estático configurado em Hermes.

## O que v0.0.1 entrega

- Descoberta pública do endpoint de pareamento.
- Pedido autônomo com chave pública do agente e metadados declarados.
- Aprovação explícita do dono por uma chamada protegida por Cloudflare Access.
- Credencial curta vinculada à chave aprovada, materializada como estado de agente no broker.
- Revogação e trilha de auditoria.
- Proxy autenticado ao gateway Hermes com credencial privada do broker.

## O que fica fora

- Escopos por ferramenta, projeto, documento ou tenant.
- Autoaprovação, compartilhamento com terceiros e delegação de aprovação.
- Aceitar uma resposta de GitHub, Google Drive ou Cloudflare MCP como prova de identidade.
- Persistir o bearer do Hermes, o token Cloudflare Access ou qualquer token de provedor no SQLite.

## Persistência inicial

SQLite na VPS, em volume privado e com backup operacional. O banco contém somente `pairing_requests`, `agents`, `audit_events` e hashes de nonces já consumidos. Não armazena desafios em claro, assinaturas, consultas, respostas ou tokens.

## Fronteiras

```text
agente → broker → Hermes A2A
           ↑
     aprovação do dono
```

O token de agente termina no broker. O broker emite ou usa sua própria credencial no salto para Hermes. A especificação completa está em [[wiki/architecture/0001-agent-pairing-broker-v001]].

## Operação

O broker só sobe quando todas as variáveis obrigatórias estão presentes; não há valores padrão para segredos. `scripts/setup-vps.sh /caminho/absoluto/auth-broker.env` prepara o ambiente virtual e roda testes sem iniciar o serviço. O procedimento de instalação, CNAME e política Cloudflare Access está em [[docs/operations/install-auth-broker-vps]]. Ele é um guia de preparação: não significa que a VPS ou o Tunnel tenham sido alterados.
