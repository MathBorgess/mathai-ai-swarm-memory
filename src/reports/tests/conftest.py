"""Shared fixtures for the F3 server tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from swarm_reports.config import ReportsConfig
from swarm_reports.metrics.state import FrozenItem, apply_morning_freeze
from swarm_reports.server.config import (
    AUTH_SYNTHETIC_LOCAL,
    ServerConfig,
)
from swarm_reports.storage import StateTransaction

DAY = date(2026, 9, 14)
FROZEN_IDS = ("MAT-1", "MAT-2")
WEIGHTS = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"


def freeze_day(state_dir: Path, day: date = DAY, ids=FROZEN_IDS) -> None:
    with StateTransaction(state_dir).locked() as state:
        apply_morning_freeze(
            state,
            day,
            [
                FrozenItem(task_id=task_id, text=task_id, first_planned=day, is_p0=index == 0)
                for index, task_id in enumerate(ids)
            ],
            commit="deadbeef",
            snapshot=f"{day.isoformat()}T08:00:00-03:00",
        )


@pytest.fixture
def server_config() -> ServerConfig:
    return ServerConfig(
        bind_host="127.0.0.1",
        port=0,
        auth_mode=AUTH_SYNTHETIC_LOCAL,
        max_body_bytes=64 * 1024,
        request_timeout_seconds=5.0,
    )


@pytest.fixture
def reports_config(tmp_path, server_config) -> ReportsConfig:
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    config = ReportsConfig(
        wiki_dir=wiki,
        state_dir=tmp_path / "state",
        weights_path=WEIGHTS,
        output_dir=output,
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=None,
        public_origin="http://127.0.0.1:9",
        server=server_config,
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    freeze_day(config.state_dir)
    (output / f"{DAY.isoformat()}.html").write_text(
        "<!DOCTYPE html><html><body>report</body></html>", encoding="utf-8"
    )
    return config


def payload_dict(**extra) -> dict:
    body = {
        "schema_version": 1,
        "day": DAY.isoformat(),
        "owner_id": "owner",
        "checklist": [
            {"task_id": "MAT-1", "done": True, "classification": None},
            {"task_id": "MAT-2", "done": False, "classification": None},
        ],
        "notes": "",
    }
    body.update(extra)
    return body


def write_payload(path: Path, **extra) -> Path:
    path.write_text(json.dumps(payload_dict(**extra)), encoding="utf-8")
    return path
