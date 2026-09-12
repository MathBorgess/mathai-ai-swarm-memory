# Operação — adapter MCP local (`mathai-swarm-mcp`)

Guia do cliente S2. Não substitui o runbook do broker nem o CLI de A.
Não faz deploy, não publica pacote, não configura MCP global, não completa
login real nesta entrega.

## Dependências e estado

| Peça | Estado nesta fatia |
|---|---|
| Adapter stdio + Device Flow + DPoP + keystore | Implementado em `src/swarm-mcp/` |
| Servidor HTTP v1 (A) | Pendente; testes usam HTTP fake |
| Query/resolve (C) | Pendente; o adapter só encaminha o envelope |
| Propose → PR T2 (D) | Pendente; o adapter só POST o contrato + `Idempotency-Key` |
| Integração A+B+C+D | Falta; smoke integrado é rodada posterior |

O origin público previsto continua `https://a2a.mathai.com.br`. Este cliente
não fala com Hermes e não encaminha grant A2A.

## Instalação local

```bash
cd src/swarm-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Exija um keystore OS (Secret Service, Keychain, Credential Manager). Se
`keyring` cair no backend `fail` ou em arquivo em texto claro, `login`/`serve`
falham com erro explícito. Não há fallback para gravar refresh em disco.

## Cadastro do principal (A) e login (B)

1. Gere a chave **neste** harness:
   `mathai-swarm-mcp show-key --origin https://a2a.mathai.com.br --principal <principal_id>`
   Saída: JWK pública + thumbprint `jkt`. Sem a chave privada.
2. O dono registra o principal no CLI de A com esse `jkt` e o subject GitHub
   numérico. Este pacote **não** cadastra principal.
3. No terminal do usuário (nunca como tool MCP):
   `mathai-swarm-mcp login --origin https://a2a.mathai.com.br --principal <principal_id>`
   Abra `verification_uri` (só `github.com/login/device`) e digite `user_code`.
4. Refresh fica no keystore; access token permanece só na memória do processo
   `serve`.

`logout` chama `POST /v1/oauth/revoke` com DPoP da própria família e apaga o
material local. Não administra outros principais.

## Harness

Configure Cursor / Claude Code / Codex com o binário e `--origin` /
`--principal`. Exemplos no README do pacote. **Jamais** coloque bearer,
refresh ou prova DPoP na config.

Não execute `mcp install` nem altere o MCP global do dono nesta fatia.

## Refresh, 401, 403 e recuperação

- O cliente serializa refresh por credencial (lock). Duas tools concorrentes
  não devem emitir dois sucessores; reuse no servidor revogaria a família
  ([RFC 9700](https://www.rfc-editor.org/rfc/rfc9700)).
- Access prestes a expirar (30 s) é renovado antes da chamada.
- 401: no máximo uma renovação e um retry, cada um com prova DPoP nova
  ([RFC 9449](https://www.rfc-editor.org/rfc/rfc9449)).
- 403 (fora de scope) **não** dispara refresh em loop.
- `invalid_grant`: peça `login` no terminal. Sem loop de navegador.
- Se `POST /v1/oauth/token` estourar timeout, o servidor pode ter rotacionado
  o refresh. O cliente **descarta** o refresh local e recusa reutilizá-lo.
  Recuperação: `mathai-swarm-mcp login` de novo com o mesmo principal/chave.
  Não tente reenviar o refresh antigo.

## Capabilities e tools

`GET /v1/context/capabilities` lista operações realmente instaladas. As tools
MCP `query`, `resolve` e `propose` recusam operação ausente com erro explícito
(sem fallback para Hermes). Enquanto A/C/D não estiverem ligados, espere 503
ou `operations` incompleto.

Prompt opcional `load_authorized_slice`: recorte já autorizado; não é grant.

Propose: o modelo deve reutilizar o mesmo `idempotency_key` em retries. O
adapter preserva o header no retry HTTP de 401.

## Testes

```bash
.venv/bin/python -m pytest
```

Cobertura: pending/slow_down, refresh serializado, revogação, corrida, origin
errado, redirect, DPoP key binding, 401/403, ausência de segredo em stdout/
stderr/MCP, processo stdio com `ClientSession` (`initialize` → `tools/list` →
`call_tool`), venv limpa e help dos dois entrypoints.

Esses testes **não** são prova de integração real com A/C/D.

## O que não fazer

- Login real, cadastro real, publish no PyPI, merge em `main`, deploy na VPS.
- Flag de produção que aceite HTTP claro ou outro host via discovery.
- Guardar access token no keystore, logar JWT/DPoP, ou devolver consentimento
  como resultado de ferramenta.
