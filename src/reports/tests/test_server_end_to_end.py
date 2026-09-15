"""One pass through the whole day: morning renders, the served page posts, evening saves.

This is the test that would have caught a page whose `fetch` targets a route the server
does not have, which no amount of unit testing on either side would notice.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date

from swarm_reports.cli import main
from swarm_reports.server.revisions import RevisionStore
from tests.conftest import WEIGHTS
from tests.test_morning_freeze import init_wiki
from tests.test_server_http import post, request, running

DAY = date(2026, 9, 14)
TASKS = ("MAT-1", "MAT-2")


def build_morning(tmp_path):
    wiki = init_wiki(tmp_path)
    config_path = tmp_path / "reports.json"
    config_path.write_text(
        json.dumps(
            {
                "wiki_dir": str(wiki),
                "state_dir": str(tmp_path / "state"),
                "weights_path": str(WEIGHTS),
                "output_dir": str(tmp_path / "out"),
                "owner_id": "owner",
                "public_origin": "http://127.0.0.1:8788",
                "server": {
                    "bind_host": "127.0.0.1",
                    "port": 8788,
                    "auth_mode": "synthetic-local",
                    "request_timeout_seconds": 5.0,
                },
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "checklist": [
                    {"task_id": task, "text": f"item {task}", "is_p0": index == 0}
                    for index, task in enumerate(TASKS)
                ],
                "sources": [{"kind": "linear", "pointer": "", "status": "unavailable"}],
            }
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "report",
            "morning",
            "--config",
            str(config_path),
            "--date",
            DAY.isoformat(),
            "--plan",
            str(plan_path),
            "--wiki-local-only",
        ]
    )
    assert code == 0
    from swarm_reports.config import load_config

    config = load_config(config_path)
    # An ephemeral port keeps parallel runs from fighting over the configured one.
    return replace(config, server=replace(config.server, port=0))


def fetch_targets(html: str) -> set[str]:
    """Every URL the page will call, read out of the page itself."""
    return set(re.findall(r"fetch\(\s*[\"']([^\"']+)", html)) | set(
        re.findall(r"POST_URL\s*=\s*[\"']([^\"']+)", html)
    ) | set(re.findall(r"REVISION_URL\s*=\s*[\"']([^\"']+)", html))


def test_the_served_page_posts_to_a_route_the_server_has(tmp_path):
    config = build_morning(tmp_path)
    with running(config) as server:
        _status, _headers, body = request(server, "GET", f"/{DAY.isoformat()}.html")
        html = body.decode("utf-8")
        targets = fetch_targets(html)
        assert targets, "the page declares no endpoint at all"
        for target in targets:
            method = "POST" if target == config.evening_post_url else "GET"
            if method == "GET":
                status, _h, _b = request(server, "GET", target)
                assert status == 200, target
            else:
                status, _h, _b = post(
                    server,
                    {
                        "schema_version": 1,
                        "day": DAY.isoformat(),
                        "owner_id": "owner",
                        "checklist": [
                            {"task_id": task, "done": index == 0, "classification": None}
                            for index, task in enumerate(TASKS)
                        ],
                        "notes": "e2e",
                    },
                )
                assert status == 200, target
    stored = RevisionStore(config.state_dir).latest(DAY)
    assert stored.revision == 1
    assert stored.content["notes"] == "e2e"


def test_the_page_restores_the_saved_revision(tmp_path):
    config = build_morning(tmp_path)
    with running(config) as server:
        post(
            server,
            {
                "schema_version": 1,
                "day": DAY.isoformat(),
                "owner_id": "owner",
                "checklist": [
                    {"task_id": task, "done": True, "classification": None} for task in TASKS
                ],
                "notes": "restored",
            },
        )
        status, _h, body = request(server, "GET", config.evening_revision_url(DAY))
    assert status == 200
    parsed = json.loads(body)
    assert parsed["revision"] == 1
    assert parsed["payload"]["notes"] == "restored"
    assert [item["task_id"] for item in parsed["payload"]["checklist"]] == list(TASKS)


def test_a_day_with_no_report_is_not_served(tmp_path):
    config = build_morning(tmp_path)
    with running(config) as server:
        status, _h, _b = request(server, "GET", "/2026-09-15.html")
    assert status == 404


def test_without_a_public_origin_the_page_offers_only_copy_prompt(tmp_path):
    config = build_morning(tmp_path)
    offline = replace(config, public_origin=None)
    assert offline.evening_post_url is None
    assert offline.evening_revision_url(DAY) is None
    html = (config.output_dir / f"{DAY.isoformat()}.html").read_text(encoding="utf-8")
    assert "copy-prompt" in html
