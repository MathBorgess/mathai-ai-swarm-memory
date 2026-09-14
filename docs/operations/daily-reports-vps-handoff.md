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
   GitHub do dono, nunca o login textual. Na **primeira** instalação, obtenha
   esse número com a API read-only do GitHub (endpoint `GET /users/{login}` —
   use o login conhecido do dono, sem redirecionamento de shell) e grave no
   `.env` **antes** de rodar `setup-vps.sh`. Não use `mathai-swarm allow` para
   descobrir o ID: esse comando muta o SQLite de grants e pressupõe broker/CLI
   já instalados (circular no bootstrap). Depois do broker no ar, novos
   principals continuam via `mathai-swarm allow @login` (persiste o número).
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
   `AUTH_BROKER_JWT_SIGNING_KEY`. Descubra configuração lendo arquivos locais
   com cuidado (existência, permissões, caminhos canônicos) — nunca `source` do
   `.env` de produção num shell compartilhado; se uma ferramenta for imprimir
   segredo, pare antes.
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

## 4. Broker — orientação, bootstrap e upgrade

**Correção sobre `docs/operations/install-auth-broker-vps.md`:** aquele documento
e o runbook da wiki ainda citam `git fetch origin codex/a2a-github-oauth`. Esse
branch já está em `origin/main` (seção 0). Oriente-se pelo **código mergeado
atual** em `origin/main` (confirme o SHA com `git fetch` + `rev-parse`, não
confie neste texto se o branch andou). Bootstrap (máquina fria) e upgrade
(já em produção) são fluxos distintos. **Não** faça `git switch` no checkout
vivo que produção usa como ponteiro de deploy: atualize via worktree de release
isolada (abaixo). Limpe ou reutilize árvores antigas preservando arquivos do
usuário (`.env`, SQLite, chaves) — nunca apague `$STATE` inteiro.

Convenções locais típicas (ajuste prefixo de usuário se necessário): checkout
de deploy `$HOME/services/a2a-broker`, estado `$HOME/.mathai-context-engine`,
`.env` do broker em `$STATE/auth-broker.env`. **Descubra** caminhos reais lendo
o `.env` de produção no editor ou com leitura pontual de linhas `VAR=…` — sem
`source`/`set -a` no shell, sem parsers caseiros que reinterpretam valores
shell. Monte um **mapa canônico** de todo banco e arquivo de config referenciado:
pelo menos `AUTH_BROKER_DATABASE_PATH`, e, **se a variável existir no `.env`**,
`AUTH_BROKER_CONTEXT_SQLITE`, `AUTH_BROKER_PROPOSAL_SQLITE`, paths
`AUTH_BROKER_JWT_SIGNING_KEY*`, configs de `ask` e inferência. Variável
opcional **ausente** = recurso não instalado; variável **presente** com arquivo
**ausente** em upgrade = pare e investige (fail-closed). Bootstrap pode
intencionalmente não ter nenhum SQLite ainda.

### 4.a Bootstrap — primeira instalação

Fluxo obrigatório: **staging em `127.0.0.1:9911` → testes → promoção para
`9910`**. Nunca trate “subir só produção” como smoke numa máquina fria. Não
simule backup SQLite de upgrade quando o banco principal ainda não existe.

Checklist:

1. **Hermes pessoal:** confirme um único gateway do bot do dono (sem segundo
   `hermes gateway run` competindo).
2. **Código:** clone `MathBorgess/mathai-ai-swarm-memory` no caminho de deploy;
   aponte para o SHA escolhido de `origin/main`. Na **raiz do clone** (identidade
   fria):
   ```bash
   ./hermes-sync-identity.sh pull
   ./hermes-sync-identity.sh link
   bash tests/test-hermes-identity-sync.sh
   ```
3. **Dependências:** instale o que `docs/operations/install-auth-broker-vps.md`
   e o README desta release exigem (Python, venv, ferramentas de sistema) —
   versões conforme a máquina, não suposições de outro handoff.
4. **Segredos e `.env`:** `install -d -m 700` em `$STATE`; crie `$STATE/auth-broker.env`
   (`0600`) com as chaves exigidas por `setup-vps.sh` (nomes na doc de instalação,
   nunca valores neste prompt). Resolva `GITHUB_ALLOWED_USER_ID` via API GitHub
   read-only **antes** do setup (seção 2). Segredos novos podem ser gerados ou
   copiados localmente; valores **já presentes** em Hermes ou no `.env` existente
   devem ser **preservados**, nunca rotacionados por conveniência.
