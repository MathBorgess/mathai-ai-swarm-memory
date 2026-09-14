"""The CLI seams F3 adds: `report serve --drain-outbox` and `report evening --input`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_reports.cli import main
from swarm_reports.server.outbox import Outbox
from swarm_reports.server.revisions import RevisionStore
from tests.conftest import DAY, WEIGHTS, freeze_day, payload_dict, write_payload


def write_config(tmp_path: Path, **server) -> Path:
    state = tmp_path / "state"
    output = tmp_path / "out"
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    freeze_day(state)
    block = {
        "bind_host": "127.0.0.1",
        "port": 8788,
        "auth_mode": "synthetic-local",
    }
    block.update(server)
    path = tmp_path / "reports.json"
    path.write_text(
        json.dumps(
            {
                "wiki_dir": str(wiki),
                "state_dir": str(state),
                "weights_path": str(WEIGHTS),
                "output_dir": str(output),
                "owner_id": "owner",
                "timezone": "America/Recife",
                "public_origin": "http://127.0.0.1:8788",
                "server": block,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_evening_input_saves_a_revision(tmp_path, capsys):
    config = write_config(tmp_path)
    payload = write_payload(tmp_path / "evening.json")

    assert main(["report", "evening", "--config", str(config), "--input", str(payload)]) == 0
    out = capsys.readouterr()
    assert "revision 1 (new)" in out.out

    assert main(["report", "evening", "--config", str(config), "--input", str(payload)]) == 0
    assert "revision 1 (unchanged)" in capsys.readouterr().out
    assert RevisionStore(tmp_path / "state").latest(DAY).revision == 1


def test_evening_input_and_post_agree_byte_for_byte(tmp_path, reports_config):
    """The copy-prompt fallback must produce the same stored content as the form."""
    from tests.test_server_http import post, running

    with running(reports_config) as server:
        post(server, payload_dict(notes="parity"))
    from_http = RevisionStore(reports_config.state_dir).latest(DAY)

    config = write_config(tmp_path)
    payload = write_payload(tmp_path / "evening.json", notes="parity")
    assert main(["report", "evening", "--config", str(config), "--input", str(payload)]) == 0
    from_cli = RevisionStore(tmp_path / "state").latest(DAY)

    assert from_cli.content == from_http.content
    assert from_cli.content_hash == from_http.content_hash


def test_evening_requires_an_absolute_input(tmp_path, capsys):
    config = write_config(tmp_path)
    assert main(["report", "evening", "--config", str(config), "--input", "evening.json"]) == 2
    assert "absolute" in capsys.readouterr().err


def test_evening_without_input_explains_the_f4_gap(tmp_path, capsys):
    config = write_config(tmp_path)
    assert main(["report", "evening", "--config", str(config)]) == 2
    assert "--input" in capsys.readouterr().err


def test_evening_date_must_match_the_payload(tmp_path, capsys):
    config = write_config(tmp_path)
    payload = write_payload(tmp_path / "evening.json")
    code = main(
        [
            "report",
            "evening",
            "--config",
            str(config),
            "--date",
            "2026-09-15",
            "--input",
            str(payload),
        ]
    )
    assert code == 2
    assert "does not match the payload day" in capsys.readouterr().err


def test_evening_rejects_a_malformed_file(tmp_path, capsys):
    config = write_config(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["report", "evening", "--config", str(config), "--input", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err


def test_drain_outbox_reports_pending_without_binding(tmp_path, capsys):
    config = write_config(tmp_path)
    payload = write_payload(tmp_path / "evening.json")
    main(["report", "evening", "--config", str(config), "--input", str(payload)])

    assert main(["report", "serve", "--config", str(config), "--drain-outbox"]) == 0
    out = capsys.readouterr().out
    assert f"pending {DAY.isoformat()} revision 1" in out
    assert "no evening handler configured" in out
    assert Outbox(tmp_path / "state").pending()[0].revision == 1


def test_drain_outbox_runs_the_configured_handler(tmp_path, capsys):
    marker = tmp_path / "handled.json"
    handler = tmp_path / "handler.py"
    handler.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    config = write_config(
        tmp_path, dispatch_command=[__import__("sys").executable, str(handler)]
    )
    payload = write_payload(tmp_path / "evening.json")
    main(["report", "evening", "--config", str(config), "--input", str(payload)])

    assert main(["report", "serve", "--config", str(config), "--drain-outbox"]) == 0
    assert "completed 1 of 1 pending" in capsys.readouterr().out
    assert json.loads(marker.read_text(encoding="utf-8"))["revision"] == 1
    assert Outbox(tmp_path / "state").pending() == []


def test_drain_outbox_is_idempotent(tmp_path, capsys):
    config = write_config(tmp_path)
    payload = write_payload(tmp_path / "evening.json")
    main(["report", "evening", "--config", str(config), "--input", str(payload)])
    main(["report", "serve", "--config", str(config), "--drain-outbox"])
    capsys.readouterr()
    assert main(["report", "serve", "--config", str(config), "--drain-outbox"]) == 0
    assert "1 pending" in capsys.readouterr().out


def test_serve_refuses_a_public_bind_override(tmp_path, capsys):
    config = write_config(tmp_path)
    code = main(["serve", "--config", str(config), "--host", "0.0.0.0", "--drain-outbox"])
    # `--drain-outbox` never binds, so the gate is what must reject the intent instead.
    assert code == 0  # nothing was served; the override is irrelevant without a socket
    capsys.readouterr()
    assert main(["serve", "--config", str(config), "--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["serve"], ["report", "serve"]])
def test_serve_is_reachable_under_both_spellings(tmp_path, argv, capsys):
    config = write_config(tmp_path)
    assert main([*argv, "--config", str(config), "--drain-outbox"]) == 0
    assert "0 pending" in capsys.readouterr().out
