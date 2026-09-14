"""Shared fixtures for the F3 server tests."""

from __future__ import annotations

import json
import subprocess
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


DAILY_TEMPLATE = """---
date: {{date}}
external:
  calendar: []
tags: [daily]
---

# {{date}}

## Hoje

## Evolução

- 

## Amanhã (opcional)

- 
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


@pytest.fixture
def git_wiki(tmp_path) -> Path:
    """A real git vault: the F4 writeback is git work and stubbing it proves nothing."""
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    (wiki / "brand" / "posts").mkdir(parents=True)
    (wiki / "daily" / "_template.md").write_text(DAILY_TEMPLATE, encoding="utf-8")
    git(wiki.parent, "init", "--quiet", "-b", "main", str(wiki))
    git(wiki, "config", "user.email", "tests@example.invalid")
    git(wiki, "config", "user.name", "tests")
    git(wiki, "add", "-A")
    git(wiki, "commit", "--quiet", "-m", "vault")
    return wiki


class FakeGh:
    """Records every argv instead of reaching the network.

    A fake at the process boundary, not at the publisher boundary: the argv list, the
    `ls-remote` head check and the JSON shape `gh pr list` returns are all exercised for
    real, so a wrong flag fails the test rather than shipping.
    """

    def __init__(self, *, head: str | None = None, existing_url: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.head = head
        self.existing_url = existing_url
        self.push_returncode = 0
        self.create_returncode = 0
        self.created_url = "https://github.com/owner/wiki/pull/7"

    def __call__(self, argv, *, cwd, timeout):
        from swarm_reports.wiki.publish import CommandResult

        self.calls.append(list(argv))
        if argv[:2] == ["git", "push"]:
            if self.push_returncode == 0 and self.head is None:
                # A real push makes the remote head the commit we pushed.
                self.head = argv[-1].split(":")[0]
            return CommandResult(self.push_returncode, "", "push rejected")
        if argv[:2] == ["git", "ls-remote"]:
            branch = argv[-1].rsplit("/", 1)[-1]
            return CommandResult(0, f"{self.head}\trefs/heads/{branch}\n" if self.head else "", "")
        if argv[1:3] == ["pr", "list"]:
            rows = [{"url": self.existing_url}] if self.existing_url else []
            return CommandResult(0, json.dumps(rows), "")
        if argv[1:3] == ["pr", "create"]:
            if self.create_returncode != 0:
                return CommandResult(self.create_returncode, "", "no auth token")
            return CommandResult(0, f"{self.created_url}\n", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def f4_config(tmp_path, git_wiki) -> ReportsConfig:
    """A full cycle config: real git vault, local-only remotes, nothing published."""
    from swarm_reports.evening.config import PUBLISH_NONE, EveningConfig, PublishConfig

    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    config = ReportsConfig(
        wiki_dir=git_wiki,
        state_dir=tmp_path / "state",
        weights_path=WEIGHTS,
        output_dir=output,
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=None,
        public_origin="http://127.0.0.1:8787",
        server=None,
        evening=EveningConfig(
            publish=PublishConfig(mode=PUBLISH_NONE),
            wiki_local_only=True,
        ),
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def run_morning_day(config, day: date, checklist: list[dict], tmp_path: Path):
    """Freeze a day the way the owner would: through the real morning run."""
    from swarm_reports.morning import run_morning
    from swarm_reports.wiki.publish import QueuedPublisher

    plan_path = tmp_path / f"plan-{day.isoformat()}.json"
    plan_path.write_text(
        json.dumps({"day": day.isoformat(), "checklist": checklist}), encoding="utf-8"
    )
    return run_morning(
        config,
        day=day,
        plan_path=plan_path,
        local_only_wiki=True,
        publisher=QueuedPublisher(config.state_dir / "wiki-publish-queue"),
    )


def merge_day_branch(wiki: Path, day: date) -> None:
    """Simulate the owner merging the day's vault pull request."""
    git(wiki, "merge", "--quiet", "--no-ff", "-m", f"merge {day}", f"codex/reports-freeze-{day}")


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
