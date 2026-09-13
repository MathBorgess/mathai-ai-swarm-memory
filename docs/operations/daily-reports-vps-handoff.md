# Handoff VPS — inventário, upgrade seguro do broker/MCP/Hermes e preparação de `reports.mathai.com.br`

Este documento não é um log de execução: nenhum comando abaixo foi corrido por quem escreveu este arquivo, que não tem acesso SSH à VPS. É um **prompt copiável**, autocontido, para o agente (ou o dono) que efetivamente controla a VPS. Cole o bloco da seção "Prompt" inteiro numa sessão nova com acesso à VPS e ao clone deste repositório.

Base de leitura desta entrega: `docs/operations/install-auth-broker-vps.md`, `docs/operations/swarm-connector.md`, `docs/operations/swarm-ask.md`, `docs/operations/swarm-auth.md`, `README.md`, `docs/handoffs/2026-09-11-swarm-contract.md`. O ciclo de daily reports (manhã/dia/noite) está **desenhado, não implementado** — ver a nota de desenho e o handoff na wiki privada, citados na seção "Ver também". Este arquivo não repete o desenho; ele prepara a VPS para o que já existe e deixa o terreno pronto, sem inventar comando para o que ainda não existe.

---

## Prompt

````markdown
# Tarefa: inventariar, atualizar com segurança o broker A2A/MCP/Hermes já mergeado, e preparar `reports.mathai.com.br` atrás de Cloudflare Access

Você tem acesso à VPS (Ailla box) e a um clone de `MathBorgess/mathai-ai-swarm-memory`.
Você **não** decide arquitetura aqui: qualquer decisão de desenho já foi tomada
em `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`
e no contrato do swarm (`docs/handoffs/2026-09-11-swarm-contract.md`). Se algo
aqui contradizer esses documentos, **pare e reporte** — não resolva a
contradição sozinho.

## 0. O que já existe vs. o que não existe

**Já mergeado em `origin/main` (commit `35ee9135309a86532ddea49f41b7e4760ee85d9e`,
"Merge pull request #11 from MathBorgess/codex/swarm-remote-connector")** —
confira você mesmo com `git log -1 origin/main`, não confie neste número se o
branch já andou:

- Broker A2A com GitHub Device Flow (`app/adapters/github.py`, `app/api.py`),
  card público em `/.well-known/agent-card.json`, proxy autenticado a Hermes
  via credencial própria (`HERMES_BROKER_TOKEN`).
- CLI `mathai-swarm` (`allow`, `principal add|list`, `grant set|list|revoke`,
  `token revoke`) — administra grants em SQLite local; não é rota HTTP/MCP.
- Contexto S3/S4 (`query`, `resolve`, `propose`) instalados só por configuração
  (`AUTH_BROKER_CONTEXT_SQLITE`, bloco `AUTH_BROKER_PROPOSAL_*`); sem essas
  variáveis, os endpoints existem e respondem **503**.
- `ask` isolado (Hermes worker em container, tools vazias, sem memória) —
  instalado só com `AUTH_BROKER_ASK_INFERENCE_CONFIG`, `AUTH_BROKER_ASK_IMAGE`,
  `AUTH_BROKER_ASK_NETWORK` e um contexto configurado; sem isso, **503**.
- Conector MCP remoto Streamable HTTP em `/mcp`, com OAuth authorization-code
  + PKCE distinto do Device Flow, instalado só com
  `AUTH_BROKER_WORKSPACE_ID` + chave de assinatura ES256
  (`AUTH_BROKER_JWT_SIGNING_KEY[_PATH]`) — nomes conferidos nesta sessão
  contra `src/auth-broker/app/main.py`, não repetidos de memória. Exige que o
  GitHub OAuth App também aceite o callback `{public_url}/mcp/oauth/callback`
  (idem, conferido no código), além do Device Flow.
- Verificador legado de Cloudflare Access (`app/adapters/owner.py`) para o
  **pairing antigo do próprio A2A**, desligado por padrão (só ativa se as três
  variáveis `AUTH_BROKER_CF_ACCESS_*`/`AUTH_BROKER_OWNER_EMAIL` estiverem
  presentes). Isto é código legado do a2a, **não** é o Access que este
  documento pede para `reports.mathai.com.br` — não confunda os dois.

**Não existe ainda (fases F1–F6 do ciclo de daily reports, nenhuma
implementada nesta árvore):**

- Nenhum `src/reports/`, nenhum `skills/daily-plan/`, nenhum
  `skills/daily-review/`.
- Nenhum comando `mathai-swarm report morning` ou `report evening` — a CLI
  atual não tem subcomando `report`. Não invente essa chamada.
- Nenhum servidor HTML de report, nenhum endpoint `POST /evening`, nenhum
  ledger de ações autônomas.
