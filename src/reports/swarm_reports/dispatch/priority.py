"""Fit dispatchable jobs into a daily quota budget, priority first.

Order is fixed by the design note: P0 handoffs, then drafts, then mechanism
improvements. Whatever does not fit is deferred and must be visible in the
next report, never silently dropped.

**No backfill.** Once a job does not fit, nothing after it is admitted either,
even if a later, smaller job would have fit in the leftover. Backfilling would
let a cheap mechanism improvement run on the quota a deferred P0 needed, which
inverts the exact priority the design note fixes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

Kind = Literal["p0", "draft", "improvement"]
_ORDER: dict[str, int] = {"p0": 0, "draft": 1, "improvement": 2}


def _check_cost(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{field} must be >= 0, got {value!r}")
    return value


@dataclass(frozen=True)
class Job:
    task_id: str
    kind: Kind
    cost_pct: float  # estimated share of the daily quota budget
    estimated: bool = True  # False only when a measured cost is available

    def __post_init__(self) -> None:
        if self.kind not in _ORDER:
            raise ValueError(f"unknown job kind {self.kind!r}")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        object.__setattr__(self, "cost_pct", _check_cost(self.cost_pct, field="cost_pct"))


@dataclass(frozen=True)
class FitResult:
    admitted: list[Job]
    deferred: list[Job]
    spent_pct: float


def fit_to_budget(jobs: Sequence[Job], budget_pct: float) -> FitResult:
    budget_pct = _check_cost(budget_pct, field="budget_pct")
    ordered = sorted(jobs, key=lambda j: _ORDER[j.kind])
    admitted: list[Job] = []
    deferred: list[Job] = []
    spent = 0.0
    full = False
    for job in ordered:
        if full or spent + job.cost_pct > budget_pct + 1e-9:
            full = True
            deferred.append(job)
            continue
        admitted.append(job)
        spent += job.cost_pct
    return FitResult(admitted=admitted, deferred=deferred, spent_pct=spent)


__all__ = ["FitResult", "Job", "fit_to_budget"]
