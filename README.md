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

O sync de identidade funciona. O broker v0.0.1 já tem implementação e testes locais; a instalação na VPS e a configuração Cloudflare Access ainda são uma operação explícita, não uma consequência de dar merge. Siga o [[docs/operations/install-auth-broker-vps]] para preparar esse ambiente.

O remoto GitHub é `MathBorgess/mathai-context-engine`. Clones e links locais podem manter o diretório `~/src/hermes-identity`; o bootstrap aponta ao novo remoto e os URLs antigos do GitHub permanecem redirecionados durante a transição.

```bash
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

`SOUL.md` e `memories/*` na raiz são links de compatibilidade para clones que ainda usam o layout anterior. Não os substitua por cópias.
