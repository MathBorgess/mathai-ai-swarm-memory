import json
import subprocess
from datetime import date
from pathlib import Path

from swarm_reports.config import ReportsConfig
from swarm_reports.metrics.daily import FROZEN_BEGIN
from swarm_reports.metrics.state import FrozenItem
from swarm_reports.morning import run_morning
from swarm_reports.wiki.freeze import apply_wiki_freeze


def _init_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    subprocess.run(["git", "init"], cwd=wiki, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=wiki, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=wiki, check=True)
    daily = wiki / "daily" / "2026-09-14.md"
    daily.parent.mkdir(parents=True)
    daily.write_text(
        "# 2026-09-14\n\n## Hoje\n\n- [ ] item base\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=wiki, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=wiki, check=True)
    return wiki


def _config(tmp_path: Path, wiki: Path) -> ReportsConfig:
    weights = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"
    return ReportsConfig(
        wiki_dir=wiki,
        state_dir=tmp_path / "state",
        weights_path=weights,
        output_dir=tmp_path / "out",
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=None,
    )


def test_wiki_freeze_idempotent(tmp_path):
    wiki = _init_wiki(tmp_path)
    items = [
        FrozenItem(
            task_id="MAT-1",
            text="task",
            first_planned=date(2026, 9, 14),
            is_p0=True,
        )
    ]
    wt_parent = tmp_path / "wt"
    first = apply_wiki_freeze(
        wiki,
        date(2026, 9, 14),
        items,
        timezone="America/Recife",
        worktree_parent=wt_parent,
    )
    assert first.applied is True
    assert first.commit_sha
    second = apply_wiki_freeze(
        wiki,
        date(2026, 9, 14),
        items,
        timezone="America/Recife",
        worktree_parent=wt_parent,
    )
    assert second.applied is False


def test_morning_twice_same_day(tmp_path):
    wiki = _init_wiki(tmp_path)
    wt_parent = tmp_path / "wt"
    plan = {
        "day": "2026-09-14",
        "p0_items": [
            {
                "task_id": "MAT-1",
                "text": "P0 task",
                "first_planned": "2026-09-14",
                "is_p0": True,
            }
        ],
        "handoffs": [],
        "sources": [],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    config = _config(tmp_path, wiki)
    r1 = run_morning(config, plan_path=plan_path, day=date(2026, 9, 14))
    assert r1.skipped_duplicate is False
    r2 = run_morning(config, plan_path=plan_path, day=date(2026, 9, 14))
    assert r2.skipped_duplicate is True
    wt_daily = config.state_dir / "wiki-worktrees" / "wiki-freeze" / "daily" / "2026-09-14.md"
    assert wt_daily.is_file()
    assert FROZEN_BEGIN.search(wt_daily.read_text(encoding="utf-8"))
