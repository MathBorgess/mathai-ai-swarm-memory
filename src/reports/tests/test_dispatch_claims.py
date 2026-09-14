from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from swarm_reports.dispatch.claims import (
    ClaimStore,
    DuplicateLaunch,
    RecoveryRequired,
    StaleCompletion,
)
from swarm_reports.dispatch.statefile import ProbeUnavailable, StateError, state_key

NOW = datetime(2026, 9, 14, 6, 0, 0)


class FakeProbe:
    """Injectable process identity. ``tokens[pid]`` None means 'no such process'."""

    def __init__(self, tokens: dict[int, str | None] | None = None, *, answers: bool = True):
        self.tokens = tokens or {}
        self.answers = answers

    def start_token(self, pid: int) -> str | None:
        if not self.answers:
            raise ProbeUnavailable("probe cannot answer in this environment")
        return self.tokens.get(pid)


def _store(tmp_path: Path, probe=None) -> ClaimStore:
    return ClaimStore(tmp_path, probe=probe or FakeProbe({111: "start-token-a"}))


def _claim(store: ClaimStore, *, pid: int = 111, token: str = "start-token-a", now=NOW, provider="claude"):
    return store.try_claim("2026-09-14", "MAT-159", provider, now=now, pid=pid, process_token=token)


# --- basic lifecycle ---------------------------------------------------------


def test_first_claim_succeeds(tmp_path):
    claim = _claim(_store(tmp_path))
    assert claim.status == "running"
    assert claim.attempt == 1


