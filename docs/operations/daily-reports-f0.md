# Daily reports — F0: pesquisa de quota, inventário e borda Cloudflare

**Fase:** F0 apenas (pesquisa e inventário). Não implementa `src/reports/`, cron, vault nem publicação de HTML.

**Desenho de produto:** nota privada `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md` (hostname `reports.mathai.com.br`, Access com OTP do dono antes de qualquer conteúdo privado).

**Handoff operacional VPS (sessão 02):** [daily-reports-vps-handoff.md](./daily-reports-vps-handoff.md) — é um **prompt** para quem tem acesso à VPS, não evidência executada. A sessão 02 não rodou nenhum comando na VPS; ela produziu instruções. `REPORTS_PORT` continua **TBD para F3** (não é decisão da sessão 02). Este F0 para aqui: pesquisa e inventário, sem implementar `src/reports/`, cron, vault ou publicação de HTML.

---

## 1. Resumo executivo

| Tema | Conclusão |
|------|-----------|
| **Janelas de quota** | Os três provedores **não** compartilham o mesmo modelo: Anthropic (Claude Code) documenta sessão de **5 horas** + **semanal**; OpenAI (Codex) documenta **5 horas** + **semanal** no pool Work/Codex; Cursor documenta **ciclo de cobrança mensal** com pools de uso (sem janela de 5h oficial no help). |
| **Exposição programática** | Codex: `/status` e Settings → Usage (oficial). Claude Code: `/status` e Settings → Usage (oficial). Cursor CLI: `/usage` (oficial); ferramentas tipo `cursor-cli-usage` são de terceiros e leem o mesmo dashboard. |
| **Inventário local (Mac)** | CLIs presentes: `cursor-agent`/`agent`, `claude`, `codex`. `cloudflared` **ausente** no PATH desta máquina. |
| **Inventário VPS** | **Não verificado** nesta sessão (sem SSH). Comandos seguros listados na §4 para o dono rodar na box. |
| **Borda** | Named tunnel na **mesma conta Cloudflare da zona** `mathai.com.br`; `reports.mathai.com.br` só depois de **Access + OTP**; manter `a2a.mathai.com.br` **sem** Access (Device Flow). |

---

## 2. Quota por provedor (fontes primárias)

Legenda de confiança:

- **Documentado** — help/docs oficiais do fornecedor.
- **Observação local** — comando executado neste ambiente (sem credenciais).
- **Inferência** — dedução a partir de docs + skill `handoff`; não é garantia de comportamento futuro.
- **Desconhecido** — sem fonte primária ou sem probe bem-sucedido nesta sessão.

### 2.1 Claude Code (Anthropic)

