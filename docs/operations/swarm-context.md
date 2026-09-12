# Memória de runtime S3 — manifesto curado e filtro de grafo

Este guia descreve o índice de contexto do Agente C. Não é um runbook de
deploy e não autoriza ingestão do vault `mathai-wiki`. O router ainda **não**
está ligado em `api.py`; a autenticação real depende da integração posterior
com o Agente A.

## O que esta fatia entrega

- Manifesto JSON administrado pelo operador, com mapa explícito
  `pesquisa.tcc` → `pesquisa/tcc`.
- SQLite de runtime (nós, arestas de wikilink, aliases withheld). Sem Graphiti,
  embeddings, FTS compartilhado ou cache de query.
- `POST /v1/context/query` e `POST /v1/context/resolve` via
  `build_router(*, authorize, store)`.
- Envelope e receipt do contrato, sem contagens ocultas.

Nada neste pacote chama Hermes. Grant `ctx:read` não abre o proxy A2A.

## Wiring com A (feito na rodada de integração)

`main.py` constrói o `ContextStore` a partir de `AUTH_BROKER_CONTEXT_SQLITE` e
passa `context_router_factory` para `create_app`. `api.py` chama a factory com o
verificador DPoP real e delega `/v1/context/query` e `/v1/context/resolve` para
os endpoints deste pacote — delegação, não `include_router`, porque `query`
continua despachando sessão Bearer legada para Hermes e porque `authorize` pode
rodar **uma vez só** por request (dois consumos do mesmo `jti` viram replay).

Sem a variável de ambiente o store não existe, capabilities não anuncia
`query`/`resolve` e as rotas respondem 503. Grant `ctx:read` continua sem abrir
o proxy A2A.

Os testes desta fatia usam `authorize` fake — isso prova o filtro de conteúdo,
não a autenticação. A prova ponta a ponta é `tests/test_integration_swarm.py`,
que emite token pelo Device Flow e consulta com prova DPoP verificada.

## Manifesto

Arquivo JSON, nunca input de ferramenta MCP:

```json
{
  "workspace_id": "personal",
  "source_revision": "git-sha-or-operator-label",
  "entries": [
    {
      "path": "pesquisa/tcc/exemplo.md",
      "namespace": "pesquisa.tcc",
      "classification": "shared",
      "layer": "notes",
      "private": false
    }
  ]
}
```

Campos obrigatórios por entrada: `path`, `namespace`, `classification`, `layer`.
`private` é opcional e tem de ser boolean; `private: true` força classificação
`restricted`. Metadata ausente, malformada, namespace desconhecido, path fora
do prefixo, traversal (`..`) ou symlink = **oculto** (não indexado como
compartilhável). Arquivos no disco que não estão no manifesto não entram.

`workspace_id` e `source_revision` vêm do manifesto, não do body da query.
Classificação é curadoria do operador: ausência de `private` não implica
`public`/`shared`.

Indexação (operador, fora do MCP), a partir do checkout do broker:

```bash
python - <<'PY'
from pathlib import Path
from app.context import ContextStore, ingest_manifest
store = ContextStore("/var/lib/auth-broker/context.sqlite3")
ingest_manifest(store, Path("manifest.json"), Path("/caminho/do/recorte"))
store.close()
PY
```

Use um recorte sintético ou um diretório explicitamente escolhido. Não apontar
para o clone completo de `mathai-wiki` como default.

## Autorização de conteúdo

C não interpreta `role`. O mapping injetado já traz `scopes` e
`classifications` calculados por A:

- advisor: em geral `public`/`shared` e só `ctx:read:pesquisa.tcc`
- owner/self-harness: pode incluir `internal`/`restricted`, ainda assim só com
  grant explícito

`private:true` nunca entra no recorte advisor, inclusive em TCC. Owner sem
`ctx:read:pesquisa.tcc` recebe 403; grant do namespace errado devolve envelope
vazio, igual a consulta inexistente.

Handle é `token_urlsafe` persistente por `(workspace, path, source_revision)`.
Não codifica path e não é credencial. Resolve reautoriza o principal atual.
Handle negado e handle inexistente produzem a mesma lista `items: []`.

## Filtro, ranking e travessia

A ordem é fixa: visibilidade de nós → arestas só com ambos visíveis → score no
texto já redigido → expansão de 1 salto no grafo autorizado → `limit` 1–50
(default 10). Nó oculto não pontua, não ocupa slot de limit e não serve de
ponte entre dois visíveis.

Wikilinks, links Markdown e paths de alvos ocultos são removidos do `text`
antes de pontuar e de devolver o envelope. Prosa que menciona um título
privado **sem** wikilink não é DLP universal: a curadoria do manifesto
continua sendo o pré-requisito. O receipt descreve a decisão desta consulta;
não autoriza redistribuição nem controla cópia por um modelo.

## Limites HTTP

| Campo | Limite |
|---|---|
| body | 16 KiB |
| `query` | 2000 caracteres |
| `limit` | 1–50, default 10 |
| `handles` | até 50 |

Contrato estrito: campos extra (incluindo policy) → 400. 401 credencial
ausente/inválida/expirada. 403 sem `ctx:read` conhecido. 413 excesso. 503
store/authorize ausente.

Envelope:

```json
{
  "items": [{"handle": "opaque-id", "text": "already authorized excerpt", "source_revision": "git-sha"}],
  "capability_receipt": {
    "principal_id": "advisor-01",
    "workspace_id": "personal",
    "scopes_used": ["ctx:read:pesquisa.tcc"],
    "pass_as": "handle",
    "policy_version": "v1"
  }
}
```

Não há `namespaces_omitted`, `nodes_redacted`, contagens ocultas nem
`may_disclose_to`.

## Testes

```bash
cd src/auth-broker
python -m pytest tests/test_context_manifest.py tests/test_context_filter.py tests/test_context_http.py tests -q
```

Fixtures são sintéticas em diretório temporário. Não há ingestão do vault
pessoal nestes testes.

## Fora desta fatia

- Deploy, exposição de Hermes, cache compartilhado, Graphiti, embeddings
- Ingestão automática do vault pessoal
