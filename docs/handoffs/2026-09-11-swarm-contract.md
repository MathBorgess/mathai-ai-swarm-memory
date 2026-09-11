# Swarm memory — contrato de implementação para o piloto pessoal

Data: 2026-09-11. Base inspecionada: `origin/main` em `987b832`.
O pedido atual do dono autoriza implementar a evolução e estudar um piloto web.
Esta especificação transforma o handoff de estudo em pacotes executáveis. Não
revoga o currículo KA Builder nem autoriza deploy, SaaS multiusuário, billing,
supervisor autônomo, renomear remote ou expor o vault pessoal a clientes.

## Resultado e fronteiras

Um dono conecta diferentes harnesses ao mesmo origin. Cada instalação tem um
principal distinto, grants explícitos e acesso apenas ao recorte autorizado.
Conhecimento recebido vira proposta T2 revisável. O modelo não administra policy.
O piloto comercial é hipótese separada; o código inicial serve um workspace.

Na base, `app/api.py` aceita sessões OAuth bearer opacas armazenadas por hash e
também envelopes Ed25519 legados. O docstring da query está desatualizado: diz que
nenhum bearer autentica, mas existe o ramo bearer. Não repetir essa descrição.
Device Flow concede três scopes A2A fixos; não existe refresh, MCP nem grafo ACL.
Não confundir envelope assinado legado com DPoP interoperável.

## Distribuição e integração

| Agente | Pacote | Dono de arquivos | Depende para integração |
|---|---|---|---|
| A | S1 + metade servidor de S2 | auth, SQLite de auth, CLI, `api.py`, `main.py`, card, pyproject do broker | nenhum para iniciar |
| B | metade cliente de S2 | novo `src/swarm-mcp/` | contrato HTTP de A; query C e propose D |
| C | S3 | novo `app/context/`, testes `test_context_*`, guia próprio | callable de autenticação de A |
| D | S4 | novo `app/proposals/`, testes `test_proposal_*`, guia próprio | autenticação de A; sem depender do grafo para gravar proposta |

Todos partem desta branch de handoffs, em branches/worktrees próprios. Não editar
arquivos de outro pacote. C e D entregam factories de router com autenticação
injetável; A registra as factories numa rodada posterior de integração. Não
chamar um teste com adapter fake de prova de integração real. Não mergear main.

Ordem de aceite: A/S1 → A+B/S2 → C/S3 → D/S4 → smoke integrado. A implementação
dos módulos pode ser paralela; a habilitação de rotas e capabilities segue o
aceite. Features incompletas ficam desabilitadas, com erro explícito e sem
fallback permissivo para Hermes. Cada PR enumera dependências e limites.

## Identidade, scopes e grants

- Um `workspace_id` configurado pelo operador, não escolhido no corpo da query.
- `principal_id` identifica instalação de harness; GitHub `subject` identifica
  quem consente. Vários principais podem ter o mesmo subject sem compartilhar grants.
- Registro pelo CLI vincula principal a subject numérico e JWK thumbprint. O
  Device Flow exige chave correspondente; não basta declarar ID de outro agente.
- Roles `owner`, `self-harness`, `advisor` limitam elegibilidade. Role não concede
  acesso sozinha. Grants explícitos são a autoridade. Advisor só recebe
  `ctx:read:pesquisa.tcc` e/ou `ctx:propose:pesquisa.tcc` nesta fatia.
- Gramática inicial: `ctx:read:<namespace>` / `ctx:propose:<namespace>` com mapa
  explícito `pesquisa.tcc` → `pesquisa/tcc`. Sem wildcard, prefix match arbitrário,
  `ctx:write:wiki` ou permissão de executar comandos.
- Scopes efetivos = pedidos ∩ grants ativos ∩ elegibilidade do principal. Negar
  conjunto vazio e scopes desconhecidos. Não converter grant ctx em `a2a:message`.
