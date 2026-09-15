"""Regressions for the F2 review blockers.

Each test names the behaviour that was wrong, so a future rewrite that reintroduces
the bug fails here rather than in production on a morning the owner depends on.
"""

import json
import multiprocessing
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from swarm_reports import morning as morning_mod
from swarm_reports.evening_schema import (
    SCHEMA_VERSION,
    EveningPayload,
    parse_evening_payload,
)
from swarm_reports.metrics.state import (
    FrozenItem,
    ReportsState,
    apply_morning_freeze,
    default_state_path,
    load_state,
    mark_evening_validated,
    save_state,
)
from swarm_reports.morning import run_morning
from swarm_reports.plan import MorningPlan
from swarm_reports.sources import (
    CandidateItem,
    EmptyConfirmedSources,
    UnavailableSources,
    build_plan_from_sources,
    select_p0,
)
from swarm_reports.storage import load_progress
from swarm_reports.tiles import build_metric_tiles, completion_window, open_drift, res_window
from swarm_reports.metrics.res import load_res_weights
from tests.test_morning_freeze import DAY, config_for, init_wiki, plan_file

WEIGHTS = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"


# --------------------------------------------------------------------------------------
# Blocker: P0 selection required priority AND deadline, and the plan carried only P0.
# --------------------------------------------------------------------------------------


def test_p0_selection_is_priority_or_deadline():
    candidates = [
        CandidateItem("MAT-1", "urgent, no date", "linear:1", priority=1),
        CandidateItem("MAT-2", "high, far deadline", "linear:2", priority=2,
                      deadline=DAY + timedelta(days=30)),
        CandidateItem("MAT-3", "no priority, close deadline", "cal:3",
                      deadline=DAY + timedelta(days=3)),
        CandidateItem("MAT-4", "no priority, far deadline", "cal:4",
                      deadline=DAY + timedelta(days=30)),
    ]
    chosen = {item.task_id for item in select_p0(candidates, DAY)}
    assert chosen == {"MAT-1", "MAT-2", "MAT-3"}


def test_p0_labels_count_as_priority():
    candidates = [CandidateItem("MAT-9", "urgente", "linear:9", priority_label="Urgent")]
    assert [c.task_id for c in select_p0(candidates, DAY)] == ["MAT-9"]


def test_plan_keeps_ordinary_items_outside_the_p0_subset():
    class Sources:
        def linear_candidates(self, day):
            from swarm_reports.plan import SourceRef

            items = [
                CandidateItem(f"MAT-{i}", f"item {i}", f"linear:{i}", priority=1)
                for i in range(1, 6)
            ]
            return items, SourceRef(kind="linear", pointer="q", status="ok")

        def calendar_candidates(self, day):
            from swarm_reports.plan import SourceRef

            return [], SourceRef(kind="calendar", pointer="q", status="ok")

    plan = build_plan_from_sources(DAY, Sources())
    assert len(plan.checklist) == 5
    assert len(plan.p0_items) == 3
    assert plan.confirmed_empty is False


def test_plan_rejects_more_than_three_declared_p0():
    with pytest.raises(ValueError, match="at most 3 P0"):
        MorningPlan.from_json(
            {
                "day": DAY.isoformat(),
                "checklist": [
                    {"task_id": f"t{i}", "text": f"x{i}", "is_p0": True} for i in range(4)
                ],
            }
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"checklist": [{"text": ""}]},
        {"checklist": [{"task_id": "t", "text": "x", "is_p0": "yes"}]},
        {"checklist": [{"task_id": "t", "text": "x", "first_planned": "not-a-date"}]},
        {"checklist": [{"task_id": "t", "text": "x", "first_planned": "2099-01-01"}]},
        {"handoffs": [{"title": "t", "context_links": ["javascript:alert(1)"]}]},
        {"lesson": {"link": "javascript:alert(1)"}},
        {"lesson": {"link": "/etc/passwd"}},
        {"confirmed_empty": "true"},
    ],
)
def test_plan_validates_shape_and_urls(bad):
    with pytest.raises(ValueError):
        MorningPlan.from_json({"day": DAY.isoformat(), **bad})