- Nenhum gatilho, cron ou systemd timer para nada relacionado a report.
- Nenhum conteúdo para servir em `reports.mathai.com.br` hoje. Preparar o
  hostname nesta tarefa é preparar o **encanamento** (tunnel + Access), não
  publicar um report.

Se você encontrar qualquer um desses artefatos já presente na VPS (script,
cron, hostname já exposto sem Access), **não assuma que está certo** — pare e
reporte como contradição; pode ser um resquício de outra sessão.

## 1. Leitura obrigatória antes de qualquer comando

Nesta ordem, sem pular:

1. `CLAUDE.md` e `AGENTS.md` deste repositório — modelo de confiança, limites
   de escrita, "nunca comitar segredo".
2. `docs/operations/install-auth-broker-vps.md` — pré-requisitos, variáveis,
   staging/promoção, smoke, recuperação.
3. `docs/operations/swarm-connector.md` — transporte MCP em `/mcp`, o que
   liga e o que não liga.
4. `docs/operations/swarm-ask.md` — isolamento do worker `ask`, rede dedicada,
   arquivo de inferência próprio.
5. `docs/operations/swarm-auth.md` — CLI, grants, DPoP, o que
   `/v1/context/capabilities` anuncia de verdade.
6. `README.md` deste repositório — estado atual em uma tela.
7. Se você tiver acesso à wiki privada: nota de desenho
   `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`
   e o handoff `.../2026-09-13-handoff-daily-reports-swarm.md`, só para saber
   o que vem depois — você não implementa nada de lá agora.

## 2. Restrições inegociáveis

1. **Allowlist é numérica.** `GITHUB_ALLOWED_USER_ID` é o ID numérico da conta
   GitHub do dono, nunca o login textual. O mesmo vale para qualquer novo
   principal: `mathai-swarm allow @login` resolve o ID pela API do GitHub e
   persiste o número.
2. **Credencial de Hermes é separada e privada.** `HERMES_BROKER_TOKEN` no
   `.env` do broker tem que ser exatamente o valor do peer `auth-broker` em
   `A2A_PEER_TOKENS` no `.env` de Hermes. Nunca gere um segundo valor se
   `auth-broker` já existir — sincronize (seção 4).
3. **Loopback nas portas de serviço.** Hermes escuta só em
   `127.0.0.1:9900`; o broker escuta só em `127.0.0.1:9910` (produção) ou
   `:9911` (staging). A única borda pública é o Cloudflare Tunnel. Nunca
   exponha essas portas fora do loopback.
4. **`/mcp` usa OAuth authorization-code + PKCE**, distinto do Device Flow do
   operador. Não reuse o token de Device Flow como se fosse um bearer MCP.
5. **Cloudflare Access nunca no hostname `a2a.mathai.com.br`.** Bloquearia o
   Device Flow público e o card. As rotas do pairing legado devem continuar
   respondendo `404` sem as variáveis de Access.
6. **Cloudflare Access é obrigatório e exclusivo em `reports.mathai.com.br`.**
   Sem Access esse hostname fica público e vaza o vault privado do dono.
7. **Sem cron nesta tarefa, e nenhum cron para report nas primeiras duas
   semanas depois que o ciclo existir.** Isto é regra permanente do desenho,
   não uma sugestão: não crie nenhum **agendador** — timer systemd, entrada de
   crontab, ou qualquer outro disparo automático — para `report
   morning`/`evening`. Isto **não** proíbe subir, quando F3 existir, um
   `systemd` **service** de longa duração para o servidor HTTP de reports
   (análogo a como Hermes/broker já rodam) — a proibição é sobre
   *agendamento*, não sobre *servir*. Passadas as duas semanas de disparo
   manual comprovado, o cron continua desligado até o dono **pedir
   explicitamente** para ligá-lo — o tempo decorrido não liga nada sozinho.
8. **Nunca imprima, logue, comite ou cole segredo.** Isso inclui: conteúdo de
   qualquer `.env`, `client_secret`, `HERMES_BROKER_TOKEN`, `access_token`,
   linhas cruas de `A2A_PEER_TOKENS`, chave privada, e qualquer valor
   `AUTH_BROKER_JWT_SIGNING_KEY`. Comandos abaixo foram escritos para nunca
   precisar disso; se um comando seu for imprimir algo assim, pare antes.
9. **Sem `set -e` em terminal interativo** (mata a sessão sem diagnóstico) e
   sem `pkill` genérico, `rm -rf` ou remoção de SQLite sem backup deliberado.

## 3. Inventário — só leitura, sem alterar nada

Rode e registre a saída (sem colar conteúdo de `.env`, só existência/permissão):

