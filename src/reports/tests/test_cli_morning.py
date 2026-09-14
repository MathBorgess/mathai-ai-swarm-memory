import json
import subprocess
from datetime import date
from pathlib import Path

from swarm_reports.cli import main as reports_cli_main


def _wiki_layout(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    (wiki / "daily" / "2026-09-14.md").write_text("# 2026-09-14\n\n## Hoje\n\n", encoding="utf-8")
    return wiki


def test_cli_morning_with_plan(tmp_path):
    wiki = _wiki_layout(tmp_path)
    weights = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"
    config = {
        "wiki_dir": str(wiki),
        "state_dir": str(tmp_path / "state"),
        "weights_path": str(weights),
        "output_dir": str(tmp_path / "out"),
        "owner_id": "owner",
    }
    config_path = tmp_path / "reports.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    plan = {
        "day": "2026-09-14",
        "p0_items": [],
        "handoffs": [],
        "sources": [{"kind": "linear", "pointer": "", "status": "unavailable"}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    code = reports_cli_main(
        [
            "--config",
            str(config_path),
            "report",
            "morning",
            "--date",
            "2026-09-14",
            "--plan",
            str(plan_path),
            "--replay",
        ]
    )
    assert code == 0
    assert (tmp_path / "out" / "2026-09-14.html").is_file()
