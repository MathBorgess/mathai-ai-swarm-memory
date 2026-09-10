# mathai-context-engine

Base privada para a camada de contexto dos agentes do dono. Ela mantém a identidade Hermes, descreve a fronteira de confiança do A2A e abriga o futuro Agent Pairing Broker. Não é o vault: o conhecimento compilado continua em `MathBorgess/mathai-wiki`.

## Comece aqui

1. Leia `CLAUDE.md` para o contrato de trabalho e segurança.
2. Leia `wiki/index.md` e a ADR relevante antes de alterar desenho ou operação.
3. Entre no componente: `src/hermes-identity/` ou `src/auth-broker/`.

## Layout

```text
src/hermes-identity/  identidade sincronizada e script de compatibilidade
src/auth-broker/      broker A2A com GitHub Device Flow e proxy privado para Hermes
wiki/                 decisões, arquitetura e roadmap deste repositório
docs/                 plano de implementação executável
```

## Estado atual

O sync de identidade funciona. O broker OAuth está implantado: o Agent Card público em `https://a2a.mathai.com.br` inicia GitHub Device Flow e o broker encaminha a mensagem autenticada a Hermes apenas pelo loopback privado. O fluxo antigo de pairing por Cloudflare Access está desativado; seus endpoints retornam `404` quando as variáveis de Access não estão configuradas.

Para repetir, auditar ou recuperar a instalação da VPS, comece por [docs/operations/install-auth-broker-vps.md](docs/operations/install-auth-broker-vps.md). O procedimento de operação já validado, incluindo staging, promoção, restart e cleanup, está na wiki privada: `MathBorgess/mathai-wiki` → `estudos/context-engineering/2026-09-09-a2a-oauth-broker-runbook-vps.md`. Nenhum dos dois documentos contém valores de segredos.

O remoto GitHub é `MathBorgess/mathai-context-engine`. Clones e links locais podem manter o diretório `~/src/hermes-identity`; o bootstrap aponta ao novo remoto e os URLs antigos do GitHub permanecem redirecionados durante a transição.

```bash
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

`SOUL.md` e `memories/*` na raiz são links de compatibilidade para clones que ainda usam o layout anterior. Não os substitua por cópias.