- Grant tem expiração própria (máximo 90 dias no piloto). Access token tem 5 min
  por padrão e teto de 1 h. Refresh nunca estende a expiração do grant/família.
- Mudança de grant vale na próxima emissão/renovação. Revogação de família pode
  bloquear imediatamente usando checagem persistente de revogação; documentar
  esse round-trip, sem prometer JWT totalmente stateless e revogação imediata.

Access token assinado pelo broker contém `iss`, `aud`, `sub` (principal_id),
`workspace_id`, `scope` (separado por espaços), `classifications`, `iat`, `nbf`, `exp`, `jti`, `sid`
(família) e `cnf.jkt`. Verificador valida todos os campos e algoritmo fixo.
Material de assinatura vem da configuração segura; nenhuma chave default.
Sessões legadas não ganham ctx scopes nem acessam novos endpoints por migração.
`classifications` é calculado no servidor: advisor admite `public`/`shared`;
owner/self-harness admite também `internal`/`restricted`, sempre combinado com
scope explícito. `private:true` força `restricted`; recurso sem classificação
não é indexado como compartilhável. Cliente não define classifications.

## HTTP v1 para A e B

Contrato de aplicação JSON (não anunciar discovery OAuth padrão inexistente):

| Método e rota | Entrada | Saída |
|---|---|---|
| POST `/v1/oauth/github/device/start` | `{principal_id, scopes:[...]}` + prova DPoP | `{device_code,user_code,verification_uri,expires_in,interval}` |
| POST `/v1/oauth/github/device/poll` | `{device_code,grant_type:"urn:ietf:params:oauth:grant-type:device_code"}` + prova da mesma chave | pending ou `{access_token,refresh_token,token_type:"DPoP",scope,expires_in}` |
| POST `/v1/oauth/token` | `{grant_type:"refresh_token",refresh_token}` + prova da chave vinculada | mesmo envelope de tokens, refresh rotacionado |
| POST `/v1/oauth/revoke` | credencial DPoP da própria família | `{status:"revoked"}` sem segredo |
| GET `/v1/context/capabilities` | auth DPoP | principal, scopes e operações efetivamente instaladas |
| POST `/v1/context/query` | `{query,limit?:10}` | envelope filtrado de C |
| POST `/v1/context/resolve` | `{handles:[...]}` | mesmo envelope de C, reautorizado |
| POST `/v1/context/propose` | objeto abaixo + `Idempotency-Key` | status, proposal_id, branch e pr_url quando efetivamente criado |

Limites iniciais: body 16 KiB; query 2000 caracteres; limit 1–50; até 50 handles;
proposta 12 KiB de Markdown. Contratos de input estritos, sem campos de policy.
401 credencial ausente/inválida; 403 operação fora de scope; 400 input inválido;
409 mesma chave idempotente com outro conteúdo; 413 excesso; 503 dependência
ausente. OAuth retorna `error` estável (`authorization_pending`, `slow_down`,
`expired_token`, `access_denied`, `invalid_grant`, `invalid_dpop_proof`).
401 não significa sempre grant revogado; 403 não dispara refresh em loop.

DPoP segue [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449): validar assinatura,
tipo, algoritmo assimétrico permitido, JWK pública, `htm`, `htu`, `iat`, `jti` e
`ath` nos recursos; conferir binding `cnf.jkt`. Replay store é persistente e
atômico. URL externa canônica vem da configuração, nunca de forwarded headers
arbitrários. Refresh rotativo usa hashes, transação única e detecção de reuse;
a política de família segue [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700).
O cliente serializa refresh por credencial para evitar revogar-se por corrida.

## S3 — envelope e autorização de conteúdo

```json
{
  "items": [{"handle":"opaque-id", "text":"already authorized excerpt", "source_revision":"git-sha"}],
  "capability_receipt": {
    "principal_id":"advisor-01", "workspace_id":"personal",
    "scopes_used":["ctx:read:pesquisa.tcc"],
    "pass_as":"handle", "policy_version":"v1"
  }
}
```