```bash
# checkout do broker: caminho, branch/commit atual
BROKER="$HOME/services/a2a-broker"
test -d "$BROKER" && git -C "$BROKER" rev-parse --abbrev-ref HEAD && git -C "$BROKER" rev-parse HEAD

# processos e portas relevantes (9900 Hermes, 9910 broker prod, 9911 staging)
netstat -ltnp 2>/dev/null | grep -E ':(9900|9910|9911)[[:space:]]' || true

# arquivos de configuração: só existência e modo, nunca conteúdo
STATE="$HOME/.mathai-context-engine"
for f in "$STATE/auth-broker.env" "$HOME/.hermes/.env"; do
  test -f "$f" && stat -c '%n %a' "$f" 2>/dev/null || stat -f '%N %Mp%Lp' "$f" 2>/dev/null || echo "$f ausente"
done

# nomes de peers presentes em Hermes (sem valores)
grep -o '^A2A_TRUSTED_PEERS=.*' "$HOME/.hermes/.env" 2>/dev/null | tr ',' '\n'
grep -c '^auth-broker:' <(sed -n 's/^A2A_PEER_TOKENS=//p' "$HOME/.hermes/.env" 2>/dev/null | tr ',' '\n') 2>/dev/null

# runtime e CLIs disponíveis (presença/versão, não configuração)
python3 --version
for bin in docker cloudflared claude codex cursor-agent hermes netstat; do
  command -v "$bin" >/dev/null 2>&1 && echo "$bin: $(command -v "$bin")" || echo "$bin: ausente"
done

# cron / systemd timers existentes que mencionem report ou swarm — só contagem,
# nunca a linha inteira (um argumento de cron pode conter token/segredo)
crontab -l 2>/dev/null | grep -ci -E 'report|swarm' || echo '0'
systemctl --user list-timers 2>/dev/null | grep -ci -E 'report|swarm' || echo '0'
```

Se algum comando falhar por falta de permissão ou acesso, **não simule o
resultado**: registre como lacuna ("não verificável sem X") no relatório
final. Este handoff não presume CLIs de assinatura instaladas — reporte
apenas o que os comandos acima realmente devolveram.

## 4. Upgrade seguro do broker (staging → smoke → promoção)

**Se `$BROKER` não existir ainda (máquina fria / primeira instalação),** as
seções 3–5 abaixo assumem um checkout e um `.env` já existentes — o que não
serve para bootstrap. Nesse caso:

1. Confirme que **não existe já** um processo Hermes rodando com o mesmo bot
   antes de clonar/iniciar nada aqui — subir um segundo `hermes gateway run`
   apontando para o mesmo bot/token faz os dois competirem pelo mesmo
   polling. Verifique com `pgrep -f 'hermes gateway run'` (só PID) antes de
   iniciar qualquer coisa nova.
2. Clone `MathBorgess/mathai-ai-swarm-memory` em `$HOME/services/a2a-broker`
   (ou o caminho local escolhido) e siga a sincronização de identidade Hermes
   **antes** de qualquer coisa do broker, com o repositório na raiz:
   ```bash
   ./hermes-sync-identity.sh pull
   ./hermes-sync-identity.sh link
   bash tests/test-hermes-identity-sync.sh
   ```
   Isto religa os caminhos canônicos (`SOUL.md`, `memories/*`) para
   `src/hermes-identity/`, sem duplicar a identidade num segundo lugar.
3. Crie `$STATE/auth-broker.env` (`$STATE = $HOME/.mathai-context-engine`,
   `install -d -m 700 "$STATE"` primeiro) com os segredos exigidos por
   `scripts/setup-vps.sh` — **nomes**, não valores, aqui:
   `AUTH_BROKER_DATABASE_PATH`, `AUTH_BROKER_AUDIENCE`, `HERMES_A2A_URL`,
   `HERMES_BROKER_TOKEN`, `GITHUB_OAUTH_CLIENT_ID`,
   `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_ALLOWED_USER_ID` — mais
   `AUTH_BROKER_CONTEXT_SQLITE`/`AUTH_BROKER_PROPOSAL_*` e
   `AUTH_BROKER_ASK_*`/`AUTH_BROKER_WORKSPACE_ID`/`AUTH_BROKER_JWT_SIGNING_KEY*`
   se as fatias opcionais S3/S4, `ask` ou `/mcp` remoto forem instaladas
   agora. Cada valor é obtido localmente (painel GitHub OAuth App, `.env` de
   Hermes para o par de `HERMES_BROKER_TOKEN`) — nunca gerado ou inventado
   por um agente. `GITHUB_ALLOWED_USER_ID` é o ID numérico, resolvido via
   `mathai-swarm allow @login` depois que o broker existir, não digitado à
   mão a partir do username.
