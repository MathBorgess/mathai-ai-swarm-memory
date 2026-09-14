from datetime import date

from swarm_reports.evening_schema import EveningChecklistItem, EveningPayload, SCHEMA_VERSION
from swarm_reports.plan import MorningPlan, SourceRef
from swarm_reports.rendering.html import MorningViewModel, MetricTile, render_morning_html, escape_html, json_for_script


def test_escape_html_injection():
    evil = '<script>alert(1)</script>'
    out = render_morning_html(
        MorningViewModel(
            day=date(2026, 9, 14),
            owner_id="owner",
            metrics=[MetricTile(evil, "1")],
            plan=MorningPlan(
                day=date(2026, 9, 14),
                p0_items=[{"task_id": "t1", "text": evil, "first_planned": "2026-09-14", "is_p0": True}],
                handoffs=[],
                sources=[SourceRef(kind="linear", pointer="", status="unavailable")],
            ),
            evening_seed=EveningPayload(
                schema_version=SCHEMA_VERSION,
                day=date(2026, 9, 14),
                owner_id="owner",
                checklist=[EveningChecklistItem(task_id="t1", done=False)],
            ),
        )
    )
    assert "<script>alert(1)</script>" not in out
    assert escape_html(evil) in out
    assert "</script>" not in json_for_script({"x": "</script>"})


def test_layout_375_and_save_synthetic(tmp_path):
    html = render_morning_html(
        MorningViewModel(
            day=date(2026, 9, 14),
            owner_id="owner",
            metrics=[MetricTile("RES 7d", "sem validação")],
            plan=MorningPlan(day=date(2026, 9, 14), p0_items=[], handoffs=[], sources=[]),
            evening_seed=EveningPayload(
                schema_version=SCHEMA_VERSION,
                day=date(2026, 9, 14),
                owner_id="owner",
                checklist=[],
            ),
        )
    )
    assert "max-width: 375px" in html
    assert "copy-prompt" in html
    path = tmp_path / "synthetic-morning.html"
    path.write_text(html, encoding="utf-8")
    assert path.stat().st_size > 200
