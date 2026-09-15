"""The night's text edits. Pure `str -> str`, so every rule is checkable in isolation."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from swarm_reports.metrics.daily import parse_daily_markdown
from swarm_reports.metrics.posts import parse_post_markdown
from swarm_reports.wiki.notes import (
    TomorrowItem,
    UnplannedRecord,
    post_note_filename,
    render_new_post_note,
    update_daily_note,
    update_post_note,
)

DAY = date(2026, 9, 14)
TOMORROW = date(2026, 9, 15)

FROZEN_NOTE = """---
date: 2026-09-14
external:
  calendar:
    - event_id: abc123
      title: 1:1
  linear:
    - MAT-193
tags: [daily, privado]
---

# 2026-09-14

Texto que o dono escreveu de manhã e ninguém pode tocar.

## Hoje

<!-- swarm:frozen-checklist-begin snapshot=2026-09-14T08:00:00-03:00 -->
<!-- swarm:task-meta id=MAT-193 first_planned=2026-09-12 frozen=true -->
- [ ] **P0** fechar broker <!-- swarm:p0 -->
<!-- swarm:task-meta id=MAT-201 first_planned=2026-09-14 frozen=true -->
- [ ] revisar nota
<!-- swarm:frozen-checklist-end -->

## Evolução

- linha antiga do dono

## Amanhã (opcional)

- 
"""


def _update(text=FROZEN_NOTE, **kwargs):
    defaults = dict(
        day=DAY,
        revision=1,
        done_ids=set(),
        classifications={},
        unplanned=[],
        evolution_lines=["- Conclusão: 0%"],
        tomorrow=[],
        tomorrow_day=TOMORROW,
    )
    defaults.update(kwargs)
    return update_daily_note(text, **defaults)


def test_checkboxes_follow_the_validated_ids():
    out = _update(done_ids={"MAT-193"})
    assert "- [x] **P0** fechar broker" in out
    assert "- [ ] revisar nota" in out


def test_owner_prose_and_external_pointers_survive():
    out = _update(done_ids={"MAT-193"})
    assert "Texto que o dono escreveu de manhã e ninguém pode tocar." in out
    assert "event_id: abc123" in out
    assert "- MAT-193" in out
    assert "tags: [daily, privado]" in out
    assert "- linha antiga do dono" in out


def test_frontmatter_records_the_revision_without_reformatting_the_rest():
    out = _update(revision=3)
    assert "swarm_evening_validated: true" in out
    assert "swarm_evening_revision: 3" in out
    front = out.split("---")[1]
    assert "calendar:" in front and "  linear:" in front


def test_first_planned_carryover_is_never_rewritten():
    out = _update(done_ids={"MAT-201"})
    assert "id=MAT-193 first_planned=2026-09-12" in out
    note = parse_daily_markdown(out, DAY)
    carried = next(item for item in note.items if item.task_id == "MAT-193")
    assert carried.first_planned == date(2026, 9, 12)


def test_classification_is_written_once_even_after_a_second_revision():
    once = _update(classifications={"MAT-201": "devaneio"})
    twice = update_daily_note(
        once,
        day=DAY,
        revision=2,
        done_ids=set(),
        classifications={"MAT-201": "procrastinação"},
        unplanned=[],
        evolution_lines=["- Conclusão: 0%"],
        tomorrow=[],
        tomorrow_day=TOMORROW,
    )
    assert twice.count("swarm:class") == 1
    assert "swarm:class procrastinação" in twice
    note = parse_daily_markdown(twice, DAY)
    item = next(i for i in note.items if i.task_id == "MAT-201")
    assert item.classification == "procrastinação"


def test_unplanned_work_stays_outside_the_freeze():
    out = _update(
        unplanned=[
            UnplannedRecord(
                task_id="hash-1",
                text="responder e-mail",
                done=True,
                classification="oportunidade",
                first_planned=DAY,
            )
        ]
    )
    note = parse_daily_markdown(out, DAY)
    extra = next(item for item in note.items if item.task_id == "hash-1")
    assert extra.added_after_freeze is True
    assert extra.frozen is False
    assert extra.done is True
    assert extra.classification == "oportunidade"
    # The denominator is the freeze and only the freeze.
    assert len([i for i in note.items if i.frozen and not i.added_after_freeze]) == 2


def test_a_second_revision_replaces_its_blocks_instead_of_stacking_them():
    first = _update(
        revision=1,
        unplanned=[
            UnplannedRecord("hash-1", "responder e-mail", True, None, DAY),
        ],
        evolution_lines=["- Conclusão: 50%"],
        tomorrow=[TomorrowItem("MAT-201", "revisar nota")],
    )
    second = update_daily_note(
        first,
        day=DAY,
        revision=2,
        done_ids=set(),
        classifications={},
        unplanned=[UnplannedRecord("hash-2", "outra coisa", False, None, DAY)],
        evolution_lines=["- Conclusão: 100%"],
        tomorrow=[TomorrowItem("MAT-193", "fechar broker", is_p0=True)],
        tomorrow_day=TOMORROW,
    )
    assert second.count("swarm:unplanned-begin") == 1
    assert second.count("swarm:evolution-begin") == 1
    assert second.count("swarm:tomorrow-begin") == 1
    assert "responder e-mail" not in second
    assert "- Conclusão: 50%" not in second
    assert "- Conclusão: 100%" in second
    assert "revision=2" in second and "revision=1" not in second


def test_tomorrow_block_is_a_proposal_in_todays_note():
    out = _update(tomorrow=[TomorrowItem("MAT-201", "revisar nota", is_p0=True)])
    assert "swarm:tomorrow-begin day=2026-09-15" in out
    hoje = parse_daily_markdown(out, DAY)
    # The proposal lives under `## Amanhã`, so it can never be read as today's checklist.
    assert {item.task_id for item in hoje.items} == {"MAT-193", "MAT-201"}


def test_missing_sections_are_created_rather_than_crashing():
    minimal = "# 2026-09-14\n\n## Hoje\n\n- [ ] solto\n"
    out = _update(minimal, evolution_lines=["- Conclusão: 0%"], tomorrow=[
        TomorrowItem("x", "amanhã")
    ])
    assert "## Evolução" in out
    assert "## Amanhã" in out


# ------------------------------------------------------------------------- posts

POST_NOTE = """---
guided: true
platform: linkedin
url: https://linkedin.com/posts/abc
posted_on: 2026-09-14
metrics:
  reach: 1000
  signals:
    reactions: 10
