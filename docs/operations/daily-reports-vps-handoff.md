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
   GitHub do dono, nunca o login textual. Na **primeira** instalação, resolva o
   ID só leitura com `gh api users/<login-do-dono> --jq .id` e grave no
   `.env` **antes** de rodar `setup-vps.sh` — `mathai-swarm allow` muta o
   SQLite de grants e pressupõe broker/CLI já instalados (circular no bootstrap).
   Depois do broker no ar, novos principals continuam via `mathai-swarm allow
   @login` (persiste o número).
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

## 4. Broker — bootstrap (máquina fria) vs upgrade (já em produção)

**Correção sobre `docs/operations/install-auth-broker-vps.md`:** aquele documento
e o runbook da wiki ainda citam `git fetch origin codex/a2a-github-oauth`. Esse
branch já está em `origin/main` (seção 0). Use `origin/main` e reporte a
divergência como contradição encontrada (correção dos originais fica fora
desta tarefa).

Variáveis comuns:

```bash
BROKER="$HOME/services/a2a-broker"          # checkout principal (ponteiro de deploy)
STATE="$HOME/.mathai-context-engine"
ENV_FILE="$STATE/auth-broker.env"
```

Leia caminhos de banco **sem** `source`/`set -a` no `.env` de produção (evita
vazar variáveis de staging para o shell e vice-versa):

```bash
env_path() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'\'''; }
MAIN_DB="$(env_path AUTH_BROKER_DATABASE_PATH)"
CTX_DB="$(env_path AUTH_BROKER_CONTEXT_SQLITE)"
PROP_DB="$(env_path AUTH_BROKER_PROPOSAL_SQLITE)"
```

### 4.a Bootstrap — primeira instalação (`$BROKER` ainda não existe)

**Nunca** suba produção em `9910` como único smoke numa máquina fria. Mesmo
sem upgrade anterior, o fluxo é **staging isolado em `9911` → testes →
promoção** — não há banco de produção para “backup de upgrade”; não simule
backup de arquivo inexistente.

1. Confirme que **não há** segundo `hermes gateway run` competindo pelo mesmo
   bot (`pgrep -f 'hermes gateway run'` — só PID).
2. Clone `MathBorgess/mathai-ai-swarm-memory` em `$BROKER`, `git -C "$BROKER"
   checkout main`, e **na raiz do clone**:
   ```bash
   ./hermes-sync-identity.sh pull
   ./hermes-sync-identity.sh link
   bash tests/test-hermes-identity-sync.sh
   ```
3. `install -d -m 700 "$STATE"`. Crie `$ENV_FILE` com as chaves exigidas por
   `setup-vps.sh` (nomes aqui, nunca valores neste prompt):
   `AUTH_BROKER_DATABASE_PATH`, `AUTH_BROKER_AUDIENCE`, `HERMES_A2A_URL`,
   `HERMES_BROKER_TOKEN`, `GITHUB_OAUTH_CLIENT_ID`,
   `GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_ALLOWED_USER_ID`, mais opcionais
   S3/S4/`ask`/`/mcp` se forem instalados já nesta passagem.
   - `GITHUB_ALLOWED_USER_ID`: `gh api users/<login-do-dono> --jq .id` (somente
     leitura; grave no `.env` antes do setup).
   - Segredos ausentes podem ser **gerados ou copiados localmente** (OAuth App,
     par `auth-broker` em `A2A_PEER_TOKENS` de Hermes, chaves ES256 para MCP).
     Valores **já presentes** no `.env` ou em Hermes devem ser **preservados**,
     nunca rotacionados por conveniência. Nunca imprima segredo.
4. Se `ask`/`/mcp` forem parte desta instalação, configure **antes** do primeiro
   `uvicorn` conforme `docs/operations/swarm-ask.md` (seção *Operator launch*):
   pin `ask-worker/HERMES_PIN`, `docker build -t mathai-ask-worker:local
   ask-worker` (ou `install_hermes.sh`), testes de wrapper com Docker real
   **opcionais** — se o daemon não existir, marque “Docker indisponível” no
   relatório; arquivo de inferência dedicado (`0400`, sem mounts de
   `~/.hermes`/memória pessoal); `docker network create ask-egress` (nunca
   `host`). O broker **valida** rede/imagem/config na subida e **recusa**
   iniciar com isolamento inválido — não assuma “503 nas rotas” como único
   sintoma aceitável se o processo deveria ter falhado fechado.