4. Rode `PYTHON_BIN=python3.12 scripts/setup-vps.sh "$STATE/auth-broker.env"`
   — isto cria a venv e roda a suíte de testes real antes de qualquer
   `uvicorn` subir. Só depois disso passar, siga a seção 4 abaixo para a
   primeira promoção (não há staging anterior para comparar; trate o
   primeiro boot em `9910` como o próprio smoke).

**Correção sobre `docs/operations/install-auth-broker-vps.md`:** aquele
documento e o runbook da wiki (`2026-09-09-a2a-oauth-broker-runbook-vps.md`)
ainda instruem `git fetch origin codex/a2a-github-oauth`. Esse branch já foi
mergeado em `origin/main` no commit citado na seção 0. Busque `origin/main`,
não o branch antigo — e reporte esta divergência de documentação como
contradição encontrada, para alguém corrigir os dois documentos originais
(fora do escopo de escrita desta tarefa).

```bash
BROKER="$HOME/services/a2a-broker"
STATE="$HOME/.mathai-context-engine"
ENV_FILE="$STATE/auth-broker.env"

git -C "$BROKER" fetch origin main
git -C "$BROKER" rev-parse origin/main   # confirme o SHA antes de trocar

# só prossiga se o SHA atual (seção 3) for diferente e mais antigo
```

**Se `ask` (Hermes isolado) estiver configurado** (`AUTH_BROKER_ASK_*`
presentes no `.env`), o pré-requisito de rede é a rede docker dedicada
descrita em `docs/operations/swarm-ask.md`
(`docker network create ask-egress` — nunca `host`). Isto **não** é algo
para configurar aqui pela primeira vez nesta tarefa: o código
(`app/main.py`, `optional_ask`/`IsolationConfig.validate()`) já falha
fechado — se `AUTH_BROKER_ASK_NETWORK` apontar para uma rede ausente ou for
`host`, o broker recusa subir o worker (`IsolationUnavailable`, HTTP 503 nas
chamadas de `ask`) em vez de rodar sem isolamento. Confirme apenas que a rede
citada em `AUTH_BROKER_ASK_NETWORK` existe (`docker network inspect
"$ASK_NETWORK"`) antes de promover, sem recriar rede que já existe nem trocar
para `host` "para simplificar".

Antes de tocar produção, faça backup do estado atual. **Não use `cp` num
SQLite vivo**: se o banco estiver em modo WAL, `cp` do arquivo principal sozinho
perde escritas que ainda estão em `-wal`/`-shm`, e uma cópia a frio de um banco
em uso pode ficar corrompida. Use o backup online do próprio SQLite
(`sqlite3 origem ".backup destino"`), que é seguro com o banco em uso porque
segue o protocolo de página do SQLite. Descubra os bancos **realmente
configurados** (nunca um glob `*.sqlite3`, que pode pegar arquivo errado ou
perder um caminho fora de `$STATE`):

```bash
BACKUP="$STATE/backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP"
umask 077

cp -p "$ENV_FILE" "$BACKUP/auth-broker.env"

# Bancos configurados de verdade — leia do próprio .env carregado, não adivinhe.
# AUTH_BROKER_DATABASE_PATH é sempre obrigatório; CONTEXT_SQLITE e
# PROPOSAL_SQLITE só existem se as fatias S3/S4 estiverem instaladas.
set -a; . "$ENV_FILE"; set +a
DBS=()
for var in AUTH_BROKER_DATABASE_PATH AUTH_BROKER_CONTEXT_SQLITE AUTH_BROKER_PROPOSAL_SQLITE; do
  path="${!var:-}"
  [[ -n "$path" && -f "$path" ]] && DBS+=("$var:$path")
done
[[ ${#DBS[@]} -gt 0 ]] || { echo 'erro: nenhum banco configurado encontrado — pare, não prossiga com upgrade sem backup' >&2; exit 1; }

for entry in "${DBS[@]}"; do
  var="${entry%%:*}"; src="${entry#*:}"; name=$(basename "$src")
  sqlite3 "$src" ".backup '$BACKUP/$name'" || { echo "erro: backup online falhou para $var ($src)" >&2; exit 1; }
  sqlite3 "$BACKUP/$name" "PRAGMA integrity_check;" | grep -qx ok \
    || { echo "erro: integrity_check falhou para a cópia de $var" >&2; exit 1; }
  sqlite3 "$src" ".schema" > "$BACKUP/$name.schema-before.sql"
done
chmod 600 "$BACKUP"/*
```