checkpoints:
  - at: 48h
    due: 2026-09-16
  - at: 7d
    due: 2026-09-21
---

Corpo do post que o dono escreveu.
"""


def test_launch_metrics_merge_and_guided_is_never_overwritten():
    out = update_post_note(
        POST_NOTE,
        checkpoint="launch",
        metrics={"reach": 1500, "outside_fraction": 0.77, "signals": {"comments": 4}},
    )
    post = parse_post_markdown(out)
    assert post.guided is True
    assert post.metrics.reach == 1500
    assert post.metrics.outside_fraction == 0.77
    assert post.metrics.signals == {"reactions": 10.0, "comments": 4.0}
    assert "Corpo do post que o dono escreveu." in out


def test_zero_is_recorded_and_unknown_is_left_alone():
    out = update_post_note(
        POST_NOTE,
        checkpoint="launch",
        metrics={"reach": None, "outside_fraction": 0.0, "signals": {"comments": 0}},
    )
    front = yaml.safe_load(out.split("---")[1])
    assert front["metrics"]["reach"] == 1000, "unknown must not clear what was known"
    assert front["metrics"]["outside_fraction"] == 0
    assert front["metrics"]["signals"]["comments"] == 0


def test_checkpoint_metrics_land_on_the_matching_entry():
    out = update_post_note(
        POST_NOTE,
        checkpoint="48h",
        metrics={"reach": 2000, "signals": {"reactions": 30}},
    )
    front = yaml.safe_load(out.split("---")[1])
    labels = [entry["at"] for entry in front["checkpoints"]]
    assert labels == ["48h", "7d"], "existing checkpoints are preserved, not replaced"
    assert front["checkpoints"][0]["metrics"]["reach"] == 2000
    assert front["checkpoints"][0]["due"] == date(2026, 9, 16)
    assert "metrics" not in front["checkpoints"][1]


def test_a_missing_checkpoint_is_appended():
    without = POST_NOTE.replace(
        "checkpoints:\n  - at: 48h\n    due: 2026-09-16\n  - at: 7d\n    due: 2026-09-21\n", ""
    )
    out = update_post_note(without, checkpoint="7d", metrics={"reach": 10})
    front = yaml.safe_load(out.split("---")[1])
    assert front["checkpoints"] == [{"at": "7d", "metrics": {"reach": 10}}]


@pytest.mark.parametrize("metrics", [None, {}, {"reach": None, "signals": {}}])
def test_nothing_reported_changes_nothing(metrics):
    out = update_post_note(POST_NOTE, checkpoint="launch", metrics=metrics)
    front = yaml.safe_load(out.split("---")[1])
    assert front["metrics"] == {"reach": 1000, "signals": {"reactions": 10}}
    assert front["guided"] is True


def test_spontaneous_note_is_guided_false_on_a_stable_path():
    url = "https://linkedin.com/posts/spontaneous"
    first = post_note_filename(url, DAY)
    assert first == post_note_filename(url, DAY)
    assert first != post_note_filename(url + "x", DAY)

    note = render_new_post_note(
        url=url,
        platform="linkedin",
        posted_on=DAY,
        guided=False,
        checkpoint="launch",
        metrics={"reach": 400, "signals": {"reactions": 9}},
    )
    post = parse_post_markdown(note, filename=first)
    assert post.guided is False
    assert post.url == url
    assert post.posted_on == DAY
    assert post.metrics.reach == 400
    assert [c.label for c in post.checkpoints] == ["48h", "7d"]
    assert [c.due for c in post.checkpoints] == [date(2026, 9, 16), date(2026, 9, 21)]


def test_a_post_note_with_broken_frontmatter_is_refused():
    with pytest.raises(ValueError):
        update_post_note("---\n- not a mapping\n---\nbody\n", checkpoint="launch", metrics={"reach": 1})
