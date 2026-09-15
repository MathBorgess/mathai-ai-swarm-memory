"""The night engine: ordering, idempotency, and honest effect status."""

from __future__ import annotations

import json
from datetime import date

import pytest

from swarm_reports.evening.config import PUBLISH_GH, EveningConfig, PublishConfig
from swarm_reports.evening.ledger import (
    KIND_CONTEST,
    KIND_TOMORROW_PLAN,
    KIND_WIKI_COMMIT,
    KIND_WIKI_PR,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REVIEW,
    LedgerStore,
)
from swarm_reports.evening.session import (
    STATUS_ALREADY_APPLIED,
    STATUS_APPLIED,
    STATUS_REPUBLISHED,
    STATUS_SUPERSEDED,
    EveningRejected,
    load_session,
    run_evening_session,
)
from swarm_reports.evening_schema import parse_evening_payload
from swarm_reports.metrics.daily import parse_daily_markdown
from swarm_reports.metrics.state import load_state
from swarm_reports.server.revisions import RevisionStore
from swarm_reports.server.submit import submit_evening
from swarm_reports.storage import StateTransaction
from swarm_reports.wiki.freeze import freeze_worktree_path
from tests.conftest import run_morning_day

DAY = date(2026, 9, 14)
CHECKLIST = [
    {"task_id": "MAT-193", "text": "fechar broker", "is_p0": True},
    {"task_id": "MAT-201", "text": "revisar nota", "first_planned": "2026-09-12"},
    {"task_id": "MAT-202", "text": "tarefa comum"},
]


@pytest.fixture
def frozen(f4_config, tmp_path):
    run_morning_day(f4_config, DAY, CHECKLIST, tmp_path)
    return f4_config


def _submit(config, **extra):
    body = {
        "schema_version": 1,
        "day": DAY.isoformat(),
        "owner_id": "owner",
        "checklist": [
            {"task_id": "MAT-193", "done": True},
            {"task_id": "MAT-201", "done": False},
            {"task_id": "MAT-202", "done": False},
        ],
    }
    body.update(extra)
    return submit_evening(config, parse_evening_payload(body))


def _daily(config):
    path = freeze_worktree_path(config.state_dir / "wiki-worktrees", DAY) / f"daily/{DAY}.md"
    return path.read_text(encoding="utf-8")


def test_a_revision_is_written_back_to_the_vault_and_to_the_state(frozen):
    outcome = _submit(frozen)
    result = run_evening_session(frozen, day=DAY, revision=outcome.revision)

    assert result.status == STATUS_APPLIED
    assert result.completion == pytest.approx(1 / 3)

    note = parse_daily_markdown(_daily(frozen), DAY)
    assert {i.task_id for i in note.items if i.done} == {"MAT-193"}
    assert note.evening_validated is True

    bucket = load_state(StateTransaction(frozen.state_dir).state_path).days[DAY.isoformat()]
    assert bucket.evening_validated is True
    assert bucket.evening_validated_ids == ["MAT-193"]
    assert bucket.evening_revision == 1


def test_the_evolution_block_carries_metrics_computed_from_the_freeze(frozen):
    _submit(frozen)
    run_evening_session(frozen, day=DAY, revision=1)
    body = _daily(frozen)
    assert "- Conclusão: 33% (1/3 do checklist congelado)" in body
    assert "- 7d conclusão: 33% (1/1 dias fechados)" in body
    assert "- P0 em aberto:" not in body, "the only P0 was validated as done"
    assert "- RES 7d: guiado" in body


def test_ordinary_non_p0_tasks_are_part_of_the_denominator(frozen):
    """Freezing only the P0 subset was the F2 bug; the evening must see all three."""
    _submit(frozen)
    result = run_evening_session(frozen, day=DAY, revision=1)
    assert result.completion == pytest.approx(1 / 3)
    note = parse_daily_markdown(_daily(frozen), DAY)
    assert len([i for i in note.items if i.frozen and not i.added_after_freeze]) == 3


