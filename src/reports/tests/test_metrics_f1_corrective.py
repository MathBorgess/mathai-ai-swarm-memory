"""F1 corrective regression tests (review session 03)."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from swarm_reports.metrics.daily import parse_daily_markdown, split_frontmatter
from swarm_reports.metrics.execution import (
    EveningSubmission,
    compute_completion,
    compute_date_drift,
    compute_days_without_closure,
    count_open_p0,
)
from swarm_reports.metrics.daily import ChecklistItem
from swarm_reports.metrics.ids import hash_task_text
from swarm_reports.metrics.res import (
    PostMetrics,
    ResWeights,
    compute_res,
    load_res_weights,
    period_res_summary,
)
from swarm_reports.metrics.state import (
    FrozenItem,
    ReportsState,
    apply_morning_freeze,
    save_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "metrics"


# --- PostMetrics / RES ---


def test_from_mapping_preserves_zero_outside_fraction_and_reach():
    m = PostMetrics.from_mapping(
        {"reach": 0, "outside_fraction": 0, "signals": {"reactions": 1}}
    )
    assert m.reach == 0.0
    assert m.outside_fraction == 0.0


def test_from_mapping_rejects_bool_as_numeric_signal():
    with pytest.raises((TypeError, ValueError)):
        PostMetrics.from_mapping({"reach": 100, "signals": {"reactions": True}})


def test_from_mapping_rejects_negative_signal_count():
    with pytest.raises(ValueError):
        PostMetrics.from_mapping({"reach": 100, "signals": {"reactions": -1}})


def test_validate_rejects_non_finite_and_fraction_above_one():
    with pytest.raises(ValueError):
        PostMetrics(reach=math.inf).validate()
    with pytest.raises(ValueError):
        PostMetrics(reach=100, outside_fraction=1.5).validate()
    with pytest.raises(ValueError):
        PostMetrics(reach=100, outside_fraction=-0.1).validate()


def test_compute_engagement_rejects_negative_signal():
    from swarm_reports.metrics.res import compute_engagement

    with pytest.raises(ValueError):
        compute_engagement({"reactions": 1.0}, {"reactions": -2})


def test_signal_weights_no_linkedin_fallback():
    weights = ResWeights(version=1, platforms={"twitter": {"reactions": 1.0}})
    with pytest.raises(KeyError):
        weights.signal_weights("unknown")


def test_load_res_weights_requires_schema(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_res_weights(bad)


def test_load_res_weights_rejects_empty_platforms(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "platforms": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_res_weights(bad)


def test_compute_res_invalid_zero_policy():
    with pytest.raises(ValueError):
        compute_res(1.0, 100.0, None, zero_reach_policy="bogus")  # type: ignore[arg-type]


def test_compute_res_rejects_outside_fraction_above_one():
    with pytest.raises(ValueError):
        compute_res(10.0, 100.0, 1.01)


def test_period_res_summary_counts_all_posts_mean_only_scored():
    summary = period_res_summary(
        [(True, 80.0), (True, None), (False, 56.1), (False, None)],
        period_days=7,
    )
    assert summary["guided"]["post_count"] == 2
    assert summary["guided"]["average_res"] == 80.0
    assert summary["spontaneous"]["post_count"] == 2
    assert summary["spontaneous"]["average_res"] == 56.1


def test_period_res_summary_rejects_bad_period_days():
    with pytest.raises(ValueError):
        period_res_summary([], period_days=0)
    with pytest.raises(ValueError):
        period_res_summary([], period_days=-1)


# --- daily parser ---


def test_split_frontmatter_does_not_split_embedded_dashes_in_yaml():
    text = "---\ntitle: before --- after\n---\n\n# body\n"
    fm, body = split_frontmatter(text)
    assert fm["title"] == "before --- after"
    assert body.startswith("# body")


def test_inline_task_meta_on_checklist_line_not_skipped():
    text = """---
---

# 2026-09-14

## Hoje

- [ ] Do thing <!-- swarm:task-meta id=MAT-INLINE first_planned=2026-09-10 -->
"""
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    assert len(note.items) == 1
    assert note.items[0].task_id == "MAT-INLINE"
    assert note.items[0].first_planned == date(2026, 9, 10)


def test_task_meta_carried_from_previous_comment_line():
    text = """---