Nenhum comando de backup usa `|| true`: uma falha aqui deve **parar** o
upgrade, não seguir silenciosamente sem backup. `.schema` e o resultado de
`integrity_check` (`ok`/não-`ok`, um booleano) são os únicos dados impressos —
nunca `SELECT` de linhas, nunca `dump` completo, nunca conteúdo de tabela.
Diretório e arquivos ficam `700`/`600`, privados ao usuário do broker.

**Staging isolado, nunca a produção viva.** `install-auth-broker-vps.md`
seção 4 assume um único checkout compartilhado entre staging e produção
(`git switch --detach` no mesmo diretório); isto contende com produção (o
mesmo `.venv`, o mesmo SQLite) e é evitado aqui. Use um checkout **separado**
(worktree ou clone novo), venv própria, `.env` e SQLite **copiados** —
produção continua rodando no checkout, venv e banco antigos, sem ninguém
tocar neles, até staging provar que está bom:

```bash
RELEASE="$STATE/releases/$(date -u +%Y%m%dT%H%M%SZ)"
git -C "$BROKER" fetch origin main
git worktree add --detach "$RELEASE" origin/main   # checkout novo, não mexe no $BROKER atual

STAGE_ENV="$BACKUP/staging.env"
cp -p "$ENV_FILE" "$STAGE_ENV"
# Editar $STAGE_ENV: AUTH_BROKER_DATABASE_PATH (e CONTEXT_SQLITE/PROPOSAL_SQLITE
# se instalados) devem apontar para as CÓPIAS restauradas abaixo, nunca para o
# caminho de produção — staging nunca escreve no banco vivo.
STAGE_STATE="$STATE/staging-db"
install -d -m 700 "$STAGE_STATE"
for entry in "${DBS[@]}"; do
  name=$(basename "${entry#*:}")
  cp -p "$BACKUP/$name" "$STAGE_STATE/$name"   # cópia do backup íntegro, não do arquivo vivo
done

PYTHON_BIN=python3.12 "$RELEASE/src/auth-broker/scripts/setup-vps.sh" "$STAGE_ENV"
```