def test_running_claim_races_second_caller_gets_duplicate(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    with pytest.raises(DuplicateLaunch):
        _claim(store, now=NOW + timedelta(minutes=1))


def test_running_twice_same_morning_run_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    with pytest.raises(DuplicateLaunch):
        _claim(store, now=NOW + timedelta(seconds=1))


def test_done_claim_is_never_resumable(tmp_path):
    store = _store(tmp_path)
    claim = _claim(store)
    store.mark_done("2026-09-14", "MAT-159", attempt=claim.attempt, now=NOW + timedelta(minutes=5))
    with pytest.raises(DuplicateLaunch):
        _claim(store, now=NOW + timedelta(days=1))


def test_failed_claim_is_resumable(tmp_path):
    store = _store(tmp_path)
    claim = _claim(store)
    store.mark_failed("2026-09-14", "MAT-159", "timeout", attempt=claim.attempt, now=NOW + timedelta(minutes=1))
    resumed = _claim(store, now=NOW + timedelta(minutes=2))
    assert resumed.attempt == 2
    assert resumed.status == "running"


def test_different_dates_are_independent_claims(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    claim = store.try_claim("2026-09-15", "MAT-159", "claude", now=NOW + timedelta(days=1), pid=111, process_token="t")
    assert claim.attempt == 1


def test_load_missing_claim_returns_none(tmp_path):
    assert _store(tmp_path).load("2026-09-14", "no-such-task") is None


def test_list_claims_returns_the_days_claims_for_the_ledger(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    store.try_claim("2026-09-14", "MAT-160", "codex", now=NOW, pid=111, process_token="start-token-a")
    assert {c.task_id for c in store.list_claims("2026-09-14")} == {"MAT-159", "MAT-160"}


# --- finding 3a: elapsed time alone must never authorize a second launch -----


def test_long_running_job_past_two_hours_is_not_relaunched(tmp_path):
    """A >2h job is slow, not dead: its process is still alive, so no relaunch."""
    store = _store(tmp_path, FakeProbe({111: "start-token-a"}))
    _claim(store)
    with pytest.raises(DuplicateLaunch) as exc:
        _claim(store, now=NOW + timedelta(hours=9))
    assert "elapsed time does not authorize a relaunch" in str(exc.value)


def test_dead_process_is_resumable_with_incremented_attempt(tmp_path):
    """`ps` says the pid is gone -> positive evidence the attempt died."""
    probe = FakeProbe({111: "start-token-a"})
    store = _store(tmp_path, probe)
    _claim(store)
    probe.tokens[111] = None  # process exited
    resumed = store.try_claim(
        "2026-09-14", "MAT-159", "codex", now=NOW + timedelta(minutes=5), pid=222, process_token="tok-b"
    )
    assert resumed.attempt == 2
    assert resumed.provider == "codex"


def test_recycled_pid_with_different_start_token_counts_as_dead(tmp_path):
    probe = FakeProbe({111: "start-token-a"})
    store = _store(tmp_path, probe)
    _claim(store)
    probe.tokens[111] = "a-totally-different-process"  # pid reused by the OS
    resumed = _claim(store, pid=333, token="tok-c", now=NOW + timedelta(minutes=5))
    assert resumed.attempt == 2


def test_unverifiable_process_requires_explicit_operator_takeover(tmp_path):
    """Probe cannot answer -> never guess. Fails closed until a human says so."""
    store = _store(tmp_path, FakeProbe(answers=False))
    _claim(store)
    with pytest.raises(RecoveryRequired):
        _claim(store, now=NOW + timedelta(hours=5))
    resumed = store.try_claim(
        "2026-09-14", "MAT-159", "claude", now=NOW + timedelta(hours=5), pid=111, process_token="x", allow_takeover=True
    )
    assert resumed.attempt == 2


def test_recovery_required_is_caught_by_duplicate_launch_handlers(tmp_path):
    store = _store(tmp_path, FakeProbe(answers=False))
    _claim(store)
    with pytest.raises(DuplicateLaunch):  # subclass: a duplicate guard still fails closed
        _claim(store, now=NOW + timedelta(hours=5))


def test_claim_without_recorded_pid_also_requires_takeover(tmp_path):
    store = _store(tmp_path)
    store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW)  # no pid recorded
    with pytest.raises(RecoveryRequired):
        store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(hours=3))


# --- finding 3b: completions are bound to their attempt ---------------------


def test_stale_completion_cannot_overwrite_a_newer_attempt(tmp_path):
    probe = FakeProbe({111: "start-token-a"})
    store = _store(tmp_path, probe)
    first = _claim(store)
    probe.tokens[111] = None
    second = _claim(store, pid=222, token="tok-b", now=NOW + timedelta(minutes=5))
    assert second.attempt == 2

    with pytest.raises(StaleCompletion):  # attempt 1's straggler reports success
        store.mark_done("2026-09-14", "MAT-159", attempt=first.attempt, now=NOW + timedelta(minutes=6))
    assert store.load("2026-09-14", "MAT-159").status == "running"

    store.mark_done("2026-09-14", "MAT-159", attempt=second.attempt, now=NOW + timedelta(minutes=7))
    assert store.load("2026-09-14", "MAT-159").status == "done"


def test_failure_cannot_overwrite_an_already_done_attempt(tmp_path):
    store = _store(tmp_path)
    claim = _claim(store)
    store.mark_done("2026-09-14", "MAT-159", attempt=claim.attempt, now=NOW + timedelta(minutes=1))
    with pytest.raises(StaleCompletion):
        store.mark_failed("2026-09-14", "MAT-159", "late error", attempt=claim.attempt, now=NOW + timedelta(minutes=2))


def test_completion_for_a_missing_claim_raises(tmp_path):
    with pytest.raises(LookupError):
        _store(tmp_path).mark_done("2026-09-14", "ghost", attempt=1, now=NOW)


# --- finding 3c: key derivation, validation, permissions --------------------


def test_task_ids_that_escape_to_the_same_file_when_escaped_stay_distinct(tmp_path):
    """`a/b` vs `a_b` collide under a replace('/','_') key. Digests do not."""
    store = _store(tmp_path)
    a = store.try_claim("2026-09-14", "a/b", "claude", now=NOW, pid=111, process_token="start-token-a")
    b = store.try_claim("2026-09-14", "a_b", "claude", now=NOW, pid=111, process_token="start-token-a")
    assert a.attempt == 1 and b.attempt == 1
    assert store.load("2026-09-14", "a/b").task_id == "a/b"
    assert store.load("2026-09-14", "a_b").task_id == "a_b"
    assert state_key("2026-09-14", "a/b") != state_key("2026-09-14", "a_b")


def test_task_id_cannot_traverse_out_of_the_state_dir(tmp_path):
    store = _store(tmp_path)
    claim = store.try_claim("2026-09-14", "../../etc/passwd", "claude", now=NOW, pid=111, process_token="t")
    written = list((tmp_path / "dispatch-claims").glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path / "dispatch-claims"
    assert claim.task_id == "../../etc/passwd"  # recorded verbatim, not used as a path


@pytest.mark.parametrize("bad_date", ["2026-9-14", "../2026-09-14", "2026-13-01", "", "2026-09-14/../x"])
def test_invalid_dates_are_rejected(tmp_path, bad_date):
    with pytest.raises(ValueError):
        _store(tmp_path).try_claim(bad_date, "MAT-159", "claude", now=NOW)


@pytest.mark.parametrize("bad_task", ["", "   ", "x" * 201, "with\nnewline"])
def test_invalid_task_ids_are_rejected(tmp_path, bad_task):
    with pytest.raises(ValueError):
        _store(tmp_path).try_claim("2026-09-14", bad_task, "claude", now=NOW)


def test_claim_file_is_written_0600(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    mode = stat.S_IMODE(store._path("2026-09-14", "MAT-159").stat().st_mode)
    assert mode == 0o600


def test_claim_file_is_replaced_atomically_never_partially_visible(tmp_path):
    store = _store(tmp_path)
    _claim(store)
    path = store._path("2026-09-14", "MAT-159")
    before = path.stat().st_ino
    store.mark_failed("2026-09-14", "MAT-159", "boom", attempt=1, now=NOW + timedelta(minutes=1))
    assert path.stat().st_ino != before  # os.replace swapped a fully-written file in
    assert json.loads(path.read_text())["status"] == "failed"
    assert not list(path.parent.glob(".tmp-*"))  # no temp litter left behind


# --- finding 3d: state on disk is untrusted ---------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 99, "date": "2026-09-14", "task_id": "t", "provider": "p", "status": "running", "attempt": 1, "updated_at": "2026-09-14T06:00:00"},
        {"version": 1, "date": "2026-09-14", "task_id": "t", "provider": "p", "status": "zombie", "attempt": 1, "updated_at": "2026-09-14T06:00:00"},
        {"version": 1, "date": "2026-09-14", "task_id": "t", "provider": "p", "status": "running", "attempt": "1", "updated_at": "2026-09-14T06:00:00"},
        {"version": 1, "date": "2026-09-14", "task_id": "t", "provider": "p", "status": "running", "attempt": 0, "updated_at": "2026-09-14T06:00:00"},
        {"version": 1, "date": "2026-09-14", "task_id": "t", "provider": "p", "status": "running", "attempt": 1, "updated_at": "2026-09-14T06:00:00", "pid": -5},
        {"version": 1, "date": "not-a-date", "task_id": "t", "provider": "p", "status": "running", "attempt": 1, "updated_at": "2026-09-14T06:00:00"},
        {"version": 1, "task_id": "t", "provider": "p", "status": "running", "attempt": 1, "updated_at": "2026-09-14T06:00:00"},
    ],
)
def test_malformed_state_on_disk_is_rejected_not_silently_accepted(tmp_path, payload):
    store = _store(tmp_path)
    path = store._path("2026-09-14", "MAT-159")
    path.write_text(json.dumps(payload))
    with pytest.raises((StateError, ValueError)):
        store.load("2026-09-14", "MAT-159")


def test_corrupt_json_is_reported_not_treated_as_no_claim(tmp_path):
    store = _store(tmp_path)
    store._path("2026-09-14", "MAT-159").write_text("{not json")
    with pytest.raises(StateError):
        store.load("2026-09-14", "MAT-159")


def test_unknown_extra_field_on_disk_does_not_crash_the_loader(tmp_path):
    """Forward compatibility: a newer writer's extra key must not blow up a read."""
    store = _store(tmp_path)
    claim = _claim(store)
    path = store._path("2026-09-14", "MAT-159")
    payload = json.loads(path.read_text())
    payload["something_new"] = {"added": "later"}
    path.write_text(json.dumps(payload))
    assert store.load("2026-09-14", "MAT-159").attempt == claim.attempt


# --- finding 3e: the lock must survive a crash ------------------------------


def test_lock_is_released_when_the_holder_crashes(tmp_path):
    """A killed dispatcher must not wedge the key forever.

    Runs a real child process that takes the claim lock and is then killed
    mid-hold; the parent must still be able to claim afterwards.
    """
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from swarm_reports.dispatch.statefile import file_lock
        from swarm_reports.dispatch.claims import ClaimStore
        store = ClaimStore(Path({str(tmp_path)!r}))
        with file_lock(store._lock_path("2026-09-14", "MAT-159")):
            print("locked", flush=True)
            time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
    # the kernel dropped the flock with the process; the key is usable again
    claim = _claim(_store(tmp_path))
    assert claim.attempt == 1


def test_a_live_process_from_another_os_process_blocks_a_second_launch(tmp_path):
    """End-to-end with the real `ps` probe: a genuinely running owner refuses a relaunch.

    Deterministic by construction: the child claims and then stays alive, so
    the parent's attempt is evaluated against a process that really exists.
    After the child is killed, the same attempt is allowed — the difference is
    evidence about the process, never elapsed time.
    """
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from datetime import datetime
        from pathlib import Path
        from swarm_reports.dispatch.claims import ClaimStore
        from swarm_reports.dispatch.statefile import current_process_token
        store = ClaimStore(Path({str(tmp_path)!r}))
        pid, token = current_process_token()
        store.try_claim("2026-09-14", "MAT-159", "claude",
                        now=datetime(2026, 9, 14, 6, 0), pid=pid, process_token=token)
        print("claimed", flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "claimed"
        store = ClaimStore(tmp_path)  # real SystemProcessProbe
        with pytest.raises(DuplicateLaunch):
            store.try_claim("2026-09-14", "MAT-159", "claude", now=NOW + timedelta(hours=8), pid=os.getpid())
    finally:
        child.kill()
        child.wait(timeout=10)

    resumed = ClaimStore(tmp_path).try_claim(
        "2026-09-14", "MAT-159", "claude", now=NOW + timedelta(minutes=1), pid=os.getpid(), process_token="mine"
    )
    assert resumed.attempt == 2
