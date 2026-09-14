import pytest

from swarm_reports.evening_schema import EveningPayload, parse_evening_payload, SCHEMA_VERSION


def test_evening_payload_roundtrip():
    raw = {
        "schema_version": SCHEMA_VERSION,
        "day": "2026-09-14",
        "owner_id": "owner",
        "checklist": [{"task_id": "t1", "done": False}],
        "notes": "ok",
    }
    payload = parse_evening_payload(raw)
    assert payload.day.isoformat() == "2026-09-14"
    assert payload.to_canonical_json()


def test_evening_payload_rejects_bad_version():
    with pytest.raises(ValueError):
        parse_evening_payload({"schema_version": 99, "day": "2026-09-14", "owner_id": "x", "checklist": []})
