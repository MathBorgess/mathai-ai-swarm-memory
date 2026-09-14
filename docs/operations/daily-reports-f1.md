# Daily reports — F1: núcleo de métricas

**Fase:** F1 (parser + métricas puras). Sem servidor, cron, HTML ou escrita no vault.

**Desenho:** `mathai-wiki/estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`

## Entregue neste repositório

- Pacote `src/reports/` (`mathai-swarm-reports`, Python ≥3.12)
- Testes em `src/reports/tests/` + fixtures em `tests/fixtures/metrics/`
- CI mínimo: `.github/workflows/reports.yml`

## APIs públicas (F2–F6)

Exportadas em `swarm_reports`:

- `parse_daily_markdown`, `parse_post_markdown`
- `load_res_weights`, `compute_engagement`, `compute_res`, `period_res_summary`
- `compute_completion`, `compute_date_drift`, `compute_days_without_closure`, `compute_scope_penalty`, `count_open_p0`
- `load_state`, `save_state`, `apply_morning_freeze`, `mark_evening_validated`

## Pesos RES

Caminho canônico proposto no wiki: `brand/metrics/res-weights.json` (PR draft no vault, fora do escopo F1).

## Estado runtime

Arquivo configurável: `{state_dir}/reports-state.json` (fora do Git). Ver
`swarm_reports.metrics.state` para contrato de freeze e carryover.

## Próximas fases

| Fase | Dono |
|------|------|
| F2 | `skills/daily-plan`, HTML manhã, CLI `report morning` |
| F3 | `src/reports/server`, POST `/evening` |
| F4 | `skills/daily-review`, `report evening` |
| F5 | Dispatcher/quota/digest |
| F6 | Agentes fora do fluxo |
