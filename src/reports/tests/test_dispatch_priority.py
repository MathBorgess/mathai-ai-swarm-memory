from __future__ import annotations

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
