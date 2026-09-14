from __future__ import annotations

from datetime import date
from pathlib import Path

from swarm_reports.metrics.daily import parse_daily_markdown
from swarm_reports.metrics.execution import (
    EveningSubmission,
    UnplannedCompletion,
    compute_completion,
    compute_scope_penalty,
    frozen_denominator_ids,
)
from swarm_reports.metrics.ids import stable_task_id
from swarm_reports.metrics.state import FrozenItem, ReportsState, apply_morning_freeze

FIXTURES = Path(__file__).parent / "fixtures" / "metrics"


def test_parse_frozen_markers_and_added_after_freeze():
    text = (FIXTURES / "daily-frozen.md").read_text(encoding="utf-8")
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    frozen = [item for item in note.items if item.frozen]
    added = [item for item in note.items if item.added_after_freeze]
    assert len(frozen) == 2
    assert len(added) == 1
    assert note.frozen_snapshot_commit == "abc123"
    assert frozen[0].task_id == "MAT-193"
    assert frozen[0].is_p0 is True


def test_stable_id_prefers_mat_then_event_then_hash():
    assert stable_task_id("Follow up MAT-205 portão") == "MAT-205"
    event_id = stable_task_id(
        "14:00 — SIMULADO 0 Databricks",
        [{"title": "Databricks — SIMULADO 0", "event_id": "evt-1"}],
    )
    assert event_id == "event:evt-1"
    hashed = stable_task_id("generic task without ids")
    assert hashed.startswith("hash:")


def test_completion_uses_external_frozen_snapshot_not_added_lines():
    text = (FIXTURES / "daily-frozen.md").read_text(encoding="utf-8")
    note = parse_daily_markdown(text, day=date(2026, 9, 14))
    snapshot_ids = ["MAT-193", "MAT-216"]
    evening = EveningSubmission(
        day=date(2026, 9, 14),
        validated_complete_ids=frozenset({"MAT-193"}),
    )
    rate = compute_completion(note, evening, frozen_snapshot_ids=snapshot_ids)
    assert rate == 0.5
    assert len(frozen_denominator_ids(note, snapshot_ids)) == 2


def test_unclosed_day_without_evening_excludes_completion():
    text = (FIXTURES / "daily-frozen.md").read_text(encoding="utf-8")
    note = parse_daily_markdown(text.replace("swarm_evening_validated: true", ""), day=date(2026, 9, 14))
    note.evening_absent = True
    assert compute_completion(note, None, frozen_snapshot_ids=["MAT-193", "MAT-216"]) is None


def test_scope_penalty_only_when_p0_open_and_procrastination():
    evening = EveningSubmission(
        day=date(2026, 9, 14),
        validated_complete_ids=frozenset(),
        unplanned_completed=(
            UnplannedCompletion("hash:abc", "procrastinação", date(2026, 9, 14)),
            UnplannedCompletion("hash:def", "oportunidade", date(2026, 9, 14)),
            UnplannedCompletion("hash:ghi", None, date(2026, 9, 14)),
        ),
    )
    assert compute_scope_penalty(evening, frozenset({"MAT-193"})) == 1
    assert compute_scope_penalty(evening, frozenset()) == 0


def test_morning_freeze_is_idempotent():
    state = ReportsState()
    items = [
        FrozenItem("MAT-193", "simulado", date(2026, 9, 14), is_p0=True),
        FrozenItem("MAT-216", "voucher", date(2026, 9, 14)),
    ]
    assert apply_morning_freeze(state, date(2026, 9, 14), items, commit="abc123") is True
    assert apply_morning_freeze(state, date(2026, 9, 14), items, commit="def456") is False
    assert state.get_day(date(2026, 9, 14)).frozen_at_commit == "abc123"
