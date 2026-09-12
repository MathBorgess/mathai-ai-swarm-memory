# Agente C — S3 memória de runtime com filtro de grafo

Implemente em `MathBorgess/mathai-context-engine`, branch própria baseada nos
handoffs. Leia orientação do repo e [contrato](2026-09-11-swarm-contract.md).
O dono pediu código via Cursor. Esta fatia é contexto filtrado, sem LLM gerador.

## Escopo e interface

Crie `src/auth-broker/app/context/` para indexador/manifest, store SQLite, query,
resolve e router; `tests/test_context_*.py`; `docs/operations/swarm-context.md`.
Sem editar api.py, main.py, pyproject, auth store, proposals ou identidade Hermes.
Use stdlib e libs existentes; não introduzir Graphiti/Neo4j/embeddings para o MVP.
Factory `build_router(*, authorize, store)` expõe query/resolve do contrato.
`authorize(request)` é injetado pelo wiring de A; tests usam fake explicitamente.
Sem fallback de autenticação para headers/body declarativos do chamador.

## Implementação e testes exigidos

1. Manifest curado com workspace, namespace, classificação, camada e revisão.
   Importar fixtures Markdown/wikilinks de diretório temporário. Nunca ingerir
   automaticamente o vault privado inteiro. Indexação por operador, fora do MCP.
2. Mapa explícito de namespaces, paths normalizados; impedir traversal e escape
   por symlink. Metadata ausente, malformada, namespace desconhecido = oculto.
3. Isolar workspace/principal/scope/classificação. private:true não chega ao
   advisor. Owner também precisa grant explícito; role não é bypass de conteúdo.
4. Filtrar nós e arestas ANTES de ranking/limit/traversal. Grafo restrito não pode
   servir de ponte entre dois nós visíveis nem alterar posição dos resultados.
5. Handle aleatório/opaco, persistente por revisão conforme decisão documentada;
   resolve reautoriza atual principal. Sem nomes de paths nos handles/erros.
6. Retornar apenas envelope/receipt do contrato. Sanitizar wikilinks/citações e
   nomes ocultos em snippets; corpo já redigido é a entrada do resultado, não
   somente metadados filtrados depois. Testes cobrem backlinks, títulos, contagens,
   endpoint resolve, mistura de workspaces, reindexação e revogação via auth fake.
7. Fixtures A visível → B oculto → C visível: query não atravessa B. Adicionar nó
   oculto não muda corpo, ranking, contagens nem receipt observáveis ao advisor.
8. Input bounds e respostas vazias equivalentes para proibido/inexistente. Sem
   cache compartilhado nem encaminhamento de texto restrito para Hermes.

Valide com pytest focado e suite do broker. Registre que router com authorize fake
ainda depende da integração de A. Documente limitações de redigir prosa que menciona
privado sem wikilink: classificação/curadoria continuam pré-requisito, sem promessa
de DLP universal ou de que um receipt controla cópia do texto por um modelo.

Entrega: código, fixtures sintéticas, testes executados, guia do manifest, PR draft
com branch/commit e wiring requerido. Sem merge/deploy, sem editar mathai-wiki.
