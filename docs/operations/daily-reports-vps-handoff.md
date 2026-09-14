# Daily reports — handoff de VPS (F0–F6)

Documento operacional no repositório `MathBorgess/mathai-ai-swarm-memory`. Não substitui o runbook do broker A2A (`docs/operations/install-auth-broker-vps.md`). Nada aqui é deploy automático: cron do ciclo diário permanece **desligado** nas duas semanas manuais.

## Estado alvo (não confundir com `main`)

| Artefato | Branch / PR | Observação |
|---|---|---|
| F0 ops | `codex/daily-reports-f0` → PR #12 | Pesquisa de quota e Access |
| F1–F4 | `codex/daily-reports-f1` … `f4` → PRs #13–#16 | Stack empilhada |
| F5 | `codex/daily-reports-f5` → PR #18 | Dispatch, quota, digest |
| F6 | `codex/daily-reports-f6` → PR #17 | Discovery somente leitura |
| Pesos RES (wiki) | PR #88 em `mathai-wiki` | `brand/metrics/res-weights.json` |

Fixar o commit exato após merge aprovado pelo dono; até lá, instalar a partir do SHA que o PR #17 apontar no push de assurance.

## Inventário antes de mudar qualquer coisa

O agente na VPS deve registrar **localmente** (nunca no Git, wiki ou HTML do report):

1. Usuário Linux, diretório do clone, versão de `python3`, presença de `gh`, CLIs `agent` / `claude` / `codex` (se usados no dispatch).
2. Serviços já existentes: broker A2A (`a2a.mathai.com.br`), Hermes em loopback, `cloudflared`, unidades systemd reais (nomes **não** presumidos neste doc).
3. Named tunnel Cloudflare da zona `mathai.com.br`: hostnames atuais, se `reports.mathai.com.br` já existe, se Access está aplicado **somente** ao hostname de reports (nunca ao origin público OAuth `a2a.mathai.com.br`).
4. Caminhos de estado fora do Git: SQLite do broker, `.env`, `state_dir` de reports, `output_dir`, policy JSON.
5. Backup verificável dos arquivos acima antes de upgrade.

## Separação de hostnames (inegociável)

| Hostname | Proteção | Função |
|---|---|---|
| `https://a2a.mathai.com.br` | OAuth Device Flow público | Broker MCP/A2A — **sem** Cloudflare Access na frente |
| `https://reports.mathai.com.br` | Cloudflare Access (OTP e-mail do dono) **antes** de expor HTML | Reports estáticos + `POST /evening` |
| Hermes | `127.0.0.1` apenas | Nunca publicar |

## Configuração JSON (reports)

Copiar `config/reports-policy.example.json` para um path absoluto fora do Git (ex.: `/var/lib/mathai-reports/policy.json`). O exemplo de **reports** (não policy) segue o schema em `swarm_reports.config.ReportsConfig`:

```json
{
  "wiki_dir": "<INVENTARIAR: clone absoluto mathai-wiki>",
  "state_dir": "<INVENTARIAR: dir 0700 fora do Git>",
  "weights_path": "<INVENTARIAR: brand/metrics/res-weights.json no wiki>",
  "output_dir": "<INVENTARIAR: HTML gerado>",
  "owner_id": "owner",
  "timezone": "America/Recife",
  "public_origin": "https://reports.mathai.com.br",
  "dispatch_policy_path": "<INVENTARIAR: policy.json>",
  "discovery": {
    "github": { "repos": ["MathBorgess/mathai-ai-swarm-memory"], "max_pages": 3 },
    "linear": { "token_env": "LINEAR_API_TOKEN" },
    "wiki_log": { "path": "<INVENTARIAR>/wiki/log.md" }
  },
  "server": {
    "bind_host": "127.0.0.1",
    "port": 8787,
    "auth_mode": "tunnel-loopback",
    "max_body_bytes": 262144,
    "request_timeout_seconds": 10,
    "dispatch_command": ["<INVENTARIAR: venv>/bin/python", "-m", "swarm_reports.cli", "report", "evening", "--job-stdin", "--config", "<reports.json>"],
    "dispatch_timeout_seconds": 900,
    "max_attempts": 5
  }
}
```

- `weights_path` é **JSON** (`res-weights.json`), não YAML.
- Quota na manhã: sem `quota.probes` configurados, o dispatch marca quota como **indisponível** (não inventa %). Probes suportados: arquivo JSON absoluto ou `command` argv — ver `swarm_reports.dispatch.probes.read_probe`.
- Discovery F6: somente leitura; itens “fora do plano” não entram no denominador de conclusão.

## Entrypoints CLI

Com venv que instala `src/reports` e o delegador do broker:

```bash
python3 -m swarm_reports.cli report morning --config <abs/reports.json>
python3 -m swarm_reports.cli report evening --config <abs/reports.json> --input <abs/evening.json>
python3 -m swarm_reports.cli report serve --config <abs/reports.json>
```

`mathai-swarm report …` repassa quando o pacote do broker está no mesmo venv (`src/auth-broker`).

## Provisionamento mínimo

1. Clone/atualize `mathai-ai-swarm-memory` na revisão aprovada; `pip install -e src/reports` (e dependências do broker se for drenar outbox via dispatch).
2. Clone `mathai-wiki` alinhado ao PR de pesos #88 quando for calcular RES em produção.
3. Criar `state_dir`, `output_dir`, policy e reports JSON com permissões restritas; segredos só em `.env` referenciado por `token_env` / broker.
4. Configurar hostname `reports.mathai.com.br` no **mesmo** named tunnel já usado na zona, apontando para `http://127.0.0.1:<porta do serve>` — porta e nome do serviço são **inventário VPS**.
5. Criar aplicação Cloudflare Access para `reports.mathai.com.br` (OTP e-mail do dono). Validar JWT/AUD se usar `auth_mode: cloudflare-access-jwt` em vez de `tunnel-loopback`.
6. **Não** habilitar cron Hermes para `report morning|evening` até o dono encerrar as duas semanas manuais.

## Verificação (local na VPS)

| Prova | Comando / critério |
|---|---|
| Loopback + auth | `curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:<port>/healthz` → 200 |
| Access nega anônimo | Request externo sem cookie/JWT → 302/403 na borda |
| Ciclo sintético | manhã → servir HTML → POST ou `--input` evening → `serve --drain-outbox` |
| Identity | `bash tests/test-hermes-identity-sync.sh` no clone |
| Reports tests | `cd src/reports && PYTHONPATH=. python3 -m pytest -q` |

## Rollback

1. Parar o processo `report serve` (unidade inventariada).
2. Remover ou despublicar hostname `reports.mathai.com.br` no tunnel.
3. Restaurar backup de `state_dir`, policy e `.env`.
4. Voltar o worktree Git ao SHA anterior registrado no inventário.

## Lacunas que só o ambiente resolve

- Formato exato dos arquivos JSON de quota por CLI (Cursor/Claude/Codex): operador define `quota.probes` conforme F0/PR #12; o código não adivinha endpoints.
- IDs Cloudflare (AUD Access, tunnel UUID): inventário VPS.
- Nomes de unit files systemd e usuário de serviço: inventário VPS.

Prompt copiável para agente na VPS: ver `$RUN/VPS-SETUP-UPGRADES.md` no handoff run (gerado na sessão 20).