5. **MCP (`/mcp`) opcional:** se for habilitar nesta passagem, configure chave
   ES256 de assinatura e grants/admin por ID numérico conforme
   `docs/operations/swarm-connector.md` / `swarm-auth.md`; preserve audience
   `/mcp` e o fluxo OAuth authorization-code + PKCE distinto do Device Flow.
6. **`ask` opcional:** siga `docs/operations/swarm-ask.md` (pin em
   `ask-worker/HERMES_PIN`, build de imagem a partir do diretório correto,
   rede dedicada não-`host`, inferência isolada sem mounts de memória pessoal).
   **Verifique** se o daemon Docker responde de fato antes de declarar `ask`
   habilitado; se Docker não estiver disponível, deixe `ask` **desligado**,
   reporte a lacuna e **não** declare bootstrap completo com `ask` suposto.
7. **Portão de testes no checkout:** na raiz do clone, rode o script de setup
   (venv desta árvore + pytest do broker):
   `PYTHON_BIN=<python da VPS> src/auth-broker/scripts/setup-vps.sh "$STATE/auth-broker.env"`.
   Falha → pare. Isto **não** substitui staging em `9911` nem prova operacional
   na VPS por si só — é portão de protocolo/SQLite no código mergeado.
8. **Staging e promoção:** crie diretório de backup/staging único sob `$STATE`
   (sem SQLite de produção para copiar). Siga **4.c**, smoke em `9911`, registe
   mapa de rollback (**seção 5**), depois **4.d**.

### 4.b Upgrade — broker já em produção

Pré-condição: `.env` de produção e caminho do banco **principal** existem; broker
ouvindo em `9910` (se não houver broker, trate como **4.a**).

Checklist:

1. `git -C "$HOME/services/a2a-broker" fetch origin main` — registre SHA alvo
   antes de qualquer staging.
2. **Registro pré-upgrade:** anote release/venv atuais, unidade ou script de
   launch (manager), `.env`, configs de assinatura MCP, inferência/`ask`, e o
   mapa canônico **completo** de cada SQLite e arquivo referenciado (caminhos
   absolutos resolvidos, não basename).
3. **Backup fail-closed:** diretório privado novo sob `$STATE/backups/…`
   (`0700`/`0600`). Copie o `.env` de produção para lá. Para **cada** variável
   de banco **configurada** no mapa, faça backup SQLite **online** com
   `sqlite3 <origem> ".backup '<destino>'"` (nunca `cp` do arquivo vivo — perde
   WAL). Nomeie artefatos pelo **nome da variável de config** (ex.
   `AUTH_BROKER_DATABASE_PATH.sqlite3`), não pelo basename do path, para evitar
   colisões. Rode `PRAGMA integrity_check` no **destino** do backup; guarde
   `.schema` antes do upgrade por variável. Banco **principal** configurado e
   arquivo ausente → **pare**. Opcional configurado e ausente → **pare** e
   investigue (não pule silenciosamente). Backup online **por arquivo** não
   garante snapshot transacional **entre** vários bancos.
4. **Consistência na promoção:** ao promover, **quiesce** escritores relevantes
   (broker; Hermes se compartilhar stores) e obtenha backup final coerente
   entre todos os stores mapeados, ou documente divergência aceita. Preserve
   correção WAL; faça checkpoint explícito quando houver risco de disco/recovery.
   **Nunca** inicie leitura de estado privado (SQLite, `.env`) redirecionando
   saída para stdout/logs compartilhados.
5. Continue em **4.c** → **4.d** com o mesmo SHA alvo.

### 4.c Staging isolado (bootstrap **e** upgrade)

Produção permanece no checkout, venv e bancos atuais até promoção bem-sucedida.

Checklist:

1. Crie worktree **detached** da release alvo, sem tocar o checkout de deploy
   vivo: `git -C "$HOME/services/a2a-broker" worktree add --detach
   "$STATE/releases/<timestamp>" origin/main` (ajuste caminhos locais).
2. Diretório de staging único: cópia do `.env` de produção editada **à mão**
   (`staging.env` no prefixo de backup), SQLite e chaves **novos** sob
   `$STATE/staging-db/<release-id>/`, nunca paths de produção.
