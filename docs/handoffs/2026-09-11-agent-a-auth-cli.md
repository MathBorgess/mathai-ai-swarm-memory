# Agente A — S1 grants/CLI e S2 no servidor

Implemente este pacote no `MathBorgess/mathai-context-engine`, em branch própria
baseada na branch de handoffs. Leia AGENTS.md, CLAUDE.md, wiki/index.md,
src/auth-broker/README.md e [contrato compartilhado](2026-09-11-swarm-contract.md).
O dono já pediu implementação via Cursor. Produza código, testes e PR draft.

## Escopo e arquivos

Você é o único dono de `src/auth-broker/app/api.py`, `main.py`, `agent_card.py`,
`adapters/sqlite.py`, `pyproject.toml`, auth modules novos e respectivos testes.
Pode criar `app/auth/`, `app/cli.py`, `tests/test_grants.py`,
`tests/test_refresh.py`, `tests/test_dpop.py` e `docs/operations/swarm-auth.md`.
Não edite `app/context/`, `app/proposals/`, `src/swarm-mcp/` nem identidade Hermes.
Não alterar AGENTS.md/CLAUDE.md, infra, DNS, VPS ou remote.

## Entrega sequencial dentro do pacote

1. Rodar suite base e registrar resultado. Adicionar migrações aditivas explícitas,
   preservando sessões, nonces e auditoria legados. Um banco novo e um banco da
   versão anterior precisam migrar; reabrir store não duplica eventos/dados.
2. Implementar principals e grants com roles limitando elegibilidade, expiração e
   auditoria append-only própria. CLI `mathai-swarm principal add/list`,
   `grant set/list/revoke`, `token revoke --family`. Exigir caminho de store
   explícito; nenhuma rota MCP administra policy. Validar scope e TTL antes de SQL.
3. Emitir JWT com snapshot de scopes e chave do principal, sem ampliar permissão
   legada. Construir verificador que exporta o mapping descrito no contrato.
4. Implementar Device Flow vinculado a principal/chave, refresh rotativo atômico,
   revogação de família e DPoP. Verificar standards nos links do contrato. Permitir
   modo legado documentado só para fluxos antigos, nunca fallback de ctx a Hermes.
5. Endpoint capabilities reflete só rotas instaladas. Preparar wiring de routers
   C/D sem anunciar operação ausente. Integração dos branches será rodada depois;
   esta primeira entrega pode responder 503 para memória ainda não integrada.

## Aceite

- Principais com mesmo GitHub subject recebem grants diferentes; chave/ID trocados
  não se fazem passar pelo outro. Scope inexistente e `ctx:write:wiki` são negados.
- Grant expirado ou removido não aparece no refresh; access token já emitido dura
  até seu exp salvo revogação de família, cuja checagem deve ser documentada.
- Refresh concorrente não emite dois sucessores; reuse revoga a família; expirado,
  revogado e chave errada retornam invalid_grant sem imprimir token.
- DPoP: key mismatch, htm/htu/ath incorretos, replay jti, iat inválido, algoritmo
  inesperado e JWK com chave privada rejeitados. Nonces/replay sobrevivem restart.
- JWT: assinatura, issuer, audience, exp/nbf, claims e família validados.
- Novo grant ctx não abre o proxy Hermes. Legacy permanece coberto por testes.
- CLI instalada expõe help e faz ciclo add → grant → list → revoke em DB temporário.

Use pytest e TestClient conforme o repo. Teste comportamento antes de implementar.
Execute `python -m pytest` em `src/auth-broker` e `bash tests/test-hermes-identity-sync.sh`
na raiz. Se dependências faltarem, use venv temporária ignorada; não editar ambiente
de produção. PR deve indicar S1 e servidor S2, sem alegar MCP/ACL já integrados.

Ao terminar, relate branch, commit, URL do PR, resultados exatos e pontos para B/C/D.
Não mergear/deployar. Divergência de contrato: documente e pare só a parte dependente.
