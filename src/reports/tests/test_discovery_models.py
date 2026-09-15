from __future__ import annotations

import json

from swarm_reports.discovery.models import DiscoveryBatch, DiscoveryError, DiscoveryItem


def test_item_round_trips_through_json():
    item = DiscoveryItem(
        kind="commit",
        id="github:o/r:commit:abc",
        source="github:o/r",
        url="https://example.invalid/c/abc",
        title="fix: thing",
        observed_at="2026-09-14T10:00:00Z",
    )
    restored = DiscoveryItem.from_json(json.loads(json.dumps(item.to_json())))
    assert restored == item
    assert restored.classification is None  # unclassified: info only, no penalty


def test_batch_is_json_serializable():
    batch = DiscoveryBatch(
        items=[
            DiscoveryItem(
                kind="wiki_log_entry",
                id="wiki_log:aaa",
                source="wiki_log",
                url=None,
                title="ingest x",
                observed_at="2026-09-14",
            )
        ],
        errors=[DiscoveryError(source="linear", message="not configured", at="2026-09-14T00:00:00Z")],
        checkpoints={"wiki_log": {"cursor": "10", "seen_ids": ["wiki_log:aaa"]}},
    )
    payload = json.dumps(batch.to_json())
    parsed = json.loads(payload)
    assert parsed["items"][0]["id"] == "wiki_log:aaa"
    assert parsed["errors"][0]["source"] == "linear"
    assert parsed["checkpoints"]["wiki_log"]["cursor"] == "10"
