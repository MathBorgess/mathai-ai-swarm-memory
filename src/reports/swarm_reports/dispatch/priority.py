"""Fit dispatchable jobs into a daily quota budget, priority first.

Order is fixed by the design note: P0 handoffs, then drafts, then mechanism
improvements. Whatever does not fit is deferred and must be visible in the
next report, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["p0", "draft", "improvement"]
_ORDER: dict[Kind, int] = {"p0": 0, "draft": 1, "improvement": 2}


@dataclass(frozen=True)
class Job:
    task_id: str
    kind: Kind
    cost_pct: float  # estimated share of the daily quota budget


@dataclass(frozen=True)
class FitResult:
    admitted: list[Job]
    deferred: list[Job]
    spent_pct: float


def fit_to_budget(jobs: list[Job], budget_pct: float) -> FitResult:
    ordered = sorted(jobs, key=lambda j: _ORDER[j.kind])
    admitted: list[Job] = []
    deferred: list[Job] = []
    spent = 0.0
    for job in ordered:
        if spent + job.cost_pct <= budget_pct:
            admitted.append(job)
            spent += job.cost_pct
        else:
            deferred.append(job)
    return FitResult(admitted=admitted, deferred=deferred, spent_pct=spent)
