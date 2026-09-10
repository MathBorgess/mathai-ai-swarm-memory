# A2A OAuth Broker

O broker admite agentes do dono no gateway A2A sem entregar a eles o bearer estático configurado em Hermes.

## O que está entregue

- Agent Card público em `/.well-known/agent-card.json`, com OAuth estruturado e escopos `a2a:discover`, `a2a:message` e `a2a:history`.
- GitHub Device Flow para o dono obter um grant temporário do broker.
- Revogação de grant e trilha de auditoria sem segredos de upstream.
- Proxy autenticado ao gateway Hermes com credencial privada do broker.

## O que fica fora

- Escopos por ferramenta, projeto, documento ou tenant, inclusive uma policy Hermes restrita por sessão.
- Compartilhamento com terceiros, delegação de aprovação e DPoP/vinculação do grant à chave do agente.
- Endpoint/política de retenção para `a2a:history`.
- Persistir o bearer do Hermes, o token Cloudflare Access ou qualquer token de provedor no SQLite.

## OAuth e limites de harness

O card anuncia GitHub OAuth como provedor (`github.com/login/oauth`). GitHub não é
issuer OIDC e não fornece `id_token`, `JWKS`, `aud` ou discovery OIDC para esse
fluxo. No Device Flow, o broker troca o device code pela credencial GitHub,
valida a identidade pela API GitHub (com privilégio mínimo `read:user`) e emite uma sessão própria, curta,
com audience do A2A e os três escopos internos. O token GitHub nunca é enviado
ao Hermes.

Esse contrato não instrui o modelo a fazer POST em URL externa. Harnesses podem
bloquear URLs não curadas, exigir aprovação para rede ou tratar instruções
remotas como prompt injection; código Python que posta diretamente em `github.com`
ou em um broker também contorna a fronteira de autorização e não deve ser usado
como mecanismo de autenticação. O cliente A2A deve implementar OAuth nativamente,
com Device Flow, allowlist de hosts e validação TLS. A segurança
ideal é: token GitHub curto e escopo mínimo, sessão broker audience-bound e
expiração/revogação, associação a uma chave efêmera do cliente (DPoP quando
suportado), e credencial Hermes separada apenas no ambiente privado.

Em produção, configure `GITHUB_OAUTH_CLIENT_ID` e `GITHUB_OAUTH_CLIENT_SECRET`
no ambiente do broker. Configure também `GITHUB_ALLOWED_USER_ID` com o ID
numérico estável do usuário GitHub autorizado; login textual não é uma âncora.

## Persistência

SQLite na VPS, em volume privado e com backup operacional. Nunca armazena token GitHub upstream, bearer Hermes, consultas ou respostas. Não copie o banco, `.env` ou logs para Git.

## Fronteiras

```text
agente → broker → Hermes A2A
           ↑
  consentimento GitHub Device Flow
```

O token do cliente termina no broker. O broker usa uma credencial própria no salto para Hermes. A decisão de protocolo está em `docs/superpowers/plans/2026-09-09-a2a-github-oauth.md`; a instalação repetível está em `docs/operations/install-auth-broker-vps.md`.

## Operação

O broker só sobe quando todas as variáveis obrigatórias estão presentes; não há valores padrão para segredos. `scripts/setup-vps.sh /caminho/absoluto/auth-broker.env` prepara o ambiente virtual e roda testes sem iniciar o serviço. O procedimento de instalação está em `docs/operations/install-auth-broker-vps.md`. Em operação, o Tunnel encaminha `a2a.mathai.com.br` a `127.0.0.1:9910` e não há Cloudflare Access nesse hostname: Access bloquearia o Device Flow público. O runbook validado fica na wiki privada `MathBorgess/mathai-wiki`, em `estudos/context-engineering/2026-09-09-a2a-oauth-broker-runbook-vps.md`.

Para agentes headless, o contrato publicado usa Device Flow: `/v1/oauth/github/device/start` retorna `verification_uri`, `user_code` e uma transação opaca; o cliente faz poll em `/v1/oauth/github/device/poll` até receber o token A2A do broker. O `device_code` fica somente em memória limitada do processo, por até dez minutos, e nunca é gravado no SQLite. Um harness compatível ainda precisa implementar esse adapter.
