"""Real browser checks for evening draft persistence and copy-prompt parity."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from swarm_reports.evening_schema import EveningChecklistItem, EveningPayload, SCHEMA_VERSION
from swarm_reports.plan import MorningPlan
from swarm_reports.rendering.html import MorningViewModel, render_morning_html

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


def _html_path(tmp_path: Path) -> Path:
    plan = MorningPlan(
        day=date(2026, 9, 14),
        checklist=[
            {
                "task_id": "t1",
                "text": "Tarefa</script>evil",
                "first_planned": "2026-09-14",
                "is_p0": True,
                "source_pointer": "",
            }
        ],
    )
    seed = EveningPayload(
        schema_version=SCHEMA_VERSION,
        day=date(2026, 9, 14),
        owner_id="owner",
        checklist=[EveningChecklistItem(task_id="t1", done=False)],
    )
    html = render_morning_html(
        MorningViewModel(day=date(2026, 9, 14), owner_id="owner", metrics=[], plan=plan, evening_seed=seed)
    )
    path = tmp_path / "morning.html"
    path.write_text(html, encoding="utf-8")
    return path


def test_browser_375_localstorage_before_blur_and_copy_parity(tmp_path):
    path = _html_path(tmp_path)
    url = path.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 812})
        page.goto(url)
        page.click('button[data-tab="Noite"]')
        notes = page.locator("#evening-notes")
        notes.fill("nota sem blur")
        stored = page.evaluate(
            """() => {
              const key = Object.keys(localStorage).find(k => k.startsWith('evening:'));
              return key ? localStorage.getItem(key) : null;
            }"""
        )
        assert stored is not None
        payload = json.loads(stored)["payload"]
        assert payload.get("notes") == "nota sem blur"

        page.evaluate(
            """() => {
              navigator.clipboard.writeText = (text) => {
                window.__copiedPayload = text;
                return Promise.resolve();
              };
            }"""
        )
        page.click("#copy-prompt")
        copied = page.evaluate("() => window.__copiedPayload")
        assert copied is not None
        copied_payload = json.loads(copied)
        assert copied_payload.get("notes") == payload.get("notes")
        assert copied_payload.get("checklist") == payload.get("checklist")
        assert "<script>alert" not in page.content()
        assert "Tarefa</script>evil" not in page.content()
        box = page.locator("body").bounding_box()
        assert box and box["width"] <= 375
        browser.close()
