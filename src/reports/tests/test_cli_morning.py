import json
from pathlib import Path

import pytest

from swarm_reports.cli import main as reports_cli_main

DAY = "2026-09-14"


def _wiki_layout(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    (wiki / "daily" / f"{DAY}.md").write_text(f"# {DAY}\n\n## Hoje\n\n", encoding="utf-8")
    return wiki


def write_config(tmp_path: Path, **extra) -> Path:
    wiki = _wiki_layout(tmp_path)
    weights = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"
    config = {
        "wiki_dir": str(wiki),
        "state_dir": str(tmp_path / "state"),
        "weights_path": str(weights),
        "output_dir": str(tmp_path / "out"),
        "owner_id": "owner",
        **extra,
    }
    path = tmp_path / "reports.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def write_plan(tmp_path: Path) -> Path:
    plan = {
        "day": DAY,
        "checklist": [{"task_id": "MAT-1", "text": "item", "is_p0": True}],
        "sources": [{"kind": "linear", "pointer": "", "status": "unavailable"}],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_cli_morning_with_plan(tmp_path):
    config_path = write_config(tmp_path)
    plan_path = write_plan(tmp_path)
    code = reports_cli_main(
        [
            "--config", str(config_path),
            "report", "morning",
            "--date", DAY,
            "--plan", str(plan_path),
            "--replay",
        ]
    )
    assert code == 0
    assert (tmp_path / "out" / f"{DAY}.html").is_file()


def test_cli_accepts_config_after_the_subcommand(tmp_path):
    """`mathai-swarm report morning --config ...` is the documented invocation."""
    config_path = write_config(tmp_path)
    plan_path = write_plan(tmp_path)
    code = reports_cli_main(
        [
            "report", "morning",
            "--config", str(config_path),
            "--date", DAY,
            "--plan", str(plan_path),
            "--replay",
        ]
    )
    assert code == 0
    assert (tmp_path / "out" / f"{DAY}.html").is_file()


def test_cli_rejects_relative_plan(tmp_path):
    config_path = write_config(tmp_path)
    code = reports_cli_main(
        ["report", "morning", "--config", str(config_path), "--plan", "plan.json"]
    )
    assert code == 2


@pytest.mark.parametrize("argv", [["report", "evening"], ["report", "evening", "--date", DAY]])
def test_cli_evening_requires_input(tmp_path, argv):
    config_path = write_config(tmp_path)
    assert reports_cli_main([*argv, "--config", str(config_path)]) == 2
