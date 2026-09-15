import builtins
import json

import pytest

from app.cli import main


def test_report_missing_optional_package(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "swarm_reports.cli" or (
            fromlist and "cli" in fromlist and name == "swarm_reports"
        ):
            raise ImportError("no reports package")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    code = main(["report", "morning"])
    assert code == 2


def test_report_does_not_require_store(monkeypatch):
    """`report` must never demand the broker SQLite path; it is a different subsystem."""
    seen = {}

    def fake_reports_main(argv):
        seen["argv"] = argv
        return 0

    import app.cli as cli

    monkeypatch.setattr(cli, "_delegate_report", fake_reports_main)
    assert main(["report", "morning", "--config", "/abs/reports.json"]) == 0
    assert seen["argv"] == ["report", "morning", "--config", "/abs/reports.json"]


def test_report_forwards_config_after_subcommand(tmp_path):
    """The documented `mathai-swarm report morning --config X` ordering must parse.

    The broker parser used to declare `report morning` itself, so any flag it did not
    know about — including `--config` — died as "unrecognized arguments".
    """
    swarm_reports = pytest.importorskip("swarm_reports.cli")
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    (wiki / "daily" / "2026-09-14.md").write_text("# 2026-09-14\n\n## Hoje\n\n", encoding="utf-8")
    weights = tmp_path / "res-weights.json"
    weights.write_text(
        json.dumps({"version": 1, "platforms": {"linkedin": {"signals": {"comments": 4}}}}),
        encoding="utf-8",
    )
    config = tmp_path / "reports.json"
    config.write_text(
        json.dumps(
            {
                "wiki_dir": str(wiki),
                "state_dir": str(tmp_path / "state"),
                "weights_path": str(weights),
                "output_dir": str(tmp_path / "out"),
                "owner_id": "owner",
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"day": "2026-09-14", "checklist": [{"task_id": "t1", "text": "x"}]}),
        encoding="utf-8",
    )
    assert swarm_reports is not None
    code = main(
        [
            "report", "morning",
            "--config", str(config),
            "--date", "2026-09-14",
            "--plan", str(plan),
            "--replay",
        ]
    )
    assert code == 0
    assert (tmp_path / "out" / "2026-09-14.html").is_file()