def test_an_identical_resubmit_never_reaches_the_night_twice(frozen):
    first = _submit(frozen)
    run_evening_session(frozen, day=DAY, revision=first.revision)
    head = _commit_count(frozen)

    again = _submit(frozen)
    assert again.changed is False and again.revision == first.revision

    result = run_evening_session(frozen, day=DAY, revision=again.revision)
    assert result.status == STATUS_ALREADY_APPLIED
    assert _commit_count(frozen) == head


def test_a_changed_revision_produces_one_more_commit_not_a_duplicate_block(frozen):
    _submit(frozen)
    run_evening_session(frozen, day=DAY, revision=1)
    before = _commit_count(frozen)

    second = _submit(
        frozen,
        checklist=[
            {"task_id": "MAT-193", "done": True},
            {"task_id": "MAT-201", "done": True},
            {"task_id": "MAT-202", "done": False},
        ],
    )
    assert second.revision == 2
    result = run_evening_session(frozen, day=DAY, revision=2)

    assert result.status == STATUS_APPLIED
    assert _commit_count(frozen) == before + 1
    body = _daily(frozen)
    assert body.count("swarm:evolution-begin") == 1
    assert "- Conclusão: 67%" in body


def test_a_stale_revision_cannot_overwrite_a_newer_one(frozen):
    _submit(frozen)
    _submit(frozen, notes="mudei de ideia")
    run_evening_session(frozen, day=DAY, revision=2)
    body = _daily(frozen)

    result = run_evening_session(frozen, day=DAY, revision=1)
    assert result.status == STATUS_SUPERSEDED
    assert _daily(frozen) == body
    assert load_session(frozen.state_dir, DAY).applied_revision == 2


def test_a_crash_after_the_commit_replays_without_a_second_commit(frozen, monkeypatch):
    """The checkpoint is written after the effect, so the window is real: reproduce it."""
    _submit(frozen)
    import swarm_reports.evening.session as session

    real = session.save_session
    calls = {"n": 0}

    def crash_after_wiki(state_dir, progress):
        calls["n"] += 1
        real(state_dir, progress)
        if progress.runs.get("1") and progress.runs["1"].phase == session.PHASE_WIKI:
            raise RuntimeError("power cut")

    monkeypatch.setattr(session, "save_session", crash_after_wiki)
    with pytest.raises(RuntimeError):
        run_evening_session(frozen, day=DAY, revision=1)
    after_crash = _commit_count(frozen)

    monkeypatch.setattr(session, "save_session", real)
    result = run_evening_session(frozen, day=DAY, revision=1)
    assert result.status == STATUS_APPLIED
    assert _commit_count(frozen) == after_crash, "identical bytes must not commit again"
    assert result.wiki_commit


