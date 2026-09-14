from __future__ import annotations

from datetime import date

from swarm_reports.metrics.daily import ChecklistItem
from swarm_reports.metrics.execution import compute_date_drift, count_open_p0


def test_date_drift_carries_first_planned_across_days():
    items = [
        ChecklistItem(
            task_id="MAT-193",
            text="simulado",
            done=False,
            frozen=True,
            added_after_freeze=False,
            is_p0=True,
            classification=None,
            first_planned=date(2026, 9, 13),
            line_number=1,
        )
    ]
    drift = compute_date_drift(items, date(2026, 9, 15))
    assert drift["MAT-193"] == 2


def test_max_three_p0_counted():
    items = [
        ChecklistItem(
            task_id=f"MAT-{200+i}",
            text=f"p0 {i}",
            done=False,
            frozen=True,
            added_after_freeze=False,
            is_p0=True,
            classification=None,
            first_planned=date(2026, 9, 14),
            line_number=i,
        )
        for i in range(5)
    ]
    assert len(count_open_p0(items)) == 3
