# Daily reports — F5 integration (dispatcher wired into morning/evening)

Status: integrated on `codex/daily-reports-f5` atop F4. Core modules live in
`src/reports/swarm_reports/dispatch/`; runtime glue in `policy_config.py`,
`morning_dispatch.py`, `evening_autonomy.py`, `gh_cli.py`, `runtime_store.py`.

## Config

Reports JSON (`MATHAI_REPORTS_CONFIG` / `--config`):

```json
{
  "dispatch_policy_path": "/abs/reports-policy.json"
}
```

Copy `config/reports-policy.example.json` outside Git. Optional `handoff.command`
argv receives JSON on stdin `{provider, model, task_id, prompt}`.

## Morning

After the checklist freeze, admitted handoffs run once per `(day, task_id)`
with quota pacing, weighted routing, and durable claims. Deferred jobs and
dispatch notes appear under **Desde a última rodada**. Costs stay **unknown**
unless a probe supplied measured quota.

## Evening

When `evening.publish.mode = gh` opens a wiki PR, F5 parses digest cards,
stores top 5 for the next **Revisar** tab, and evaluates merge policy (head-pinned
`gh pr merge --match-head-commit` when allowed). T0 suggestions stay local unless
`digest.allow_external_comments` is true.

## Tests

```bash
cd src/reports && PYTHONPATH=. python3 -m pytest -q
```

Includes `tests/test_f5_integration.py` (fakes only — no live `gh`/provider).

## Decisões que merecem pergunta

- tipo: heuristic_with_ceiling, src/reports/swarm_reports/dispatch/morning_dispatch.py:1, pergunta: probes de quota no morning ainda não chamam os CLIs reais — só leituras injetadas ou `unknown` explícito; quando ligar F0 na VPS?, por que importa: até lá o pacing usa janelas `missing`/`estimated` e o operador não vê percentuais medidos no HTML.
- tipo: spec_deviation, config/reports-policy.example.json:69, pergunta: `handoff.command: null` no exemplo é intencional para demo sem subprocess — qual argv do adaptador json-stdio o dono quer na VPS?, por que importa: sem comando os handoffs só registram claim `done` sem disparar agente.
- tipo: contract_change, src/reports/swarm_reports/evening/session.py:789, pergunta: autonomia pós-PR ignora falhas silenciosamente (`best-effort`); deve falhar a noite quando `gh pr view` quebra?, por que importa: hoje o ledger mostra PR aberto mesmo se digest/merge não rodaram.