def test_a_failed_pull_request_is_failed_in_the_ledger_and_retried_later(frozen, tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh()
    gh.create_returncode = 1
    config = _with_gh(frozen)
    _submit(config)

    from swarm_reports.wiki.publish import GhPullRequestPublisher

    failing = GhPullRequestPublisher(tmp_path / "q", runner=gh)
    result = run_evening_session(config, day=DAY, revision=1, publisher=failing)

    assert result.status == STATUS_APPLIED, "the day still closed; only the PR failed"
    assert result.publish_status == STATUS_FAILED
    ledger = LedgerStore(config.state_dir).entries(DAY)
    pr = next(row for row in ledger if row.kind == KIND_WIKI_PR)
    assert pr.status == STATUS_FAILED
    assert "gh pr create failed" in pr.detail
    assert next(row for row in ledger if row.kind == KIND_WIKI_COMMIT).status == STATUS_COMPLETED

    working = GhPullRequestPublisher(tmp_path / "q", runner=FakeGh())
    retry = run_evening_session(config, day=DAY, revision=1, publisher=working)
    assert retry.status == STATUS_REPUBLISHED
    assert retry.pull_request_url == "https://github.com/owner/wiki/pull/7"
    pr = next(r for r in LedgerStore(config.state_dir).entries(DAY) if r.kind == KIND_WIKI_PR)
    assert pr.status == STATUS_COMPLETED and pr.link == retry.pull_request_url


def test_nothing_is_published_when_the_mode_says_so(frozen):
    queue = frozen.state_dir / "wiki-publish-queue"
    before = sorted(queue.glob("*.json"))
    _submit(frozen)
    result = run_evening_session(frozen, day=DAY, revision=1)

    assert result.publish_status == "skipped"
    assert result.pull_request_url is None
    assert sorted(queue.glob("*.json")) == before, "the night queued no new intent"
    pr = next(r for r in LedgerStore(frozen.state_dir).entries(DAY) if r.kind == KIND_WIKI_PR)
    assert pr.status == "pending" and pr.link is None


def test_unplanned_work_is_recorded_and_can_be_contested(frozen):
    _submit(
        frozen,
        checklist=[
            {"task_id": "MAT-193", "done": False},
            {"task_id": "MAT-201", "done": False},
            {"task_id": "MAT-202", "done": False},
        ],
        unplanned=[
            {"text": "refatorar algo não pedido", "done": True, "classification": "oportunidade"}
        ],
    )
    run_evening_session(frozen, day=DAY, revision=1)

    note = parse_daily_markdown(_daily(frozen), DAY)
    extra = [i for i in note.items if i.added_after_freeze]
    assert len(extra) == 1 and extra[0].classification == "oportunidade"

    contest = next(r for r in LedgerStore(frozen.state_dir).entries(DAY) if r.kind == KIND_CONTEST)
    assert contest.status == STATUS_REVIEW
    assert "contestar" in contest.summary
    # The owner's own answer is still the one written into the vault.
    assert "swarm:class oportunidade" in _daily(frozen)


def test_a_blank_classification_carries_no_penalty(frozen):
    _submit(
        frozen,
        checklist=[{"task_id": t, "done": False} for t in ("MAT-193", "MAT-201", "MAT-202")],
        unplanned=[{"text": "algo fora do plano", "done": True, "classification": None}],
    )
    run_evening_session(frozen, day=DAY, revision=1)
    body = _daily(frozen)
    assert "penalidade de escopo 0" in body
    assert not any(r.kind == KIND_CONTEST for r in LedgerStore(frozen.state_dir).entries(DAY))


def test_tomorrow_is_proposed_with_at_most_three_p0_and_never_frozen(frozen):
    _submit(frozen)
    run_evening_session(frozen, day=DAY, revision=1)

    proposed = json.loads(
        (frozen.state_dir / "plans" / "2026-09-15.proposed.json").read_text(encoding="utf-8")
    )
    assert [item["task_id"] for item in proposed["checklist"]] == ["MAT-201", "MAT-202"]
    assert sum(1 for item in proposed["checklist"] if item["is_p0"]) <= 3

    body = _daily(frozen)
    assert "swarm:tomorrow-begin day=2026-09-15" in body
    state = load_state(StateTransaction(frozen.state_dir).state_path)
    assert "2026-09-15" not in state.days, "a proposal must not freeze anything"

    entry = next(
        r for r in LedgerStore(frozen.state_dir).entries(DAY) if r.kind == KIND_TOMORROW_PLAN
    )
    assert "a manhã ainda congela o plano real" in entry.summary


def test_a_revision_from_another_owner_is_refused(frozen):
    _submit(frozen)
    store = RevisionStore(frozen.state_dir)
    record = store.read_revision(DAY, 1)
    path = store._revision_path(DAY, 1)
    path.write_text(json.dumps({**record.to_json(), "owner_id": "someone"}), encoding="utf-8")
    with pytest.raises(EveningRejected):
        run_evening_session(frozen, day=DAY, revision=1)


def test_a_job_hash_that_does_not_match_the_stored_revision_is_refused(frozen):
    _submit(frozen)
    with pytest.raises(EveningRejected):
        run_evening_session(frozen, day=DAY, revision=1, expected_hash="0" * 64)


def _with_gh(config):
    import dataclasses

    return dataclasses.replace(
        config,
        evening=EveningConfig(
            publish=PublishConfig(mode=PUBLISH_GH),
            wiki_local_only=True,
        ),
    )


def _commit_count(config) -> int:
    from tests.conftest import git

    worktree = freeze_worktree_path(config.state_dir / "wiki-worktrees", DAY)
    return int(git(worktree, "rev-list", "--count", "HEAD"))
