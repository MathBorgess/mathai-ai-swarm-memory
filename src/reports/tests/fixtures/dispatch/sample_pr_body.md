# Summary

Implements the dispatch core.

## Decisões que merecem pergunta

- tipo: hardcode, src/reports/swarm_reports/dispatch/quota.py:24, pergunta: teto de 10% é razoável?, por que importa: afeta todo provider desconhecido
- tipo: heuristic_with_ceiling, src/reports/swarm_reports/dispatch/claims.py:70, pergunta: 2h é o timeout certo?, por que importa: resume cedo demais falha o job

## Test plan

- pytest