3. Reescreva no staging.env: cada `AUTH_BROKER_*_SQLITE` apontando para arquivo
   **dentro** do prefixo de staging; cópias de chaves JWT sob staging, não
   symlinks para produção; `HERMES_A2A_URL` stub/desabilitado; `ask` sem corpus
   pessoal; sem segundo Hermes gateway.
4. **Validação canônica:** confirme que **nenhum** path de DB ou config no
   staging.env resolve para produção (compare caminhos absolutos normalizados —
   falha → pare antes de setup).
5. Popule DBs de staging: upgrade copia **somente** dos backups nomeados por
   variável; bootstrap cria arquivos vazios nos paths de staging.
6. Na raiz da worktree de release:
   `PYTHON_BIN=<python> src/auth-broker/scripts/setup-vps.sh <staging.env>`.
7. Registre `upgrade-map` (texto ou `.env` privado) com `OLD_RELEASE`,
   `NEW_RELEASE`, caminhos de produção de **cada** store/config do mapa, e
   prefixo do backup — **sem** glob.
8. Suba broker de staging só em **`127.0.0.1:9911`**, venv **da release**,
   `staging.env`, logs privados. Smoke mínimo: card HTTP local (sem colar
   segredo). Desabilite efeitos externos (sem chamadas reais a provedores além
   do necessário para testes declarados).
9. **Provas exigidas antes de promover:**
   - Identidade: `bash tests/test-hermes-identity-sync.sh` na raiz do repo de
     deploy/release conforme aplicável.
   - Broker: `setup-vps.sh` + pytest (já no passo 6).
   - MCP: suíte em `src/swarm-mcp/` conforme README/`pyproject.toml` (separada
     do pytest do broker).
   - Para cada capacidade **declarada habilitada** no staging (contexto,
     proposal, `/mcp`, `ask`): smoke autenticado real conforme docs
     (`swarm-mcp.md`, etc.) — testes com fakes no CI **não** contam como prova
     operacional na VPS. Credencial device/MCP só para prova **local**; não
     invente comando curl DPoP.
   Falha → não promova.

### 4.d Promoção para produção

Somente após portões verdes **e** card estável em `9911`:

1. Quiesce escritores; backups finais coerentes com o mapa (seção 4.b).
2. Pare broker produção (`9910`) e staging (`9911`) sem logar argv com segredo.
3. Suba a **release** promovida com `.env` de **produção** (paths de produção
   intactos), porta **`127.0.0.1:9910`**, manager registrado no passo de upgrade.
4. Atualize ponteiro de deploy/systemd **sem** substituir um diretório real por
   symlink às cegas; preserve release anterior intacta para rollback.
5. Confirme `9911` encerrado e **sem** gateway Hermes duplicado.
6. Verificação externa: saúde pública (`a2a.mathai.com.br` card), rotas
   protegidas por auth onde aplicável, e uma verificação representativa de cada
   capacidade **efetivamente habilitada** em produção.

## 5. Rollback

Pré-condição: mapa de upgrade completo (releases, venv, manager, `.env`,
**todos** os caminhos de DB/config do mapa canônico, prefixo de backup).

Checklist:

1. **Antes** de reativar código antigo: pare broker/Hermes — **zero** escritores
   nos SQLite de produção.
2. Valide **compatibilidade** de schema/dados da release **nova** que chegou a
   tocar produção: compare schema atual de **cada** path explícito do mapa com
   o `.schema-before` do backup correspondente. Schema divergente → investigue
   antes de declarar rollback concluído.
3. Se a release nova migrou dados em produção, **salve** o estado produção mais
   recente (backup privado) **antes** de restaurar backups antigos; reconcilie
   writes perdidos — não descarte silenciosamente.
4. Restaure SQLite a partir dos backups nomeados por variável; trate WAL/
   sidecars conforme documentação SQLite com escritores parados (recovery
   suportado, sem atalho destrutivo).
5. Restaure `$OLD_RELEASE`, venv antiga e `.env` de produção; reinicie via
   manager **registrado**, sem imprimir segredos.
6. Verificação externa mínima (card público). Mantenha `$NEW_RELEASE` até
   estabilidade confirmada. Reporte lacunas (mapa incompleto, backup ausente,
   Docker/`ask`, etc.).

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
