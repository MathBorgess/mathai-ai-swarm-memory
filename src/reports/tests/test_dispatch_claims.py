from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from swarm_reports.dispatch.claims import ClaimStore, DuplicateLaunch

NOW = datetime(2026, 9, 14, 6, 0, 0)


def test_first_claim_succeeds(tmp_path):
    store = ClaimStore(tmp_path)
    claim = store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    assert claim.status == "running"
    assert claim.attempt == 1


def test_running_claim_races_second_caller_gets_duplicate(tmp_path):
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    with pytest.raises(DuplicateLaunch):
        store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(minutes=1))


def test_running_twice_same_morning_run_is_idempotent(tmp_path):
    """Running the morning job again must not re-dispatch a claim already running."""
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    with pytest.raises(DuplicateLaunch):
        store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(seconds=1))


def test_done_claim_is_never_resumable(tmp_path):
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    store.mark_done("2026-09-14", "MAT-159", now=NOW + timedelta(minutes=5))
    with pytest.raises(DuplicateLaunch):
        store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(days=1))


def test_stale_running_claim_beyond_timeout_is_resumable_with_incremented_attempt(tmp_path):
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    later = NOW + timedelta(hours=3)  # beyond DEFAULT_RUNNING_TIMEOUT of 2h
    resumed = store.try_claim("2026-09-14", "MAT-159", "codex", now=later)
    assert resumed.attempt == 2
    assert resumed.provider == "codex"


def test_failed_claim_is_resumable(tmp_path):
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    store.mark_failed("2026-09-14", "MAT-159", "timeout", now=NOW + timedelta(minutes=1))
    resumed = store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(minutes=2))
    assert resumed.attempt == 2
    assert resumed.status == "running"


def test_different_dates_are_independent_claims(tmp_path):
    store = ClaimStore(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)
    claim = store.try_claim("2026-09-15", "MAT-159", "claude", now=NOW + timedelta(days=1))
    assert claim.attempt == 1


def test_load_missing_claim_returns_none(tmp_path):
    store = ClaimStore(tmp_path)
    assert store.load("2026-09-14", "no-such-task") is None