5. Na raiz do clone: `PYTHON_BIN=python3.12 src/auth-broker/scripts/setup-vps.sh
   "$ENV_FILE"` — cria venv **deste checkout** e roda `pytest tests/ -q` do
   broker (portão de protocolo/SQLite/MCP). Falha → pare.
6. Crie `$BACKUP="$STATE/backups/$(date -u +%Y%m%dT%H%M%SZ)"; install -d -m 700
   "$BACKUP"` (sem backup SQLite — bootstrap). Siga **4.c** (worktree de release,
   `.env` de staging com caminhos de SQLite **novos** sob `$STATE/staging-db/…`),
   smoke em `9911`, depois **4.d** promoção para `9910`. Registre o mapeamento
   de rollback (seção 5) antes da promoção.

### 4.b Upgrade — broker já rodando

Pré-condição: `$ENV_FILE` e `$MAIN_DB` existem; processo em `9910` (ou
documente “sem broker — tratar como 4.a”).

`git -C "$BROKER" fetch origin main` e confirme o SHA alvo antes de staging.

**Backup (fail-closed em upgrade):** use `sqlite3 … ".backup …"` (nunca `cp`
do `.sqlite3` vivo — perde WAL). Um `.backup` online **por arquivo** não
garante snapshot transacional **entre** vários bancos; na **promoção**, pare
escritores (broker/Hermes conforme política local) ou aceite que backups
feitos com tudo no ar podem divergir entre `AUTH_BROKER_DATABASE_PATH`,
`AUTH_BROKER_CONTEXT_SQLITE` e `AUTH_BROKER_PROPOSAL_SQLITE`.

```bash
BACKUP="$STATE/backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP"
umask 077
cp -p "$ENV_FILE" "$BACKUP/auth-broker.env"

backup_one() {
  local var="$1" src="$2" dest="$BACKUP/${var}.sqlite3"
  [[ -n "$src" ]] || return 0
  if [[ ! -f "$src" ]]; then
    if [[ "$var" == AUTH_BROKER_DATABASE_PATH ]]; then
      echo "erro: $var configurado mas arquivo ausente — pare (upgrade)" >&2
      exit 1
    fi
    echo "aviso: $var configurado mas arquivo ausente — opcional, pulando" >&2
    return 0
  fi
  sqlite3 "$src" ".backup '$dest'" || { echo "erro: backup falhou $var" >&2; exit 1; }
  sqlite3 "$dest" "PRAGMA integrity_check;" | grep -qx ok \
    || { echo "erro: integrity_check $var" >&2; exit 1; }
  sqlite3 "$src" ".schema" > "$BACKUP/${var}.schema-before.sql"
}

backup_one AUTH_BROKER_DATABASE_PATH "$MAIN_DB"
backup_one AUTH_BROKER_CONTEXT_SQLITE "$CTX_DB"
backup_one AUTH_BROKER_PROPOSAL_SQLITE "$PROP_DB"
chmod 600 "$BACKUP"/*
```

Nenhum `|| true` silencioso. Imprima só `integrity_check` ok/não-ok e caminhos
de artefato, nunca conteúdo de tabela.

### 4.c Staging isolado (fresh **e** upgrade)

Produção continua no checkout/venv/bancos atuais até promoção.

```bash
RELEASE="$STATE/releases/$(date -u +%Y%m%dT%H%M%SZ)"
UPGRADE_MAP="$BACKUP/upgrade-map.env"   # bootstrap: registre mapa manual equivalente
git -C "$BROKER" worktree add --detach "$RELEASE" origin/main

STAGE_ENV="$BACKUP/staging.env"
cp -p "$ENV_FILE" "$STAGE_ENV"
STAGE_STATE="$STATE/staging-db/$(basename "$RELEASE")"
install -d -m 700 "$STAGE_STATE"
```

**Reescreva** `$STAGE_ENV` (editor, não automatize segredo):

- `AUTH_BROKER_DATABASE_PATH` → `$STAGE_STATE/AUTH_BROKER_DATABASE_PATH.sqlite3`
  (mesmo padrão por **nome de variável** para context/proposal se instalados).
- Paths de `AUTH_BROKER_JWT_SIGNING_KEY*` → cópias sob `$STAGE_STATE/`, nunca
  arquivos de produção.
