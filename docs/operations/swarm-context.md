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

## Wiring pendente com A

A deve, numa rodada posterior:

1. Construir o verificador DPoP/JWT que devolve o mapping
   `{principal_id, workspace_id, scopes, classifications, family_id, expires_at}`.
2. Incluir o router só depois desse callable existir:
   `app.include_router(build_router(authorize=verify, store=context_store))`.
3. Não anunciar `query`/`resolve` em capabilities enquanto o store/authorize
   não estiverem instalados (503 explícito, sem fallback para Hermes).
4. Não popular o mapping a partir do JSON do chamador.

Testes desta fatia usam `authorize` fake. Isso prova o filtro de conteúdo, não
a integração OAuth.

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

- Ligar o router em `api.py` / capabilities (Agente A)
- MCP cliente (Agente B)
- Propose T2 (Agente D)
- Deploy, merge em `main`, exposição de Hermes, cache compartilhado, Graphiti
