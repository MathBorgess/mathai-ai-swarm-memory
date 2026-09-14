from __future__ import annotations

from pathlib import Path

from swarm_reports.discovery.checkpoints import (
    CheckpointState,
    SourceCheckpoint,
    checkpoint_lock,
    default_checkpoint_path,
    load_checkpoints,
    save_checkpoints,
)


def test_load_missing_state_returns_empty(tmp_path: Path):
    state = load_checkpoints(default_checkpoint_path(tmp_path))
    assert state.sources == {}


def test_save_then_load_round_trips(tmp_path: Path):
    path = default_checkpoint_path(tmp_path)
    state = CheckpointState()
    state.sources["github"] = SourceCheckpoint(cursor="2026-09-14T00:00:00Z", seen_ids=["a", "b"])
    save_checkpoints(path, state)

    restored = load_checkpoints(path)
    assert restored.sources["github"].cursor == "2026-09-14T00:00:00Z"
    assert restored.sources["github"].seen_ids == ["a", "b"]
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_seen_ids_ring_buffer_is_bounded():
    cp = SourceCheckpoint(cursor=None, seen_ids=[])
    updated = cp.with_update(cursor="c1", new_ids=[f"id-{i}" for i in range(600)])
    assert len(updated.seen_ids) == 500
    assert updated.seen_ids[-1] == "id-599"


def test_checkpoint_lock_is_reentrant_across_sequential_uses(tmp_path: Path):
    with checkpoint_lock(tmp_path):
        pass
    with checkpoint_lock(tmp_path):
        pass  # second acquisition after release must not deadlock