- Desabilite efeitos externos: `HERMES_A2A_URL` de teste/stub; não dispare
  `ask` contra corpus de produção; não inicie segundo Hermes pessoal.

**Validação obrigatória** (falha → pare antes de `setup-vps.sh`):

```bash
stage_main="$(grep -E '^AUTH_BROKER_DATABASE_PATH=' "$STAGE_ENV" | cut -d= -f2- | tr -d '"'\''')"
[[ "$stage_main" == "$STAGE_STATE/"* ]] || { echo 'erro: staging DB ainda aponta produção' >&2; exit 1; }
[[ "$stage_main" != "$MAIN_DB" ]] || { echo 'erro: MAIN_DB igual produção' >&2; exit 1; }
```

Copie para staging **só** a partir de `$BACKUP/AUTH_BROKER_*.sqlite3` (upgrade)
ou crie arquivos novos nos paths de staging (bootstrap).

```bash
PYTHON_BIN=python3.12 "$RELEASE/src/auth-broker/scripts/setup-vps.sh" "$STAGE_ENV"
```

Registre em `$UPGRADE_MAP`: `OLD_RELEASE=…`, `NEW_RELEASE=$RELEASE`, caminhos
de produção e prefixo `$BACKUP`.

Smoke staging (`9911`, venv **da release**, `$STAGE_ENV`):

```bash
nohup /bin/bash -lc "cd '$RELEASE/src/auth-broker' && set -a && . '$STAGE_ENV' && set +a && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9911" \
  >"$STATE/auth-broker-next.log" 2>&1 &
sleep 2
curl -fsS http://127.0.0.1:9911/.well-known/agent-card.json | jq '.securitySchemes'
```

### 4.d Promoção para produção

Somente após `setup-vps.sh` verde **e** card em `9911`:

1. Pare escritores se for consistir vários SQLite (ver backup acima).
2. `kill -TERM` no PID de `9910` (via `netstat -ltnp`, sem logar argv).
3. Pare staging `9911`.
4. Suba `$RELEASE` com `$ENV_FILE` de **produção**, porta `9910`.
5. Atualize ponteiro de deploy/systemd **sem** substituir diretório real por
   symlink às cegas.

```bash
curl -fsS https://a2a.mathai.com.br/.well-known/agent-card.json | jq '{url, security}'
git -C "$RELEASE" rev-parse HEAD
```

### 4.e Testes — o que conta como prova

| Prova | Comando / artefato |
|--------|-------------------|
| Identidade Hermes | `bash tests/test-hermes-identity-sync.sh` (raiz do repo) |
| Protocolo broker | `src/auth-broker/scripts/setup-vps.sh` → `pytest tests/ -q` |
| MCP adapter | Suíte em `src/swarm-mcp/` (`pyproject.toml`); Docker/live só se daemon presente |
| Fumaça HTTP autenticada | Opcional se `command -v mathai-swarm-mcp`; fluxo em `docs/operations/swarm-mcp.md` |
| DPoP curl inventado | **Inválido** como prova de protocolo |

## 5. Rollback

Pré-condição: `$UPGRADE_MAP` com `OLD_RELEASE`, `NEW_RELEASE`, caminhos de
produção (`MAIN_DB`, …) e `$BACKUP`.

1. Pare broker (e Hermes se compartilha SQLite) — **sem** escritores.
2. Se a release nova migrou schema em **produção**, restaure de
   `$BACKUP/AUTH_BROKER_<VAR>.sqlite3`; writes após o backup podem perder-se —
   **reporte/reconcilie**, não sobrescreva silenciosamente.
3. Compare schema **por caminho explícito** do mapa (não glob):
   ```bash
   sqlite3 "$MAIN_DB" ".schema" > /tmp/schema-prod-now.sql
   diff -u "$BACKUP/AUTH_BROKER_DATABASE_PATH.schema-before.sql" /tmp/schema-prod-now.sql \
     || echo 'schema MAIN mudou — investigue antes de declarar rollback completo'
   ```
4. Suba `$OLD_RELEASE` com venv antiga e `$ENV_FILE` de produção.
5. Restaure unit/systemd original (cwd, env file, porta) sem imprimir argv com
   segredo.
6. `curl -fsS https://a2a.mathai.com.br/.well-known/agent-card.json | jq '.url'`

Não apague `$NEW_RELEASE` até confirmar estabilidade. Nunca `rm -rf` em
`$STATE` inteiro.

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
