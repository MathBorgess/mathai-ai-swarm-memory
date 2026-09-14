import json
import multiprocessing
from datetime import date
from pathlib import Path

import pytest

from swarm_reports.metrics.state import ReportsState, apply_morning_freeze, default_state_path, save_state
from swarm_reports.metrics.state import FrozenItem
from swarm_reports.storage import StateTransaction


def _worker(state_dir: str, day: str) -> None:
    txn = StateTransaction(Path(state_dir))
    with txn.locked() as state:
        apply_morning_freeze(
            state,
            date.fromisoformat(day),
            [FrozenItem(task_id="a", text="t", first_planned=date.fromisoformat(day))],
            commit="c",
            snapshot="s",
        )


def test_concurrent_freeze_races(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    day = "2026-09-14"
    procs = [multiprocessing.Process(target=_worker, args=(str(state_dir), day)) for _ in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=10)
        assert proc.exitcode == 0
    state = json.loads(default_state_path(state_dir).read_text(encoding="utf-8"))
    bucket = state["days"][day]
    assert bucket["morning_freeze_applied"] is True
    assert len(bucket["frozen_checklist"]) == 1


def test_corrupt_state_fail_closed(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = default_state_path(state_dir)
    path.write_text("{not json", encoding="utf-8")
    txn = StateTransaction(state_dir)
    with pytest.raises(json.JSONDecodeError):
        with txn.locked():
            pass
