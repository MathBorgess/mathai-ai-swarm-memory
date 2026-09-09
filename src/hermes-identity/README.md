# Hermes identity

Arquivos canônicos que Hermes compartilha entre as máquinas do dono:

- `SOUL.md`
- `memories/MEMORY.md`
- `memories/USER.md`

Nada além deles sincroniza. `.env`, sessões, cache, logs e credenciais continuam locais.

Use a partir da raiz do repositório:

```bash
./hermes-sync-identity.sh pull
./hermes-sync-identity.sh link
./hermes-sync-identity.sh push
```

Os caminhos antigos na raiz existem somente para que links já instalados continuem a resolver. Toda alteração nova deve usar este diretório canônico.
