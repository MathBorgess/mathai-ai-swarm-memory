from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from swarm_reports.dispatch.routing import ProviderState, classify, eligible, route


def test_classify_buckets():
    assert classify("claude", 80.0).remaining_pct == 80.0
    assert classify("claude", 5.0).remaining_pct == 5.0
    assert classify("claude", 0.0).remaining_pct == 0.0
    assert classify("claude", None).remaining_pct is None


def test_classify_marks_stale_reading_as_unknown_not_ok():
    old = datetime(2026, 9, 14, 0, 0)
    now = datetime(2026, 9, 14, 8, 0)  # 8h later, beyond default 6h staleness
    state = classify("claude", 90.0, probed_at=old, now=now)
    assert state.stale is True
    assert state.remaining_pct is None  # treated as unknown, not as "still 90%"


def test_classify_fresh_reading_is_not_stale():
    now = datetime(2026, 9, 14, 8, 0)
    probed = now - timedelta(minutes=30)
    state = classify("claude", 90.0, probed_at=probed, now=now)
    assert state.stale is False
    assert state.remaining_pct == 90.0


def test_eligible_prefers_ok_and_unknown_over_low():
    states = [
        ProviderState("cursor", 10.0),  # low
        ProviderState("claude", 80.0),  # ok
        ProviderState("codex", None),  # unknown
    ]
    result = {s.provider for s in eligible(states)}
    assert result == {"claude", "codex"}


def test_eligible_falls_back_to_richest_low_when_nothing_else_is_left():
    states = [ProviderState("cursor", 15.0), ProviderState("claude", 5.0)]
    result = eligible(states)
    assert [s.provider for s in result] == ["cursor"]


def test_eligible_empty_when_everything_is_empty():
    states = [ProviderState("cursor", 0.0), ProviderState("claude", 0.0)]
    assert eligible(states) == []


def test_weighted_round_robin_worked_example_from_handoff_quota_md():
    # parent cursor; claude 80, codex 70, cursor 10 (low, excluded)
    states = [ProviderState("cursor", 10.0), ProviderState("claude", 80.0), ProviderState("codex", 70.0)]
    tasks = ["t1", "t2", "t3", "t4"]
    assignment = route(tasks, states, parent="cursor")
    assert [assignment[t] for t in tasks] == ["claude", "codex", "claude", "codex"]


def test_two_eligible_providers_never_all_land_on_one_row():
    states = [ProviderState("claude", 50.0), ProviderState("codex", 50.0)]
    tasks = [f"t{i}" for i in range(6)]
    assignment = route(tasks, states, parent="claude")
    used = {assignment[t] for t in tasks}
    assert used == {"claude", "codex"}


def test_user_override_wins_even_if_low():
    states = [ProviderState("cursor", 5.0), ProviderState("claude", 90.0)]
    assignment = route(["t1"], states, parent="claude", overrides={"t1": "cursor"})
    assert assignment["t1"] == "cursor"


def test_route_returns_empty_when_everything_empty():
    states = [ProviderState("cursor", 0.0), ProviderState("claude", 0.0), ProviderState("codex", 0.0)]
    assert route(["t1"], states, parent="claude") == {}


# --- adversarial: inputs a probe or a config can actually produce ------------


def test_a_provider_barely_above_zero_does_not_divide_by_zero():
    """weight = remaining%, and a 0.0000001% reading must not crash the split."""
    states = [ProviderState("claude", 1e-9), ProviderState("codex", 50.0)]
    assignment = route(["t1", "t2"], states, parent="cursor")
    assert set(assignment.values()) <= {"claude", "codex"}


@pytest.mark.parametrize("pct", [-1.0, 101.0, float("nan"), float("inf"), "80", True])
def test_impossible_remaining_pct_is_rejected_at_construction(pct):
    with pytest.raises(ValueError):
        ProviderState("claude", pct)


def test_empty_provider_name_is_rejected():
    with pytest.raises(ValueError):
        ProviderState("  ", 50.0)


def test_override_naming_an_unknown_provider_is_an_error_not_a_silent_launch():
    states = [ProviderState("claude", 90.0)]
    with pytest.raises(ValueError):
        route(["t1"], states, parent="claude", overrides={"t1": "gemini"})


def test_stale_probe_keeps_a_provider_eligible_as_unknown_not_as_full():
    """A 9h-old 90% reading must weigh as 'unknown' (50), not as 90."""
    now = datetime(2026, 9, 14, 12, 0)
    stale = classify("claude", 90.0, probed_at=now - timedelta(hours=9), now=now)
    fresh = classify("codex", 60.0, probed_at=now, now=now)
    assignment = route(["t1", "t2", "t3", "t4", "t5"], [stale, fresh], parent="cursor")
    counts = {p: list(assignment.values()).count(p) for p in ("claude", "codex")}
    assert counts["codex"] > counts["claude"]  # 60 outweighs the unknown-weight 50
