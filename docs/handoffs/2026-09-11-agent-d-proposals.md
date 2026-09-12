# Agente D — S4 propose → branch → PR T2

Implemente em `MathBorgess/mathai-context-engine`, branch própria baseada nos
handoffs. Leia orientação do repo e [contrato](2026-09-11-swarm-contract.md).
Código solicitado pelo dono; use fixtures e adapter GitHub mockado.

## Escopo e interface

Crie `src/auth-broker/app/proposals/` com validação, outbox SQLite e adapter GitHub
HTTP, router factory; `tests/test_proposal_*.py`; `docs/operations/swarm-proposals.md`.
Sem alterar api.py, main.py, pyproject, auth, context, MCP ou identidade Hermes.
Factory `build_router(*, authorize, store, github, repository)` publica propose.
Auth mapping vem de A, jamais do JSON do cliente. Use libs existentes/stdlib.

## Implementação

1. Schema estrito do contrato e validação de ctx:propose:namespace. Repo e prefixo
   permitidos vêm de configuração confiável. O usuário envia título/conteúdo/fontes,
   não paths, branch, shell, repo destino nem permissões.
2. Renderizar nota T2 com proveniência, data, principal e claims como sugestões
   não verificadas. Escapar frontmatter; referências são dados, sem fetch/execução.
3. Caminho derivado pelo servidor dentro de pesquisa/tcc/inbox; nenhum acesso a
   wiki/, fontes/, AGENTS.md, scripts, CI, symlinks ou path traversal.
4. Outbox com idempotência workspace/principal/chave + payload hash, estados
   persistidos para branch/commit/PR. Repetição não cria outro PR; chave com payload
   diferente dá 409. Concurrency e reinício precisam de testes.
5. GitHub adapter cria branch a partir de base/ref configurada, escreve só o arquivo
   permitido e abre draft PR. Credencial é configuração separada; token do broker
   ou GitHub Device Flow jamais é reenviado para esse adapter.
6. Reconciliar timeout depois de criar branch/commit/PR usando nomes/IDs persistidos;
   não duplicar efeito ao repetir. Repo remoto deve ser fixo e redirects negados.
7. Cartão de revisão: origem, afirmações propostas, fontes e decisão humana pendente.
   Sem auto-merge, promoção a T1/T0, workflow executável ou integração de scheduler.

## Aceite e limites

Teste escopo correto/incorreto, workspace/principal separados, input grande,
frontmatter injection, paths maliciosos, conflito idempotente, retries concorrentes,
crash/timeout em cada fronteira e leitura após reabrir DB. Fake GitHub deve afirmar
repo, prefixo, base e draft exatos. Nenhum teste toca repo real do dono.
Guia operacional informa permissões mínimas da credencial e smoke opt-in em repo
descartável. Sem credencial configurada, 503; não fingir PR criado com URL inventada.

Execute pytest focado e suite do broker. Entregue branch, commit, PR draft, comandos,
resultados e integração pendente com A/B. Não mergear/deployar, nem criar propostas
de teste no mathai-wiki real. O PR de implementação deste pacote está autorizado.
