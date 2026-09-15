"""Morning → POST /evening → outbox drain → next morning shows real ledger and metrics."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from swarm_reports.evening.session import make_dispatcher
from swarm_reports.metrics.daily import parse_daily_markdown
from swarm_reports.metrics.posts import parse_post_markdown
from swarm_reports.metrics.state import load_state
from swarm_reports.morning import run_morning
from swarm_reports.server.config import AUTH_SYNTHETIC_LOCAL, ServerConfig
from swarm_reports.storage import StateTransaction
from swarm_reports.wiki.freeze import freeze_worktree_path
from tests.test_evening_session import CHECKLIST, DAY
from swarm_reports.wiki.notes import post_note_filename
from tests.test_server_http import post, running

NEXT = DAY + timedelta(days=1)
GUIDED_URL = "https://linkedin.com/posts/guided-morning"
SPONT_URL = "https://linkedin.com/posts/spontaneous-evening"


def _server_config():
    return ServerConfig(
        bind_host="127.0.0.1",
        port=0,
        auth_mode=AUTH_SYNTHETIC_LOCAL,
        max_body_bytes=256 * 1024,
        request_timeout_seconds=10.0,
    )


def _seed_guided_post(wiki: Path) -> None:
    note = """---
url: https://linkedin.com/posts/guided-morning
platform: linkedin
guided: true
posted_on: 2026-09-14
checkpoints:
  - at: 48h
    due: 2026-09-16
  - at: 7d
    due: 2026-09-21
metrics:
  reach: 1000
  signals:
    reactions: 10
---

# Guided post
"""
    posts = wiki / "brand" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / "guided-morning.md").write_text(note, encoding="utf-8")
    from tests.conftest import git

    git(wiki, "add", "-A")
    git(wiki, "commit", "--quiet", "-m", "guided post seed")


def test_full_day_morning_post_drain_then_next_morning_shows_ledger(f4_config, tmp_path):
    from tests.conftest import run_morning_day

    _seed_guided_post(f4_config.wiki_dir)
    config = replace(
        f4_config,
        public_origin="http://127.0.0.1:8787",
        server=_server_config(),
    )
    run_morning_day(config, DAY, CHECKLIST, tmp_path)

    payload = {
        "schema_version": 1,
        "day": DAY.isoformat(),
        "owner_id": "owner",
        "checklist": [
            {"task_id": "MAT-193", "done": True},
            {"task_id": "MAT-201", "done": False},
            {"task_id": "MAT-202", "done": False},
        ],
        "posts": [
            {
                "url": GUIDED_URL,
                "platform": "linkedin",
                "guided": True,
                "checkpoint": "48h",
                "metrics": {"reach": 1200, "signals": {"reactions": 12}},
            },
            {
                "url": SPONT_URL,
                "platform": "linkedin",
                "guided": False,
                "checkpoint": "launch",
                "metrics": {"reach": 400, "signals": {"reactions": 9}},
            },
        ],
        "notes": "fechamento sintético",
    }
    dispatcher = make_dispatcher(config, publish=False)
    with running(config, dispatcher=dispatcher) as server:
        status, _headers, body = post(server, payload)
        assert status == 200, body.decode("utf-8", errors="replace")
        for _ in range(5):
            if not server.outbox.pending():
                break
            server.worker.drain_once()
        assert not server.outbox.pending(), "evening job must leave the outbox"

    bucket = load_state(StateTransaction(config.state_dir).state_path).days[DAY.isoformat()]
    assert bucket.evening_validated is True
    assert bucket.evening_revision == 1

    daily_path = freeze_worktree_path(config.state_dir / "wiki-worktrees", DAY) / f"daily/{DAY}.md"
    note = parse_daily_markdown(daily_path.read_text(encoding="utf-8"), DAY)
    assert note.evening_validated is True
    assert "- Conclusão: 33%" in daily_path.read_text(encoding="utf-8")

    posts_dir = freeze_worktree_path(config.state_dir / "wiki-worktrees", DAY) / "brand/posts"
    spont_path = posts_dir / post_note_filename(SPONT_URL, DAY)
    spont = parse_post_markdown(
        spont_path.read_text(encoding="utf-8"), filename=spont_path.name
    )
    assert spont.guided is False and spont.url == SPONT_URL

    proposed = config.state_dir / "plans" / f"{NEXT.isoformat()}.proposed.json"
    assert proposed.exists()
    plan_path = tmp_path / "next-plan.json"
    plan_path.write_text(proposed.read_text(encoding="utf-8"), encoding="utf-8")

    result = run_morning(
        config,
        day=NEXT,
        plan_path=plan_path,
        local_only_wiki=True,
        replay=True,
    )
    html = result.html_path.read_text(encoding="utf-8")
    assert "writeback da noite" in html
    assert "33%" in html or "Conclusão" in html