Receipt informa a decisão daquela consulta; não é token nem autorização de
redistribuição. Excluir `namespaces_omitted`, `nodes_redacted`, contagens ocultas
e `may_disclose_to` que pareça prometer controle do transcript. Handle opaco não
codifica caminho e não é credencial. Resolver exige principal atual, workspace,
scope e classificação. Negado e inexistente produzem o mesmo resultado vazio.

Índice de runtime mínimo pode usar SQLite sobre manifest curado de arquivos e
wikilinks, sem Graphiti, embeddings ou extração LLM obrigatórios. Metadados
obrigatórios: workspace, namespace, classificação, camada, source_revision.
Sem metadados válidos = oculto. `private:true` nunca entra no recorte advisor,
mesmo em TCC. Não inferir compartilhável só porque `private` está ausente.
Manifest é administrado pelo operador, nunca por input de ferramenta do modelo.

Filtrar antes de ranking, limit e travessia. Aresta só existe se também estiver
autorizada e ambos os nós visíveis. Não usar nós ocultos como ponte ou influência
de ranking. Filtrar títulos, citações, wikilinks, snippets e backlinks que revelem
alvos ocultos. Caches precisam chave de autorização ou não são usados no piloto.
Um LLM que recebeu texto autorizado ainda pode copiá-lo: esta versão não fornece
controle de saída universal. A integração de delegação encaminha handles; não
encaminha corpos automaticamente. Nenhum contexto restrito passa por Hermes.

Factory de C/D: `build_router(*, authorize, ...)`, em que `authorize(request)`
retorna um mapping validado com `principal_id`, `workspace_id`, `scopes` (tuple),
`classifications` (tuple), `family_id`, `expires_at`. Só o wiring de A produz esse mapping de token validado.
Dados JSON do chamador não podem povoá-lo. Rotas C: query/resolve; D: propose.

## S4 — proposta, escrita e revisão

Entrada: `{namespace:"pesquisa.tcc",title,body_markdown,sources:[{url,label}]}`.
O servidor escolhe caminho `pesquisa/tcc/inbox/YYYY-MM-DD-<proposal_id>.md` e
branch `swarm/proposal-<proposal_id>` num repo configurado, sem aceitar paths,
branch, shell, destino remoto ou template executável do cliente. Prefixo
permitido é fechado; negar wiki, fontes, traversal e symlinks.

GitHub adapter com credencial separada e privilégio mínimo cria branch + commit
+ PR draft. Sem merge. Credencial GitHub do Device Flow não serve para escrever.
Conteúdo e URLs são dados; não buscar URLs nem executar texto recebido. Frontmatter
e cartão de revisão registram principal, proveniência, claims e confirmação
humana pendente. Runbook explicita que T2 não promove a wiki T1/T0.

Idempotência persistente por workspace/principal/chave e hash do payload: retries
retornam a mesma proposta/PR. Reconciliar efeitos parciais branch/commit/PR após
timeout; não assumir que erro HTTP prova ausência de criação. Sem GitHub real nos
testes: contrato com fake e integração opt-in em repo descartável explicitamente
configurado. Não usar o vault pessoal como fixture de escrita.

## Evidência e rollout

Cada agente entrega diff focado, comandos/testes realmente executados, cenários
negativos, docs de configuração e PR draft sem deploy. Não comitar segredos, logs,
bancos reais, conteúdo pessoal nem .env. Fixtures sintéticas, rede mockada nos
testes. Smoke final: duas identidades e dois harnesses; renewal; replay/reuse;
private e fronteiras invisíveis; handle reautorizado; propose idempotente; PR T2.

O primeiro piloto web é uma instância dedicada por dono com onboarding assistido
e endpoint web. UI administrativa, novos clientes reais, scheduler e modelo de
serviço pago são decisões futuras. O request atual permite estudar essas opções,
não instalar gatilhos permanentes nem contatar clientes.
