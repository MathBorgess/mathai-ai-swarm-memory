# mathai-context-engine

Base privada para a camada de contexto dos agentes do dono. Ela mantém a identidade Hermes, descreve a fronteira de confiança do A2A e abriga o futuro Agent Pairing Broker. Não é o vault: o conhecimento compilado continua em `MathBorgess/mathai-wiki`.

## Comece aqui

1. Leia `CLAUDE.md` para o contrato de trabalho e segurança.
2. Leia `wiki/index.md` e a ADR relevante antes de alterar desenho ou operação.
3. Entre no componente: `src/hermes-identity/` ou `src/auth-broker/`.

## Layout

```text
src/hermes-identity/  identidade sincronizada e script de compatibilidade
src/auth-broker/      contrato do broker de pareamento A2A v0.0.1
wiki/                 decisões, arquitetura e roadmap deste repositório
docs/                 plano de implementação executável
```

## Estado atual

O sync de identidade funciona. O broker ainda não é um serviço executável: v0.0.1 foi delimitada, mas só começa após o plano, a revisão da ADR e a escolha da configuração Cloudflare Access.

O remoto GitHub ainda se chama `MathBorgess/hermes-identity`. A renomeação para `mathai-context-engine` fica pendente de migração coordenada das máquinas e da box; mudar o remoto agora quebraria o bootstrap que elas já usam.

```bash
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

`SOUL.md` e `memories/*` na raiz são links de compatibilidade para clones que ainda usam o layout anterior. Não os substitua por cópias.
