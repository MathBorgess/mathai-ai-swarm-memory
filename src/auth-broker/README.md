# Agent Pairing Broker — v0.0.1

O broker admite agentes do dono no gateway A2A sem entregar a eles o bearer estático configurado em Hermes.

## O que v0.0.1 entrega

- Descoberta pública do endpoint de pareamento.
- Pedido autônomo com chave pública do agente e metadados declarados.
- Aprovação ou recusa explícita do dono em UI autenticada.
- Credencial curta vinculada à chave aprovada.
- Revogação e trilha de auditoria.
- Proxy autenticado ao gateway Hermes com credencial privada do broker.

## O que fica fora

- Escopos por ferramenta, projeto, documento ou tenant.
- Autoaprovação, compartilhamento com terceiros e delegação de aprovação.
- Aceitar uma resposta de GitHub, Google Drive ou Cloudflare MCP como prova de identidade.
- Persistir o bearer do Hermes, o token Cloudflare Access ou qualquer token de provedor no SQLite.

## Persistência inicial

SQLite na VPS, em volume privado e com backup operacional. O banco conterá somente registros de domínio: `pairing_requests`, `agents`, `credential_keys`, `revocations` e `audit_events`. O schema e as migrações entram junto com a aplicação, não nesta fundação documental.

## Fronteiras

```text
agente → broker → Hermes A2A
           ↑
     aprovação do dono
```

O token de agente termina no broker. O broker emite ou usa sua própria credencial no salto para Hermes. A especificação completa está em [[wiki/architecture/0001-agent-pairing-broker-v001]].