def test_plan_allows_internal_lesson_path():
    plan = MorningPlan.from_json(
        {"day": DAY.isoformat(), "lesson": {"link": "/lessons/aws-sap.html"}}
    )
    assert plan.lesson.link == "/lessons/aws-sap.html"


# --------------------------------------------------------------------------------------
# Blocker: a source error could freeze an empty, irreversible day.
# --------------------------------------------------------------------------------------


def test_unreadable_sources_never_freeze_an_empty_day(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    with pytest.raises(ValueError, match="refusing to freeze"):
        run_morning(config, day=DAY, sources=UnavailableSources(), local_only_wiki=True)
    state = load_state(default_state_path(config.state_dir))
    assert DAY.isoformat() not in state.days


def test_confirmed_empty_day_can_freeze(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    result = run_morning(config, day=DAY, sources=EmptyConfirmedSources(), local_only_wiki=True)
    assert result.freeze_applied is True
    state = load_state(default_state_path(config.state_dir))
    assert state.days[DAY.isoformat()].morning_freeze_applied is True
    assert state.days[DAY.isoformat()].frozen_checklist == []


# --------------------------------------------------------------------------------------
# Blocker: the duplicate path re-invoked the planner and returned without rendering.
# --------------------------------------------------------------------------------------


PLANNER_SCRIPT = """
import json, pathlib, sys
counter = pathlib.Path(sys.argv[1])
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
json.load(sys.stdin)
sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())
"""


def _provider_config(tmp_path, wiki, plan_payload):
    script = tmp_path / "planner.py"
    script.write_text(PLANNER_SCRIPT, encoding="utf-8")
    counter = tmp_path / "planner-calls.txt"
    payload = tmp_path / "planner-plan.json"
    payload.write_text(json.dumps(plan_payload), encoding="utf-8")
    from swarm_reports.config import PlannerProviderConfig

    provider = PlannerProviderConfig(
        command=(sys.executable, str(script), str(counter), str(payload))
    )
    return config_for(tmp_path, wiki, planner_provider=provider), counter


def _calls(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


def test_rerun_does_not_redispatch_the_planner(tmp_path):
    wiki = init_wiki(tmp_path)
    config, counter = _provider_config(
        tmp_path,
        wiki,
        {
            "day": DAY.isoformat(),
            "checklist": [{"task_id": "MAT-1", "text": "provider item", "is_p0": True}],
            "sources": [{"kind": "linear", "pointer": "q", "status": "ok"}],
        },
    )
    first = run_morning(config, day=DAY, local_only_wiki=True)
    assert first.planner_dispatched is True
    assert _calls(counter) == 1

    second = run_morning(config, day=DAY, local_only_wiki=True)
    assert second.planner_dispatched is False
    assert _calls(counter) == 1
    assert second.freeze_reused is True
    assert second.html_path.is_file()


def test_alternate_plan_refreshes_context_but_not_the_frozen_checklist(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    first_plan = plan_file(tmp_path, [{"task_id": "MAT-1", "text": "frozen item", "is_p0": True}])
    run_morning(config, plan_path=first_plan, day=DAY, local_only_wiki=True)

    alternate = tmp_path / "alternate.json"
    alternate.write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "checklist": [{"task_id": "MAT-99", "text": "different item", "is_p0": True}],
                "handoffs": [{"task_id": "MAT-1", "title": "handoff novo", "objective": "obj"}],
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    second = run_morning(config, plan_path=alternate, day=DAY, local_only_wiki=True)
    assert [item["task_id"] for item in second.plan.checklist] == ["MAT-1"]
    html = second.html_path.read_text(encoding="utf-8")
    assert "frozen item" in html
    assert "different item" not in html
    assert "handoff novo" in html


# --------------------------------------------------------------------------------------
# Blocker: the claim file was written before the work, wedging the day after a crash.
# --------------------------------------------------------------------------------------


def test_failed_first_run_does_not_wedge_the_day(tmp_path, monkeypatch):
    wiki = init_wiki(tmp_path)
    config, counter = _provider_config(
        tmp_path,
        wiki,
        {
            "day": DAY.isoformat(),
            "checklist": [{"task_id": "MAT-1", "text": "item", "is_p0": True}],
            "sources": [{"kind": "linear", "pointer": "q", "status": "ok"}],
        },
    )
    real = morning_mod.apply_wiki_freeze
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("git exploded mid-freeze")
        return real(*args, **kwargs)

    monkeypatch.setattr(morning_mod, "apply_wiki_freeze", flaky)

    with pytest.raises(RuntimeError, match="git exploded"):
        run_morning(config, day=DAY, local_only_wiki=True)
    progress = load_progress(config.state_dir, DAY)
    assert progress.phase == "planned"
    assert progress.last_error

    result = run_morning(config, day=DAY, local_only_wiki=True)
    assert result.freeze_applied is True
    assert result.html_path.is_file()
    # The uncertain provider ran once and was never relaunched.
    assert _calls(counter) == 1
    assert load_progress(config.state_dir, DAY).phase == "completed"


def test_completed_is_only_written_after_the_html_exists(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    path = plan_file(tmp_path, [{"task_id": "MAT-1", "text": "item", "is_p0": True}])
    result = run_morning(config, plan_path=path, day=DAY, local_only_wiki=True)
    progress = load_progress(config.state_dir, DAY)
    assert progress.phase == "completed"
    assert progress.html_path == str(result.html_path)
    assert Path(progress.html_path).is_file()


def _concurrent_worker(state_dir: str, wiki: str, out: str, script: str, counter: str, payload: str):
    from swarm_reports.config import PlannerProviderConfig, ReportsConfig

    config = ReportsConfig(
        wiki_dir=Path(wiki),
        state_dir=Path(state_dir),
        weights_path=WEIGHTS,
        output_dir=Path(out),
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=PlannerProviderConfig(
            command=(sys.executable, script, counter, payload)
        ),
    )
    run_morning(config, day=DAY, local_only_wiki=True)


def test_concurrent_runs_launch_the_planner_once(tmp_path):
    wiki = init_wiki(tmp_path)
    config, counter = _provider_config(
        tmp_path,
        wiki,
        {
            "day": DAY.isoformat(),
            "checklist": [{"task_id": "MAT-1", "text": "item", "is_p0": True}],
            "sources": [{"kind": "linear", "pointer": "q", "status": "ok"}],
        },
    )
    args = (
        str(config.state_dir),
        str(wiki),
        str(config.output_dir),
        str(tmp_path / "planner.py"),
        str(counter),
        str(tmp_path / "planner-plan.json"),
    )
    procs = [
        multiprocessing.Process(target=_concurrent_worker, args=args) for _ in range(3)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    assert _calls(counter) == 1
    state = load_state(default_state_path(config.state_dir))
    assert len(state.days[DAY.isoformat()].frozen_checklist) == 1


# --------------------------------------------------------------------------------------
# Blocker: every metric tile was hardcoded to "sem validação".
# --------------------------------------------------------------------------------------


def _state_with_history() -> ReportsState:
    state = ReportsState()
    yesterday = DAY - timedelta(days=1)
    apply_morning_freeze(
        state,
        yesterday,
        [
            FrozenItem(task_id="A", text="a", first_planned=yesterday, is_p0=True),
            FrozenItem(task_id="B", text="b", first_planned=yesterday),
            FrozenItem(task_id="C", text="c", first_planned=yesterday),
            FrozenItem(task_id="D", text="d", first_planned=yesterday),
        ],
    )
    mark_evening_validated(state, yesterday, validated_ids=["A", "B"])
    two_days = DAY - timedelta(days=2)
    apply_morning_freeze(
        state,
        two_days,
        [FrozenItem(task_id="E", text="e", first_planned=two_days)],
    )
    return state


def test_yesterday_completion_is_a_real_number():
    state = _state_with_history()
    tiles = build_metric_tiles(state, DAY, wiki_dir=Path("/nonexistent"), weights_path=WEIGHTS)
    labelled = {tile.label: tile.value for tile in tiles}
    assert labelled["Ontem"] == "50%"


def test_seven_day_window_excludes_unclosed_days_from_the_mean():
    state = _state_with_history()
    window = completion_window(state, DAY)
    assert window.planned_days == 2
    assert window.closed_days == 1
    assert window.average == pytest.approx(0.5)


def test_drift_includes_unclosed_history_and_carryover():
    state = _state_with_history()
    drift = open_drift(state, DAY)
    # C and D open since yesterday (1 day each), E open since two days ago (2 days).
    assert drift.open_items == 3
    assert drift.total_days == 1 + 1 + 2


def test_drift_uses_carryover_first_planned_after_rollover():
    state = ReportsState()
    monday = DAY - timedelta(days=3)
    apply_morning_freeze(state, monday, [FrozenItem(task_id="A", text="a", first_planned=monday)])
    # The item slips to today; the plan naively re-declares first_planned as today.
    from swarm_reports.metrics.state import carryover_first_planned

    assert carryover_first_planned(state, "A", DAY) == monday
    apply_morning_freeze(
        state, DAY, [FrozenItem(task_id="A", text="a", first_planned=monday)]
    )
    assert open_drift(state, DAY).total_days == 3


def _post(day: date, guided: bool, reach: int, comments: int) -> str:
    return (
        "---\n"
        f"guided: {'true' if guided else 'false'}\n"
        "platform: linkedin\n"
        "url: https://example.com/p\n"
        f"posted_on: {day.isoformat()}\n"
        "metrics:\n"
        f"  reach: {reach}\n"
        "  outside_fraction: 0.5\n"
        "  signals:\n"
        f"    comments: {comments}\n"
        "---\n\n# post\n"
    )


def test_res_window_keeps_guided_and_spontaneous_apart(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "a.md").write_text(_post(DAY - timedelta(days=1), True, 1000, 10), encoding="utf-8")
    (posts / "b.md").write_text(_post(DAY - timedelta(days=2), False, 1000, 2), encoding="utf-8")
    (posts / "old.md").write_text(_post(DAY - timedelta(days=40), True, 1000, 99), encoding="utf-8")
    (posts / "unflagged.md").write_text(
        "---\nplatform: linkedin\nposted_on: %s\n---\n" % (DAY - timedelta(days=1)).isoformat(),
        encoding="utf-8",
    )
    window = res_window(posts, load_res_weights(WEIGHTS), DAY)
    assert window.guided_average == pytest.approx(40.0)
    assert window.spontaneous_average == pytest.approx(8.0)
    assert window.guided_average != window.spontaneous_average
    assert window.guided_per_week == pytest.approx(1.0)
    assert window.spontaneous_per_week == pytest.approx(1.0)
    assert window.unflagged == 1


def test_res_tile_reports_the_two_groups_separately(tmp_path):
    wiki = tmp_path / "wiki"
    posts = wiki / "brand" / "posts"
    posts.mkdir(parents=True)
    (posts / "a.md").write_text(_post(DAY - timedelta(days=1), True, 1000, 10), encoding="utf-8")
    tiles = {t.label: t.value for t in build_metric_tiles(ReportsState(), DAY, wiki_dir=wiki, weights_path=WEIGHTS)}
    assert "RES guiado 7d" in tiles
    assert "RES espontâneo 7d" in tiles
    assert tiles["RES espontâneo 7d"].startswith("sem validação")


# --------------------------------------------------------------------------------------
# Blocker: the evening schema coerced types, truncated silently and could not
# represent what F4 needs.
# --------------------------------------------------------------------------------------


def _payload(**extra):
    return {
        "schema_version": SCHEMA_VERSION,
        "day": DAY.isoformat(),
        "owner_id": "owner",
        "checklist": [{"task_id": "MAT-1", "done": True}],
        **extra,
    }


def test_blank_notes_are_legal():
    payload = parse_evening_payload(_payload(notes=""))
    assert payload.notes == ""


def test_missing_notes_is_legal():
    assert parse_evening_payload(_payload()).notes == ""


@pytest.mark.parametrize(
    "raw",
    [
        {"checklist": [{"task_id": "t", "done": "false"}]},
        {"checklist": [{"task_id": "t", "done": 0}]},
        {"checklist": [{"task_id": "t", "done": 1}]},
        {"checklist": [{"task_id": "t", "done": None}]},
    ],
)
def test_done_must_be_a_real_boolean(raw):
    with pytest.raises(ValueError, match="boolean"):
        parse_evening_payload(_payload(**raw))


@pytest.mark.parametrize("version", ["1", 1.0, True, None])
def test_schema_version_must_be_an_integer(version):
    body = _payload()
    body["schema_version"] = version
    with pytest.raises(ValueError):
        parse_evening_payload(body)


def test_unknown_keys_are_rejected():
    with pytest.raises(ValueError, match="unknown keys: lesson_completed"):
        parse_evening_payload(_payload(lesson_completed=True))


def test_unknown_nested_keys_are_rejected():
    with pytest.raises(ValueError, match="unknown keys"):
        parse_evening_payload(_payload(checklist=[{"task_id": "t", "done": True, "oops": 1}]))


def test_non_finite_metrics_are_rejected():
    body = json.loads('{"reach": NaN}')
    with pytest.raises(ValueError, match="finite"):
        parse_evening_payload(
            _payload(
                posts=[
                    {
                        "url": "https://example.com/p",
                        "platform": "linkedin",
                        "guided": True,
                        "checkpoint": "48h",
                        "metrics": body,
                    }
                ]
            )
        )


def test_negative_metrics_are_rejected():
    with pytest.raises(ValueError, match="negative"):
        parse_evening_payload(
            _payload(
                posts=[
                    {
                        "url": "https://example.com/p",
                        "platform": "linkedin",
                        "guided": False,
                        "checkpoint": "7d",
                        "metrics": {"reach": -1},
                    }
                ]
            )
        )


def test_oversize_notes_raise_instead_of_truncating():
    with pytest.raises(ValueError, match="exceeds"):
        parse_evening_payload(_payload(notes="x" * 5000))


def test_oversize_lists_raise_instead_of_truncating():
    with pytest.raises(ValueError, match="exceeds"):
        parse_evening_payload(
            _payload(pending_checkpoint_ids=[f"c{i}" for i in range(200)])
        )


def test_duplicate_checklist_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        parse_evening_payload(
            _payload(checklist=[{"task_id": "t", "done": True}, {"task_id": "t", "done": False}])
        )


def test_unplanned_carries_text_done_and_generated_id():
    payload = parse_evening_payload(
        _payload(unplanned=[{"text": "consertei o CI", "done": True}])
    )
    assert payload.unplanned[0].task_id.startswith("hash:")
    assert payload.unplanned[0].done is True
    assert payload.unplanned[0].classification is None


def test_blank_classification_is_null_and_unpenalized():
    payload = parse_evening_payload(
        _payload(unplanned=[{"text": "x", "done": True, "classification": "  "}])
    )
    assert payload.unplanned[0].classification is None


def test_posts_carry_url_platform_guided_checkpoint_and_metrics():
    payload = parse_evening_payload(
        _payload(
            posts=[
                {
                    "url": "https://linkedin.com/p/1",
                    "platform": "linkedin",
                    "guided": False,
                    "checkpoint": "48h",
                    "metrics": {
                        "reach": 320,
                        "outside_fraction": 0.77,
                        "signals": {"comments": 4, "reactions": 12},
                    },
                }
            ]
        )
    )
    post = payload.posts[0]
    assert post.guided is False
    assert post.checkpoint == "48h"
    assert post.metrics.signals == {"comments": 4.0, "reactions": 12.0}


def test_review_edit_requires_content():
    with pytest.raises(ValueError, match="content is required"):
        parse_evening_payload(
            _payload(review_actions=[{"draft_id": "d1", "action": "edit"}])
        )
    payload = parse_evening_payload(
        _payload(review_actions=[{"draft_id": "d1", "action": "edit", "content": "novo texto"}])
    )
    assert payload.review_actions[0].content == "novo texto"


def test_ledger_classifications_round_trip():
    payload = parse_evening_payload(
        _payload(ledger_classifications=[{"entry_id": "pr-42", "classification": "oportunidade"}])
    )
    assert payload.ledger_classifications[0].classification == "oportunidade"


def test_transport_timestamp_is_outside_the_content_hash():
    a = parse_evening_payload(_payload(client_saved_at="2026-09-14T22:00:00Z"))
    b = parse_evening_payload(_payload(client_saved_at="2026-09-14T23:30:00Z"))
    assert a.to_canonical_json() == b.to_canonical_json()
    assert a.to_json() != b.to_json()


def test_owner_comes_from_config_not_the_body():
    payload = parse_evening_payload(_payload(owner_id="attacker"))
    assert payload.with_owner("owner").owner_id == "owner"


def test_schema_document_covers_every_field():
    from swarm_reports.evening_schema import evening_payload_json_schema

    schema = evening_payload_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) >= {
        "schema_version",
        "day",
        "owner_id",
        "checklist",
        "unplanned",
        "posts",
        "ledger_classifications",
        "review_actions",
        "pending_checkpoint_ids",
        "notes",
    }
    assert "lesson_completed" not in schema["properties"]


# --------------------------------------------------------------------------------------
# Blocker: the form had no unplanned/post/edit fields and two payload builders.
# --------------------------------------------------------------------------------------


def _render(tmp_path) -> str:
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki, public_origin="https://reports.mathai.com.br")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "checklist": [
                    {"task_id": "MAT-1", "text": "P0 <b>bold</b>", "is_p0": True},
                    {"task_id": "MAT-2", "text": "ordinary"},
                ],
                "agenda": [{"title": "Reunião", "when": "09:00"}],
                "ledger": [
                    {"entry_id": "pr-42", "kind": "pr", "summary": "merged",
                     "link": "https://github.com/x/y/pull/42"}
                ],
                "review_drafts": [
                    {"draft_id": "d1", "kind": "post", "reason": "porque", "content": "rascunho"}
                ],
                "lesson": {"link": "/lessons/aws.html", "topic": "SAP"},
                "sources": [{"kind": "linear", "pointer": "q", "status": "ok"},
                            {"kind": "calendar", "pointer": "q", "status": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    result = run_morning(config, plan_path=plan, day=DAY, local_only_wiki=True)
    return result.html_path.read_text(encoding="utf-8")


def test_form_has_one_payload_builder_used_by_copy_and_post(tmp_path):
    html = _render(tmp_path)
    assert html.count("function readForm()") == 1
    copy_block = html.split('byId("copy-prompt")')[1].split('byId("send-evening")')[0]
    send_block = html.split('byId("send-evening")')[1]
    assert "readForm()" in copy_block
    assert "readForm()" in send_block
    # Only the POST carries transport metadata; the clipboard stays canonical content.
    assert "client_saved_at" in send_block
    assert "client_saved_at" not in copy_block


def test_form_exposes_every_field_f4_needs(tmp_path):
    html = _render(tmp_path)
    for marker in (
        "data-check-row",
        "data-check-class",
        "data-unplanned-text",
        "data-unplanned-done",
        "data-unplanned-class",
        "data-post-url",
        "data-post-guided",
        "data-post-checkpoint",
        "data-post-reach",
        "data-post-outside",
        "data-post-signal",
        "data-ledger-class",
        "data-review-action",
        "data-review-content",
        "evening-notes",
    ):
        assert marker in html, marker


def test_seed_embedded_in_the_page_is_a_valid_payload(tmp_path):
    html = _render(tmp_path)
    match = re.search(r"const SEED = (\{.*?\});\n", html, re.DOTALL)
    assert match
    seed = json.loads(match.group(1))
    parsed = parse_evening_payload(seed)
    assert [item.task_id for item in parsed.checklist] == ["MAT-1", "MAT-2"]
    assert [entry.entry_id for entry in parsed.ledger_classifications] == ["pr-42"]


def test_rendered_page_escapes_plan_text_and_uses_no_external_script(tmp_path):
    html = _render(tmp_path)
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "<script src" not in html
    assert "https://reports.mathai.com.br" not in html  # same-origin relative URLs only
    assert '"/evening"' in html


def test_local_storage_versioned_by_revision_not_clock(tmp_path):
    html = _render(tmp_path)
    assert "revision: revision" in html or "revision: currentRevision" in html
    storage = html.split("function saveLocal")[1].split("function loadLocal")[0]
    assert "catch" in storage
    assert "Date" not in storage
