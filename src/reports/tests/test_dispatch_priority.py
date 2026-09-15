from __future__ import annotations

import pytest
from swarm_reports.dispatch.priority import Job, fit_to_budget


def test_p0_always_admitted_before_drafts_and_improvements():
    jobs = [
        Job("imp-1", "improvement", 3.0),
        Job("draft-1", "draft", 3.0),
        Job("p0-1", "p0", 3.0),
    ]
    result = fit_to_budget(jobs, budget_pct=6.0)
    assert [j.task_id for j in result.admitted] == ["p0-1", "draft-1"]
    assert [j.task_id for j in result.deferred] == ["imp-1"]


def test_deferred_jobs_are_never_silently_dropped():
    jobs = [Job("p0-1", "p0", 5.0), Job("p0-2", "p0", 5.0)]
    result = fit_to_budget(jobs, budget_pct=5.0)
    assert result.deferred == [jobs[1]]
    assert result.spent_pct == 5.0


def test_everything_fits_when_budget_is_generous():
    jobs = [Job("p0-1", "p0", 1.0), Job("draft-1", "draft", 1.0), Job("imp-1", "improvement", 1.0)]
    result = fit_to_budget(jobs, budget_pct=10.0)
    assert result.deferred == []
    assert result.spent_pct == 3.0


def test_zero_budget_defers_everything():
    jobs = [Job("p0-1", "p0", 0.1)]
    result = fit_to_budget(jobs, budget_pct=0.0)
    assert result.admitted == []
    assert result.deferred == jobs


def test_a_cheap_improvement_never_backfills_over_a_deferred_p0():
    """Priority inversion guard: the leftover belongs to the deferred P0's retry."""
    jobs = [Job("p0-big", "p0", 9.0), Job("imp-tiny", "improvement", 0.5)]
    result = fit_to_budget(jobs, budget_pct=5.0)
    assert result.admitted == []
    assert [j.task_id for j in result.deferred] == ["p0-big", "imp-tiny"]
    assert result.spent_pct == 0.0


def test_ordering_within_a_kind_is_stable_so_reruns_agree():
    jobs = [Job(f"p0-{i}", "p0", 1.0) for i in range(5)]
    first = fit_to_budget(jobs, budget_pct=3.0)
    second = fit_to_budget(jobs, budget_pct=3.0)
    assert [j.task_id for j in first.admitted] == [j.task_id for j in second.admitted] == ["p0-0", "p0-1", "p0-2"]


def test_float_accumulation_does_not_defer_a_job_that_exactly_fits():
    jobs = [Job("a", "p0", 0.1), Job("b", "p0", 0.2)]
    result = fit_to_budget(jobs, budget_pct=0.3)  # 0.1 + 0.2 != 0.3 in binary floating point
    assert result.deferred == []


@pytest.mark.parametrize("cost", [-1.0, float("nan"), float("inf"), "3", True])
def test_invalid_job_costs_are_rejected(cost):
    with pytest.raises(ValueError):
        Job("x", "p0", cost)


def test_unknown_job_kind_is_rejected():
    with pytest.raises(ValueError):
        Job("x", "someday-maybe", 1.0)


def test_empty_task_id_is_rejected():
    with pytest.raises(ValueError):
        Job("  ", "p0", 1.0)


@pytest.mark.parametrize("budget", [-1.0, float("nan"), float("inf")])
def test_invalid_budget_is_rejected(budget):
    with pytest.raises(ValueError):
        fit_to_budget([Job("a", "p0", 1.0)], budget)