`setup-vps.sh` cria a venv **dessa release** e roda `pytest tests/ -q` da
suíte real do broker (`test_dpop.py`, `test_sqlite.py`, `test_mcp_oauth*.py`,
`test_grants.py`, `test_allow.py`, entre outras) — isto **é** o teste de
protocolo/SQLite/MCP/identidade exigido por `AGENTS.md` ("Testes de protocolo
e SQLite são obrigatórios antes de qualquer deploy"). Se o script sair
diferente de zero, **pare**: não promova com testes falhando, e não troque por
um curl solto. Não existe smoke de DPoP com placeholder — um DPoP inválido
(header ausente ou prova errada) só prova que o endpoint rejeita entrada
inválida, não que o protocolo funciona; a prova real já está nos testes
automatizados acima. Se quiser um teste de fumaça adicional autenticado,
gere um par de chaves de teste com o próprio pacote (`mathai-swarm-mcp
show-key` ou o helper de teste do broker) e rode a chamada real com prova
DPoP calculada, guardando a chave privada de teste só localmente, nunca em
argumento de comando.

Só depois de `setup-vps.sh` e a suíte de testes passarem, suba o servidor de
staging numa porta separada (`9911`) usando a venv **da release nova**,
apontando para as cópias de banco (nunca para produção):

```bash
nohup /bin/bash -lc "cd '$RELEASE/src/auth-broker' && set -a && . '$STAGE_ENV' && set +a && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9911" \
  >"$STATE/auth-broker-next.log" 2>&1 &
sleep 2
curl -fsS http://127.0.0.1:9911/.well-known/agent-card.json | jq '.securitySchemes'
```

Staging não deve chamar Hermes real nem qualquer serviço externo com efeito
colateral (não reusar `HERMES_BROKER_TOKEN` de produção contra a instância
real de Hermes a partir do staging) — se `optional_ask`/S3/S4 estiverem
configurados no `.env` de staging, aponte para um contexto de teste, não para
o de produção.

Só depois do smoke em `9911` (card público correto + suíte de testes verde),
promova: identifique o PID em `9910` via `netstat -ltnp` (só para localizar o
PID a matar, não para imprimir a linha de comando completa em log
compartilhado), pare-o com `kill -TERM <pid>`, pare o staging em `9911`, e
suba a **release nova** (não staging, o mesmo checkout `$RELEASE` com o
`$ENV_FILE` de produção real, apontando para o SQLite de produção real) com
`--port 9910`. Só então troque o ponteiro `$BROKER` (symlink ou variável de
deploy) para `$RELEASE`. O Tunnel já encaminha `a2a.mathai.com.br` para
`127.0.0.1:9910` — não precisa reconfigurar o hostname existente.

Verificação pós-promoção (sem credencial, não vaza nada):

```bash
curl -fsS https://a2a.mathai.com.br/.well-known/agent-card.json \
  | jq '{url, security}'
git -C "$BROKER" rev-parse HEAD   # registre este SHA como "versão em produção agora"
```

## 5. Rollback

Se o smoke em `9911` falhar, ou a promoção quebrar o card público, o rollback
restaura o **checkout antigo com a venv antiga** — nunca `git switch` no
mesmo diretório trocando só o código, deixando a venv nova (com dependências
possivelmente incompatíveis) para trás:

```bash
STATE="$HOME/.mathai-context-engine"
ENV_FILE="$STATE/auth-broker.env"
OLD_RELEASE="<caminho da release anterior, registrado antes do upgrade — o \$BROKER de antes de trocar o ponteiro>"

# pare o processo problemático em 9910/9911 (kill -TERM pelo PID do netstat -ltnp; não logue a linha de comando inteira)

# ponteiro de deploy volta para a release antiga, com a venv antiga intacta
# (não recrie a venv aqui — se ela precisar ser recriada, o rollback falhou)
test -x "$OLD_RELEASE/src/auth-broker/.venv/bin/uvicorn" || { echo 'erro: venv da release anterior ausente, rollback não pode prosseguir assim' >&2; exit 1; }

nohup /bin/bash -lc "cd '$OLD_RELEASE/src/auth-broker' && set -a && . '$ENV_FILE' && set +a && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9910" \
  >"$STATE/auth-broker.log" 2>&1 &
sleep 2
curl -fsS https://a2a.mathai.com.br/.well-known/agent-card.json | jq '.url'

# compatibilidade de schema: compare a cópia de backup (não o banco vivo) com o estado atual
sqlite3 "$STAGE_STATE"/*.sqlite3 ".schema" > /tmp/schema-rollback-check.sql 2>/dev/null
diff "$BACKUP"/*.schema-before.sql /tmp/schema-rollback-check.sql \
  || echo 'schema mudou entre backup e release anterior — investigue antes de aceitar o rollback como completo'
```

Se a release nova já tiver rodado migração de schema contra o banco de
produção real antes de o rollback ser decidido, o rollback não é só trocar
código/venv — restaure também o SQLite de produção a partir do backup
`.backup` da seção 4 (com o broker parado, nunca com o processo escrevendo)
antes de subir a release antiga. Não delete a release nova nem os artefatos
de staging durante a janela de observação — remova-os manualmente, depois de
confirmar que nenhuma porta ou processo os usa, nunca com `rm -rf` do
diretório de estado inteiro.

## 6. Preparar `reports.mathai.com.br` atrás de Cloudflare Access — sem publicar nada

Isto prepara o encanamento de rede para quando F2/F3 existirem. Não inicie
nenhum servidor de report: não há código para isso ainda (seção 0).
**Ordem obrigatória: Access (aplicação + policy) primeiro, rota pública
depois.** Criar a rota antes da policy deixa uma janela real, ainda que
curta, em que `reports.mathai.com.br` responde sem autenticação — mesma regra
de F0 §5.2, repetida aqui porque é a parte executável.

**Passo 1 — identity provider One-time PIN** (só se ainda não estiver
habilitado na conta). Fonte primária:
[One-time PIN login](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/).

1. `Zero Trust` → `Integrations` → `Identity providers` → `Add new identity
   provider` → `One-time PIN`.
2. Não precisa de configuração adicional; o PIN expira 10 minutos após o
   pedido, é de uso único, e um pedido novo invalida o anterior.

**Passo 2 — aplicação Access exclusiva para `reports.mathai.com.br`, antes de
qualquer rota existir**. Fonte primária:
[Publish a self-hosted application to the Internet](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).

1. `Zero Trust` → `Access controls` → `Applications` → `Create new
   application` → `Self-hosted and private`.
2. `Add public hostname`: domínio `mathai.com.br`, subdomínio `reports`.
   (Cloudflare permite criar a aplicação Access referenciando um hostname que
   ainda não tem rota — o DNS/rota só é adicionado no Passo 3.)
3. Em `Access policies`, crie uma política nova:
   - `Include` → seletor `Emails` (não `Emails ending in`) → o endereço exato
     do e-mail do dono. Fonte para o seletor certo:
     [Cloudflare Access policies — selectors](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/).
   - `Require` → `Login methods` → `One-time PIN`.
   - **Não** use `Emails ending in` com o domínio do e-mail do dono: isso
     permitiria qualquer conta desse domínio, não só a dele. Fonte:
     [Common Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/)
     (avisa explicitamente que combinar `Login Methods: One-time PIN` como
     único `Include`, sem restringir e-mail, deixa qualquer endereço entrar).
   - Nenhuma policy `Bypass`, nenhuma policy `Allow` com `Everyone`.
4. Identity providers habilitados para esta aplicação: só `One-time PIN`.
5. `Create`. Confirme, antes de salvar, que a aplicação lista
   `reports.mathai.com.br` e **não** `a2a.mathai.com.br` — é o único uso de
   Cloudflare Access nesta VPS.

**Passo 3 — só agora, a rota**, no mesmo named tunnel da zona `mathai.com.br`
(mesma conta usada por `a2a.mathai.com.br`; conta diferente causa erro 1033,
já registrado na wiki de operação de Hermes). Use uma porta loopback
provisória hoje **sem uso** — sugestão `9920`, confirme com
`netstat -ltn | grep ':9920 '` antes de configurar a rota — e não inicie
nada ouvindo nela; é esperado que a origem devolva `502` até o servidor real
existir. Descubra primeiro se o tunnel é gerido pelo dashboard ou por
`config.yml` local (ver F0 §5.2.a/5.2.b para o procedimento completo nos dois
modos, incluindo como preservar `a2a.mathai.com.br` e qualquer catch-all).
Resumo do caminho por dashboard:

1. `Networking` → `Tunnels` → selecione o tunnel já usado por
   `a2a.mathai.com.br`.
2. `Routes` → `Add a public hostname`: subdomínio `reports`, domínio
   `mathai.com.br`, `Service` → `HTTP` → `127.0.0.1:9920`.
3. Confirme que as rotas existentes (`a2a.mathai.com.br`, qualquer catch-all)
   continuam na lista antes e depois desta mudança.
4. `Save hostname`. O CNAME para `<tunnel-id>.cfargotunnel.com` é criado
   automaticamente; não crie o registro manualmente por outro caminho.

Se o tunnel for gerido localmente (`config.yml`), siga F0 §5.2.b: ler a
ingress list atual, preservar ordem e catch-all, validar sintaxe antes de
recarregar, e conferir que `a2a.mathai.com.br` segue respondendo depois do
reload.

## 7. Três testes obrigatórios antes de qualquer report ser servido

Nenhum dos três é conclusivo sozinho por código HTTP. Um `502` na origem
prova só que não há servidor — não prova que Access está na frente; e "nenhum
e-mail de PIN chegou" também não prova negação por policy, pode ser falha de
entrega de e-mail. A evidência real é o **Access decision log** (`Zero Trust`
→ `Logs` → `Access`) mostrando `allow`/`block` para cada tentativa, cruzado
com o teste de HTTP. O e-mail do dono é registrado neste documento como
identificador (já é conhecido do sistema), não como segredo; **nunca** cole
cookie de sessão `CF_Authorization` ou token de Access no relatório final —
isso seria uma credencial de sessão válida.

O login em si (passos 2 e 3) é feito pelo **dono, no navegador dele** — um
agente não deve digitar o e-mail nem colar o PIN em nome do dono; se o
agente tem acesso de navegador automatizado, ele reporta o resultado
observável (código HTTP, entrada no decision log), não executa o login.

1. **Teste sem autenticação.** De uma rede externa (não da própria VPS):
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' https://reports.mathai.com.br/
   ```
   Esperado: redirecionamento para a página de login do Access (não `200`
   com conteúdo). Cruze com o decision log: deve haver uma entrada de
   desafio de autenticação para esta requisição.

2. **Login permitido do dono.** O dono abre `https://reports.mathai.com.br/`
   no próprio navegador, digita o e-mail exato cadastrado na política, pede o
   código, cola o PIN recebido. Esperado: decision log mostra `allow` para o
   e-mail do dono; a origem pode devolver `502` (sem servidor ainda) — isso
   sozinho não invalida o teste, mas também não é a prova; a prova é o
   `allow` no log.

3. **Negação de usuário errado.** Repita com um e-mail que você controla mas
   que **não** está na política. Esperado, por comportamento documentado do
   Access: nenhum PIN válido chega e o decision log mostra `block` (não
   ausência de log)
   ([Troubleshoot Access — "Policy denial"](https://developers.cloudflare.com/cloudflare-one/access-controls/troubleshooting/)).
   Se o log mostrar `allow` para esse e-mail, ou se nenhuma entrada aparecer
   no log para a tentativa, **pare**: a política está mal configurada
   (provavelmente `Emails ending in` no lugar de `Emails`, ou falta o
   `Require` de login method) — não prossiga para nenhum passo seguinte até
   corrigir.

Só depois dos três passarem, com evidência de decision log e não só de
código HTTP, é que faz sentido, numa sessão futura, apontar essa rota para
um servidor de report real.

## 8. Portões de upgrade para fases futuras (F1–F6)

Nenhuma fase futura é implementada por este handoff. Quando um PR de F1–F6
for mergeado em `origin/main`, **não promova automaticamente**. A tabela de
autonomia da nota de desenho já decide isto — a tradução operacional para a
VPS:

| Situação no PR mergeado | Antes de tocar a VPS |
|---|---|
| Wiki (`mathai-wiki`) T2/T3 | Nada a fazer na VPS — não é código de serviço |
| Skill nova (`skills/daily-plan`, `skills/daily-review`) ou peso do RES | PR fica **draft**; não suba nada até o dono revisar e mergear deliberadamente |
| Código em `src/reports/` com CI verde | Repita as seções 3–4 deste documento (inventário → staging em porta separada → smoke → só então promoção); nunca pule o staging por o PR já ter CI verde |
| Qualquer PR tocando `CLAUDE.md`, `AGENTS.md`, `wiki/principles/` | Só sugestão — nunca aplique sozinho |
| `tau-intent` | PR sem merge nas duas primeiras semanas do ciclo, sem excecão |

**Lacuna conhecida:** a nota de desenho promete um "arquivo de allowlist de
repos e caminhos protegidos" (entrega de F5). Esse arquivo **não existe**
nesta árvore hoje. Até ele existir, o gate de "caminho protegido" não pode
ser automatizado — trate qualquer PR de F1–F6 como revisão manual do dono
antes de ir para a VPS, mesmo com CI verde.

**Cron continua proibido** para tudo que dispare `report morning`/`evening`
até completarem duas semanas de disparo manual comprovado — essa contagem
começa quando F2/F4 existirem e forem operados manualmente, não antes. E
mesmo depois de as duas semanas passarem, isso **não liga cron sozinho**: o
tempo decorrido é uma pré-condição necessária, não suficiente — cron só liga
se o dono pedir explicitamente numa sessão futura.

## 9. Não faça

- Não crie `mathai-swarm report morning` nem `evening` — não existe, não
  simule com outro comando.
- Não coloque Cloudflare Access em `a2a.mathai.com.br`.
- Não crie cron, systemd timer ou qualquer disparo automático para report.
- Não inicie um servidor "provisório" de report em `9920` só para o teste da
  seção 7 passar com `200` — o teste vale com `502` na origem.
- Não publique nem exponha `reports.mathai.com.br` sem os três testes da
  seção 7 passando primeiro.
- Não imprima, logue ou comite segredo — seção 2, item 8.
- Não promova código de F1–F6 para a VPS sem PR mergeado, CI verde e revisão
  humana explícita quando tocar caminho ainda não coberto pela allowlist
  (que ainda não existe).
- Não decida arquitetura: se algo aqui parecer incompleto, reporte a lacuna
  em vez de inventar a decisão que falta.

## 10. O que reportar ao final

Um relatório curto, com:

- SHA do broker antes e depois (seção 3 e seção 4), e se houve upgrade ou só
  inventário.
- Códigos HTTP dos três testes da seção 7 (não corpo de resposta).
- Presença/ausência de cada CLI verificada na seção 3, com versão quando
  existir.
- Confirmação de que `A2A_TRUSTED_PEERS` contém `auth-broker` e que o SHA256
  de `HERMES_BROKER_TOKEN` bate com o do peer (comando comparável sem exibir
  valor — ex. `sha256sum` dos dois lados, comparando só os hashes).
- Cada comando que falhou por falta de acesso, listado como "não verificável
  sem <o que falta>" — nunca como "assumido OK".
- Toda contradição encontrada entre este documento e o estado real da VPS,
  incluindo a divergência de branch já registrada na seção 4.
- Se e quando algum passo deste documento precisar mudar por causa do que a
  VPS realmente mostrou, diga isso explicitamente em vez de silenciosamente
  seguir outro caminho.
````

---

## Ver também

- `docs/operations/install-auth-broker-vps.md` — instalação/promoção original do broker (branch de origem desatualizada; ver correção na seção 4 do prompt acima).
- `docs/operations/swarm-connector.md`, `docs/operations/swarm-ask.md`, `docs/operations/swarm-auth.md` — contrato das capacidades já mergeadas.
- `docs/handoffs/2026-09-11-swarm-contract.md` — fronteiras S1–S4 que este trabalho não reabre.
- Wiki privada (`mathai-wiki`): `estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md` (desenho do ciclo) e `estudos/time-de-agentes/2026-09-08-hermes-gateway-operacional.md` (tunnel/Access/conta Cloudflare).
- Fontes primárias Cloudflare citadas no prompt: [Set up Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/), [One-time PIN login](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/), [Publish a self-hosted application to the Internet](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), [Access policies — selectors](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/), [Common Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/), [Troubleshoot Access](https://developers.cloudflare.com/cloudflare-one/access-controls/troubleshooting/).