---

# 2026-09-14

## Hoje

<!-- swarm:task-meta id=MAT-CARRY first_planned=2026-09-11 -->
- [ ] Carried task
"""
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    assert note.items[0].task_id == "MAT-CARRY"
    assert note.items[0].first_planned == date(2026, 9, 11)


def test_stable_hash_ignores_swarm_comments_and_p0_markers():
    a = "Task alpha <!-- swarm:class procrastinação -->"
    b = "Task alpha <!-- swarm:p0 -->"
    c = "Task alpha P0"
    assert hash_task_text(a) == hash_task_text(b) == hash_task_text(c)


def test_first_planned_defaults_to_note_day_when_missing():
    text = """---
---

# 2026-09-14

## Hoje

- [ ] No meta task
"""
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    assert note.items[0].first_planned == date(2026, 9, 14)


# --- state / freeze ---


def test_morning_freeze_empty_checklist_once():
    state = ReportsState()
    assert apply_morning_freeze(state, date(2026, 9, 14), [], commit="c1") is True
    assert apply_morning_freeze(state, date(2026, 9, 14), [], commit="c2") is False
    assert state.get_day(date(2026, 9, 14)).frozen_at_commit == "c1"


def test_morning_freeze_rejects_duplicate_task_ids():
    state = ReportsState()
    items = [
        FrozenItem("dup", "a", date(2026, 9, 14)),
        FrozenItem("dup", "b", date(2026, 9, 14)),
    ]
    with pytest.raises(ValueError):
        apply_morning_freeze(state, date(2026, 9, 14), items)


def test_morning_freeze_rejects_more_than_three_p0():
    state = ReportsState()
    items = [
        FrozenItem(f"p{i}", f"t{i}", date(2026, 9, 14), is_p0=True) for i in range(4)
    ]
    with pytest.raises(ValueError):
        apply_morning_freeze(state, date(2026, 9, 14), items)


def test_save_state_atomic_and_mode_600(tmp_path: Path):
    path = tmp_path / "nested" / "reports-state.json"
    save_state(path, ReportsState())
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


# --- execution ---


def test_compute_completion_requires_same_day():
    text = (FIXTURES / "daily-frozen.md").read_text(encoding="utf-8")
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    evening = EveningSubmission(
        day=date(2026, 9, 15),
        validated_complete_ids=frozenset({"MAT-193"}),
    )
    with pytest.raises(ValueError):
        compute_completion(note, evening, frozen_snapshot_ids=["MAT-193", "MAT-216"])


def test_compute_completion_rejects_duplicate_denominator_ids():
    text = (FIXTURES / "daily-frozen.md").read_text(encoding="utf-8")
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    evening = EveningSubmission(
        day=date(2026, 9, 14),
        validated_complete_ids=frozenset({"MAT-193"}),
    )
    with pytest.raises(ValueError):
        compute_completion(note, evening, frozen_snapshot_ids=["MAT-193", "MAT-193"])


def test_count_open_p0_does_not_silently_cap():
    items = [
        ChecklistItem(
            task_id=f"MAT-{200 + i}",
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
    assert len(count_open_p0(items)) == 5


def test_date_drift_ignores_unvalidated_checkmark():
    items = [
        ChecklistItem(
            task_id="MAT-1",
            text="t",
            done=True,
            frozen=True,
            added_after_freeze=False,
            is_p0=False,
            classification=None,
            first_planned=date(2026, 9, 10),
            line_number=1,
        )
    ]
    drift = compute_date_drift(
        items,
        date(2026, 9, 14),
        validated_complete_ids=frozenset(),
    )
    assert drift["MAT-1"] == 4


def test_days_without_closure_excludes_future_and_pending_today():
    planned = {date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 14)}
    validated = {date(2026, 9, 12)}
    count = compute_days_without_closure(
        planned,
        validated_days=validated,
        range_start=date(2026, 9, 12),
        range_end=date(2026, 9, 14),
        as_of=date(2026, 9, 14),
    )
    assert count == 1  # only 2026-09-13; 14th still pending

