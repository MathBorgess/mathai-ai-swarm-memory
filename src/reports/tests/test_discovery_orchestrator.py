from __future__ import annotations

from pathlib import Path

import pytest

from swarm_reports.discovery.checkpoints import SourceCheckpoint, load_checkpoints, default_checkpoint_path
from swarm_reports.discovery.models import DiscoveryItem
from swarm_reports.discovery.orchestrator import DiscoveryConfig, run_discovery
from swarm_reports.discovery.sources.base import SourceError, SourceResult


class _FakeSource:
    def __init__(self, name: str, *, items=None, error: str | None = None, new_cursor="c1"):
        self.name = name
        self._items = items or []
        self._error = error
        self._new_cursor = new_cursor
        self.calls = 0

    def discover(self, checkpoint: SourceCheckpoint) -> SourceResult:
        self.calls += 1
        if self._error:
            raise SourceError(self._error)
        return SourceResult(items=self._items, new_cursor=self._new_cursor, new_ids=[i.id for i in self._items])


def _item(item_id: str, observed_at: str, source: str = "fake", kind: str = "commit") -> DiscoveryItem:
    return DiscoveryItem(kind=kind, id=item_id, source=source, url=None, title=item_id, observed_at=observed_at)


def test_no_sources_configured_raises():
    with pytest.raises(SourceError):
        run_discovery(DiscoveryConfig(state_dir=Path("/tmp")))


def test_source_failure_does_not_advance_its_checkpoint(tmp_path, monkeypatch):
    import swarm_reports.discovery.orchestrator as orch

    good = _FakeSource("good", items=[_item("good:1", "2026-09-14T00:00:00Z")], new_cursor="2026-09-14T00:00:00Z")
    bad = _FakeSource("bad", error="boom")

    monkeypatch.setattr(orch, "_build_sources", lambda config: {"good": good, "bad": bad})
    config = DiscoveryConfig(state_dir=tmp_path)
    batch = run_discovery(config)

    assert len(batch.errors) == 1
    assert batch.errors[0].source == "bad"
    assert any(item.id == "good:1" for item in batch.items)

    state = load_checkpoints(default_checkpoint_path(tmp_path))
    assert state.sources["good"].cursor == "2026-09-14T00:00:00Z"
    assert "bad" not in state.sources  # untouched: no bucket ever written


def test_known_ledger_action_ids_are_excluded_explicitly(tmp_path, monkeypatch):
    import swarm_reports.discovery.orchestrator as orch

    own_action = _item("github:o/r:pr:1:merged", "2026-09-14T00:00:00Z")
    outside_action = _item("github:o/r:pr:2:merged", "2026-09-14T00:01:00Z")
    src = _FakeSource("github", items=[own_action, outside_action])
    monkeypatch.setattr(orch, "_build_sources", lambda config: {"github": src})

    config = DiscoveryConfig(state_dir=tmp_path, known_ledger_action_ids=frozenset({own_action.id}))
    batch = run_discovery(config)

    ids = {item.id for item in batch.items}
    assert own_action.id not in ids
    assert outside_action.id in ids


def test_unknown_classification_defaults_to_none_info_only(tmp_path, monkeypatch):
    import swarm_reports.discovery.orchestrator as orch

    src = _FakeSource("wiki_log", items=[_item("wiki_log:1", "2026-09-14")])
    monkeypatch.setattr(orch, "_build_sources", lambda config: {"wiki_log": src})
    batch = run_discovery(DiscoveryConfig(state_dir=tmp_path))
    assert batch.items[0].classification is None


def test_deterministic_ordering_with_same_timestamp_ties(tmp_path, monkeypatch):
    import swarm_reports.discovery.orchestrator as orch

    tied = "2026-09-14T00:00:00Z"
    src_a = _FakeSource("a", items=[_item("a:2", tied, source="a"), _item("a:1", tied, source="a")])
    src_b = _FakeSource("b", items=[_item("b:1", tied, source="b")])
    monkeypatch.setattr(orch, "_build_sources", lambda config: {"a": src_a, "b": src_b})

    batch = run_discovery(DiscoveryConfig(state_dir=tmp_path))
    ids_in_order = [item.id for item in batch.items]
    assert ids_in_order == sorted(ids_in_order)  # (observed_at, source, kind, id) tuple sort


def test_source_isolation_one_source_bug_does_not_kill_the_run(tmp_path, monkeypatch):
    import swarm_reports.discovery.orchestrator as orch

    class _Exploding:
        name = "exploding"

        def discover(self, checkpoint):
            raise RuntimeError("unexpected crash, not a SourceError")

    good = _FakeSource("good", items=[_item("good:1", "2026-09-14T00:00:00Z")])
    monkeypatch.setattr(orch, "_build_sources", lambda config: {"good": good, "exploding": _Exploding()})

    batch = run_discovery(DiscoveryConfig(state_dir=tmp_path))
    assert any(e.source == "exploding" for e in batch.errors)
    assert any(item.id == "good:1" for item in batch.items)
