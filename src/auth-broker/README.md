# Agent Pairing Broker — v0.0.1

O broker admite agentes do dono no gateway A2A sem entregar a eles o bearer estático configurado em Hermes.

## O que v0.0.1 entrega

- Agent Card público em `/.well-known/agent-card.json`, com OAuth estruturado e escopos `a2a:discover`, `a2a:message` e `a2a:history`.
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

## OAuth e limites de harness

O card anuncia GitHub OAuth como provedor (`github.com/login/oauth`). GitHub não é
issuer OIDC e não fornece `id_token`, `JWKS`, `aud` ou discovery OIDC para esse
fluxo. O broker deve trocar o authorization code, validar a identidade pela API
GitHub (com privilégio mínimo `read:user`) e emitir uma sessão própria, curta,
com audience do A2A e os três escopos internos. O token GitHub nunca é enviado
ao Hermes.

Esse contrato não instrui o modelo a fazer POST em URL externa. Harnesses podem
bloquear URLs não curadas, exigir aprovação para rede ou tratar instruções
remotas como prompt injection; código Python que posta diretamente em `github.com`
ou em um broker também contorna a fronteira de autorização e não deve ser usado
como mecanismo de autenticação. O cliente A2A deve implementar OAuth nativamente,
com PKCE/state (ou Device Flow), allowlist de hosts e validação TLS. A segurança
ideal é: token GitHub curto e escopo mínimo, sessão broker audience-bound e
expiração/revogação, associação a uma chave efêmera do cliente (DPoP quando
suportado), e credencial Hermes separada apenas no ambiente privado.

Em produção, configure `GITHUB_OAUTH_CLIENT_ID` e `GITHUB_OAUTH_CLIENT_SECRET`
no ambiente do broker. Configure também `GITHUB_ALLOWED_USER_ID` com o ID
numérico estável do usuário GitHub autorizado; login textual não é uma âncora.

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

Para agentes headless, o contrato publicado usa Device Flow: `/v1/oauth/github/device/start` retorna `verification_uri`, `user_code` e uma transação opaca; o cliente faz poll em `/v1/oauth/github/device/poll` até receber o token A2A do broker. O `device_code` fica somente em memória limitada do processo, por até dez minutos, e nunca é gravado no SQLite. Um harness compatível ainda precisa implementar esse adapter.