| Janela | O que a fonte diz | Confiança |
|--------|-------------------|-----------|
| **Sessão (~5h)** | Planos pagos: barra de “current session” em Settings → Usage; limites de sessão associados a janela de cinco horas (Pro/Max descrevem reset da sessão a cada cinco horas). | Documentado — [Usage limit best practices](https://support.anthropic.com/en/articles/9797557-usage-limit-best-practices), [What is the Pro plan?](https://support.anthropic.com/en/articles/8325606-what-is-claude-pro), [What is the Max plan?](https://support.anthropic.com/en/articles/11049741-what-is-the-max-plan) |
| **Semanal** | Limite semanal separado; reset em horário fixo por conta (visível em Settings → Usage). Pode hair entradas distintas para Opus vs demais modelos no painel. | Documentado — mesmas fontes acima |
| **Mensal** | Não há “cota mensal incluída” genérica no mesmo sentido do Cursor; consumo extra pode passar por **usage credits** (planos pagos). | Documentado — [Using Claude Code with your Pro or Max plan](https://support.anthropic.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) |
| **Superfícies** | Claude.ai, Claude Desktop e Claude Code contam para o **mesmo** limite de uso. | Documentado — [Understanding usage and length limits](https://support.anthropic.com/en/articles/11647753-understanding-usage-and-length-limits) |

**Como ver restante (oficial):**

1. **Claude Code (terminal):** comando `/status` — monitorar alocação restante ([Using Claude Code with Pro or Max](https://support.anthropic.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)).
2. **Web:** Settings → Usage — barras de sessão de 5h e semanal ([Usage limit best practices](https://support.anthropic.com/en/articles/9797557-usage-limit-best-practices)).

**Observação local:** `claude --version` → `2.1.239 (Claude Code)`. Não foi aberta sessão interativa para `/status` (evitar side effects).

**Status de autenticação (distinto de quota):** uma tentativa anterior de lançar sessão Claude nesta rodada falhou por **OAuth expirado**; o dono reportou login renovado depois. Autenticação válida **não** implica percentual de quota conhecido — os dois são fatos separados. Nenhum percentual de quota Claude foi coletado nesta sessão, autenticado ou não.

**Ferramentas de automação (handoff):** `cclimits --claude --json` — **não instalado** neste Mac (`command -v` vazio). Sem percentual restante capturado.

**Superfície machine-readable (documentada):** [`code.claude.com/docs/en/statusline`](https://code.claude.com/docs/en/statusline) descreve o objeto de status que o Claude Code entrega ao script de statusline, incluindo `rate_limits.five_hour` e `rate_limits.seven_day`, cada um com `used_percentage` e `resets_at` — com a ressalva documentada de que a disponibilidade desses campos depende do plano/conta (podem faltar). Isto é uma superfície de statusline não-interativa (o script recebe JSON via stdin a cada atualização), diferente do slash command `/status`, que é interativo e não tem saída programática oficial. Não existe fonte primária equivalente para um "statusline" do Codex — nenhuma afirmação sobre isso é feita aqui (ver §2.2).

**Inferência de pacing para F5 (não reabre o desenho, só aplica a fórmula já decidida):** o orçamento diário por provedor é `restante ÷ dias restantes`, calculado **separadamente para cada janela LONGA aplicável** (semanal e, se existir, mensal); vale o **mais restritivo** entre os orçamentos diários normalizados assim obtidos. A janela de 5h **não** entra nesse cálculo como um balde a mais — ela é (a) um portão de admissão de curto prazo (pode bloquear uma chamada mesmo com orçamento diário longo disponível) e (b) uma oportunidade de reset noturno (uma sessão de madrugada pode abrir uma janela de 5h nova antes da manhã), mas o consumo dentro dela **continua debitando** o orçamento diário calculado a partir das janelas longas. Bucket ausente/não lido é **desconhecido**, nunca tratado como zero.

**Desconhecido / lacunas:**

- Percentuais numéricos atuais da conta do dono nesta máquina/box (exigem `/status`, statusline configurado ou UI autenticada).
- Se existe limite **mensal** unificado além de créditos — não está no mesmo modelo do Cursor; não assumir paridade.

### 2.2 Codex (OpenAI / ChatGPT)

| Janela | O que a fonte diz | Confiança |
|--------|-------------------|-----------|
| **5 horas** | Limite de uso em janela de cinco horas; nova janela começa na primeira mensagem em Work ou Codex **após** o fim da janela anterior. Pode esgotar antes de 5h de relógio. | Documentado — [Managing usage with GPT-6 Astra in Work and Codex](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex) |
| **Semanal** | Limite semanal no mesmo pool Work/Codex; onde ambos aplicam, é preciso ter saldo nos **dois**. | Documentado — mesmo artigo; [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan) |
| **Mensal** | Não descrito como terceira janela fixa no help de Codex; resets comprados/banked alteram data da semana. | Documentado — artigo 20001516 (banked/purchased resets) |

**Como ver restante (oficial):**

1. **Codex CLI:** `/status` na sessão ativa ([Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)). Interativo; sem API headless documentada equivalente ao `statusline` do Claude Code — nenhuma superfície machine-readable de terceiros é assumida aqui.
2. **Web:** Settings → Usage / painel de uso Codex.

**Observação local:** `codex --version` → `codex-cli 0.153.2`. `codex-cli-usage` e `cclimits` **não** instalados.

**Instantâneo de conta verificado nesta sessão (operador, app Codex — não a VPS):** `/status` do app Codex do operador reportou **90% restante na janela de 5h** e **70% restante na semanal** no momento da checagem. Isto é uma **observação pontual desta conta e desta máquina**, não uma medição da conta usada pela VPS (que pode ser outra assinatura) nem um valor estável — registrado no manifest do handoff, não neste documento como fato permanente. Não usar como proxy para quota da VPS.

**Desconhecido:** percentuais atuais na VPS; plano exato (Plus/Pro/Business) não verificado nesta sessão.

**Inferência de pacing para F5:** mesma fórmula da §2.1 — orçamento diário `restante ÷ dias restantes` por janela longa aplicável (semanal, e mensal se existir), mais restritivo entre elas; 5h é portão de admissão + oportunidade de reset noturno, sempre debitando o orçamento diário; não assumir janela mensal do Cursor para Codex.

### 2.3 Cursor Agent / CLI

| Janela | O que a fonte diz | Confiança |
|--------|-------------------|-----------|
| **Mensal (ciclo de cobrança)** | Uso incluído reseta **mensalmente** com o ciclo de billing; data no Spending dashboard. Dois pools: “Cursor Models” e “Other Models”. | Documentado — [Usage and limits](https://cursor.com/help/models-and-usage/usage-limits) |
| **5 horas / semanal** | **Não** constam no help oficial de limites do Cursor (diferente de Claude/Codex). | Documentado (ausência) — mesma URL |
| **CLI** | Comando `/usage` mostra medidores incluídos, on-demand, plano e data de reset do ciclo ([CLI Changelog](https://cursor.com/docs/cli/changelog)). | Documentado |

**Observação local:**

- `agent` / `cursor-agent` → `2026.09.10-fd3934a`.
- `agent status --format json` → autenticado (detalhes de conta omitidos aqui; não repetir em logs públicos).

**Ferramentas handoff:** `cursor-cli-usage json` — **não instalado**. `cclimits --cursor --json` — **não instalado**.

**Inferência de pacing para F5:** para Cursor a única janela longa documentada é o **ciclo mensal de billing**; aplicar a mesma fórmula (`restante ÷ dias restantes do ciclo`) usando só essa janela, salvo leitura futura de `/usage` que mostre outro medidor. Não inventar janela de 5h para Cursor sem evidência oficial — bucket ausente é desconhecido, não zero.

### 2.4 Tabela comparativa (planejamento F5)

| Provedor | Janela curta (admissão) | Janela(s) longa(s) para orçamento diário | Comando / UI preferido | Superfície machine-readable | Probe automático (handoff) nesta sessão |
|----------|--------------------------|-------------------------------------------|-------------------------|------------------------------|----------------------------------------|
| Claude Code | ~5h sessão | Semanal (+ créditos) | `/status`, Settings → Usage | `statusline` JSON: `rate_limits.five_hour`/`seven_day` (disponibilidade varia) | `cclimits` ausente |
| Codex | 5h | Semanal | `/status`, Settings → Usage | Nenhuma documentada; `/status` é interativo | `codex-cli-usage` / `cclimits` ausentes |
| Cursor | — | Mensal (billing) | `/usage`, Spending dashboard | Nenhuma documentada além do `/usage` interativo | `cursor-cli-usage` ausente |

**Contradição a evitar:** blogs de terceiros que tratam os três como “5h + semanal + mensal” para todos — **só Cursor** tem mensal oficial claro; **não** generalizar. Também não generalizar a superfície `statusline` do Claude Code para Codex/Cursor sem fonte primária equivalente.

---

## 3. Inventário local seguro (Mac desta sessão)

Executado com `command -v` e flags de versão apenas. **Não** é inventário da VPS/box.

| Binário | Presente | Versão observada |
|---------|--------|------------------|
| `agent` / `cursor-agent` | Sim | `2026.09.10-fd3934a` |
| `claude` | Sim | `2.1.239 (Claude Code)` |
| `codex` | Sim | `0.153.2` |
| `cloudflared` | Não (PATH) | — |
| `cclimits` | Não | — |
| `cursor-cli-usage` | Não | — |
| `codex-cli-usage` | Não | — |
| `hermes` | Não verificado | — |

**Assinaturas / planos:** não inferidos a partir de binários. Confirmar plano em cada painel (Anthropic Usage, ChatGPT Usage, Cursor Spending).

---

## 4. Inventário VPS — lacunas e comandos para o dono

Sem SSH nesta sessão. [daily-reports-vps-handoff.md](./daily-reports-vps-handoff.md) é o **prompt** a rodar por quem tem acesso — não contém evidência já coletada; a evidência só existirá depois de alguém executá-lo na VPS.

Rodar **na box** (usuário típico `box`, ajustar caminhos):

```bash
# Versões de CLIs (somente leitura)
command -v hermes cloudflared python3 git claude codex agent cursor-agent 2>/dev/null
hermes --version 2>/dev/null || true
cloudflared --version 2>/dev/null || true
python3 --version

# Processos de borda — só PID, nunca argv (argv de cloudflared pode conter --token)
pgrep -f 'cloudflared tunnel' || echo 'cloudflared: sem processo'
pgrep -f 'hermes gateway run' || echo 'hermes: sem processo'
# Listener por porta, sem nome de processo/args (evita vazar linha de comando)
netstat -ltn 2>/dev/null | grep -E ':(9900|9910|9911)[[:space:]]' || true

# Checkout swarm-memory (caminho pode variar)
ls -la "$HOME/src/mathai-context-engine" "$HOME/services/a2a-broker" 2>/dev/null || true
```

**Gaps conhecidos (wiki + docs do repo):**

- Porta loopback do futuro servidor de reports **ainda não definida** no repositório (F3); até lá, não publicar hostname.
- Hermes em `127.0.0.1:9900`, broker A2A em `127.0.0.1:9910` ([install-auth-broker-vps.md](./install-auth-broker-vps.md)).
- `cloudflared` na box como processo longo (nota Hermes gateway); named tunnel deve estar na **conta da zona** ([hermes-gateway operacional](https://github.com/MathBorgess/mathai-wiki/blob/main/estudos/time-de-agentes/2026-09-08-hermes-gateway-operacional.md) — path no vault).

---

## 5. `reports.mathai.com.br` — tunnel, DNS e Access (fail-closed)

Princípio do desenho: **sem Access, o report é público** e vaza contexto privado. Ordem **fail-closed** (não expor conteúdo antes da política).

### 5.1 Pré-requisitos (decisão humana)

1. Conta Cloudflare com zona `mathai.com.br` e **o mesmo account** do named tunnel ([erro 1033 cross-account](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/create-local-tunnel/) — evidência operacional na wiki).
2. Servidor de reports escutando em loopback na VPS (F3) — `REPORTS_PORT` é **TBD**, decisão de F3, não desta fase nem da sessão 02. Até F3 existir, usar apenas uma porta provisória confirmada livre (`netstat`), sem subir nenhum processo real nela.
3. **Não** colocar Cloudflare Access em `a2a.mathai.com.br` ([AGENTS.md](../../AGENTS.md), [install-auth-broker-vps.md](./install-auth-broker-vps.md)).
4. Descobrir **como o tunnel existente é gerido** antes de tocar nele — muda o procedimento (§5.2). Fonte primária: [Set up Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/) (visão geral remotely-managed vs. locally-managed) e [Local tunnel management](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/create-local-tunnel/).

### 5.2 Sequência recomendada — Access **antes** da rota, nos dois modos de gestão do tunnel

Princípio inegociável, nos dois modos abaixo: a aplicação Access e a policy `Allow` (com OTP) existem **antes** de qualquer rota pública apontar para o serviço de reports. Rotear primeiro e proteger depois deixa uma janela real de exposição, mesmo que curta.

| Passo | Ação | Fail-closed |
|-------|------|-------------|
| 1 | Serviço de reports (quando existir) só em `127.0.0.1:REPORTS_PORT`; validar com `curl` local | Nada na internet ainda |
| 2 | Zero Trust → **Access** → Applications → Add → **Self-hosted** → hostname `reports.mathai.com.br` | App existe; nenhuma rota pública ainda |
| 3 | Identity provider **One-time PIN** ([doc OTP](https://developers.cloudflare.com/cloudflare-one/identity/one-time-pin/)), se ainda não habilitado na conta | OTP configurado |
| 4 | Policy **Allow** → Include → seletor **Emails** (não `Emails ending in`) = e-mail exato do dono; **nunca** `Everyone` nem Bypass | Default deny fora da lista |
| 5 | **Só agora**, adicionar a rota pública, escolhendo o modo de gestão real do tunnel (5.2.a ou 5.2.b) | DNS público só passa a existir neste passo |
| 6 | Smoke sem cookie: `curl -I https://reports.mathai.com.br/` → redirect/login Access (nunca `200` com HTML) | |
| 7 | Smoke com e-mail permitido: login completo com OTP do dono; origem pode devolver `502` (normal, sem serviço real ainda) — o que se prova é a fronteira de Access, não a aplicação | |
| 8 | Smoke com e-mail **fora** da policy: nenhum PIN deve chegar; se chegar, a policy está errada (provavelmente `Emails ending in` ou falta o `Require` de login method) — parar e corrigir antes de prosseguir | |

**5.2.a — Tunnel gerido remotamente (dashboard, `cloudflared tunnel run` sem `config.yml` local; configuração fica no Cloudflare).** Fonte primária: [Set up Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/setup/).

1. `Networking` → `Tunnels` → selecionar o tunnel já usado por `a2a.mathai.com.br` (mesma conta; contas diferentes causam erro 1033).
2. `Routes` → `Add a public hostname`: subdomínio `reports`, domínio `mathai.com.br`, `Service` = `HTTP` → `127.0.0.1:REPORTS_PORT` (placeholder até F3 existir).
3. As rotas existentes (`a2a.mathai.com.br` e qualquer catch-all `*`) **não são tocadas** por este passo — o dashboard adiciona uma rota nova, não substitui a lista. Confirmar isso conferindo a lista de rotas antes e depois.
4. `Save hostname`. O CNAME `reports` → `<tunnel-id>.cfargotunnel.com` (proxied) é criado automaticamente; não criar manualmente por outro caminho.

**5.2.b — Alternativa: tunnel gerido localmente (`config.yml` com bloco `ingress:`), só se a VPS já usar esse modo.** Fonte primária: [Local tunnel management](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/create-local-tunnel/) e [Local ingress rules](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/ingress/).

1. Ler o `config.yml` atual **antes** de editar; a ordem de `ingress:` importa — regras são avaliadas de cima para baixo e a **última entrada precisa ser um catch-all** (`service: http_status:404` ou equivalente). Preservar toda entrada existente (`a2a.mathai.com.br`, qualquer outro hostname, o catch-all) na mesma ordem relativa.
2. Adicionar a nova entrada de `reports.mathai.com.br` **antes** do catch-all, apontando para `http://127.0.0.1:REPORTS_PORT`.
3. Validar a sintaxe (`cloudflared tunnel ingress validate`) antes de recarregar o processo — um YAML quebrado pode derrubar `a2a.mathai.com.br` junto.
4. Criar o DNS: `cloudflared tunnel route dns <tunnel-id-ou-nome> reports.mathai.com.br` (não duplicar CNAME manual se a rota já existir de outra forma).
5. Recarregar/reiniciar o `cloudflared` só depois da validação; conferir que `a2a.mathai.com.br` continua respondendo depois do reload.

**OTP (documentado):** e-mail de `noreply@notify.cloudflare.com`; PIN 10 minutos, uso único; usuário fora da policy **não** recebe e-mail (a UI sempre diz que enviou — não usar isso como teste de DNS ou de propagação).

**Placeholder seguro:** até F3 existir, a porta de origem não deve ter nenhum processo real ouvindo nela — o teste do passo 7 vale com `502` (sem origem), nunca com conteúdo real. Não subir um serviço "provisório" só para o smoke devolver `200`; isso simula um comando/serviço de report ativo que não existe.

**Pós-condição:** qualquer regressão que remova a policy Access, adicione Bypass, ou perca cobertura de algum hostname existente na ingress list é incidente de exposição — reverter (remover Public Hostname/entrada de `reports`, ou restaurar o `config.yml` anterior) antes de investigar a causa.

### 5.3 O que não fazer

- Publicar HTML estático em bucket público sem Access.
- Reutilizar policy Bypass “Everyone” em rotas do report ([Access policies](https://developers.cloudflare.com/cloudflare-one/policies/access/)).
- Misturar hostname do broker OAuth com o do report na mesma policy sem revisar Device Flow.
- Rotear `reports.mathai.com.br` (dashboard ou `config.yml`) antes de a aplicação Access e a policy `Allow` existirem.
- Remover ou reordenar hostnames existentes (`a2a.mathai.com.br`, catch-all) ao editar `config.yml` para adicionar `reports`.
- Subir um servidor real ou "de teste" na porta de origem antes de F3 existir, só para um smoke devolver `200`.

---

## 6. Implicações para fases seguintes (somente referência)

| Fase | Dependência deste F0 |
|------|----------------------|
| F5 dispatcher | Probes `cclimits` / `cursor-cli-usage` / `codex-cli-usage` opcionais na máquina do operador; fallback contador local se `unknown`. |
| F3 servidor | Escolher `REPORTS_PORT` — decisão de F3, não deste F0 nem da sessão 02 (que só entrega instruções, não decisões de config). |
| F2/F4 | Sem impacto direto em quota além do roteamento handoff. |

---

## 7. Lacunas e contradições registradas

1. **Percentuais de quota atuais na VPS** — exigem sessões autenticadas (`/status`, `/usage`, dashboards) rodadas na própria box; não coletados aqui. O instantâneo do operador (Codex 90%/70%, §2.2) é de outra máquina/conta e não deve ser usado como proxy.
2. **Inventário real da VPS** — não existe ainda; [daily-reports-vps-handoff.md](./daily-reports-vps-handoff.md) é o prompt para produzi-lo, não o inventário em si.
3. **Porta do serviço de reports (`REPORTS_PORT`)** — TBD, decisão de F3; bloqueia o passo 5 do tunnel (§5.2) até existir.
4. **Cursor não tem 5h/semanal oficiais** — a fórmula de pacing de F5 usa janelas longas aplicáveis por provedor (semanal e/ou mensal, o mais restritivo); para Cursor isso reduz a uma única janela (mensal); não generalizar "5h + semanal + mensal" para os três.
5. **Material de terceiros** (blogs agregadores de limites) — útil como pista, **não** substitui URLs da §2.
6. **Autenticação Claude renovada, quota não verificada** — o dono reportou login Claude restabelecido após falha de OAuth; isso não produz um percentual de quota (§2.1).

---

## 8. Fontes primárias (URLs)

- Anthropic: https://support.anthropic.com/en/articles/9797557-usage-limit-best-practices
- Anthropic: https://support.anthropic.com/en/articles/8325606-what-is-claude-pro
- Anthropic: https://support.anthropic.com/en/articles/11049741-what-is-the-max-plan
- Anthropic: https://support.anthropic.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
- Anthropic: https://support.anthropic.com/en/articles/11647753-understanding-usage-and-length-limits
- OpenAI: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- OpenAI: https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex
- Cursor: https://cursor.com/help/models-and-usage/usage-limits
- Cursor: https://cursor.com/docs/cli/changelog
- Anthropic (statusline, superfície machine-readable): https://code.claude.com/docs/en/statusline
- Cloudflare tunnel (visão geral, remotely-managed): https://developers.cloudflare.com/tunnel/setup/
- Cloudflare tunnel (locally-managed, `create-local-tunnel`): https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/create-local-tunnel/
- Cloudflare tunnel (ingress rules locais): https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/ingress/
- Cloudflare Access policies: https://developers.cloudflare.com/cloudflare-one/policies/access/
- Cloudflare Access — self-hosted public app: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/
- Cloudflare OTP: https://developers.cloudflare.com/cloudflare-one/identity/one-time-pin/

**Repositório (contexto, não quota):** [AGENTS.md](../../AGENTS.md), [install-auth-broker-vps.md](./install-auth-broker-vps.md).
