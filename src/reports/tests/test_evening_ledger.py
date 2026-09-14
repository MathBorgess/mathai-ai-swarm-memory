"""The ledger has one job: say what actually happened, including what did not."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from swarm_reports.evening.ledger import (
    KIND_CONTEST,
    KIND_WIKI_PR,
    STATUS_ACKNOWLEDGED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REVIEW,
    LedgerRecord,
    LedgerStore,
    entry_id_for,
    morning_view,
)

DAY = date(2026, 9, 14)


def _record(store, **kwargs):
    defaults = dict(
        entry_id=entry_id_for(DAY, 1, KIND_WIKI_PR, "branch"),
        day=DAY.isoformat(),
        revision=1,
        kind=KIND_WIKI_PR,
        target="branch",
        status=STATUS_PENDING,
        summary="abrir PR",
    )
    defaults.update(kwargs)
    return store.record(LedgerRecord(**defaults))


def test_entry_ids_are_deterministic_so_a_replay_upserts(tmp_path):
    store = LedgerStore(tmp_path)
    _record(store)
    _record(store, summary="abrir PR (retry)")
    rows = store.entries(DAY)
    assert len(rows) == 1
    assert rows[0].summary == "abrir PR (retry)"
    assert rows[0].recorded_at <= rows[0].updated_at


def test_a_failed_effect_is_never_reported_as_completed(tmp_path):
    store = LedgerStore(tmp_path)
    entry = _record(store)
    store.set_status(DAY, entry.entry_id, STATUS_FAILED, detail="gh pr create failed: no auth")
    row = store.entries(DAY)[0]
    assert row.status == STATUS_FAILED
    assert row.landed is False

    entries, _drafts = morning_view(tmp_path, DAY + timedelta(days=1))
    assert "FALHOU" in entries[0].summary
    assert "no auth" in entries[0].summary


def test_status_of_an_unknown_entry_is_an_error_not_a_silent_write(tmp_path):
    store = LedgerStore(tmp_path)
    with pytest.raises(KeyError):
        store.set_status(DAY, "nope", STATUS_COMPLETED)


def test_owner_classification_survives_a_replay(tmp_path):
    store = LedgerStore(tmp_path)
    entry = _record(store, kind=KIND_CONTEST, status=STATUS_REVIEW, summary="contestar?")
    store.classify(DAY, entry.entry_id, "oportunidade")
    assert store.entries(DAY)[0].status == STATUS_ACKNOWLEDGED

    _record(store, kind=KIND_CONTEST, status=STATUS_REVIEW, summary="contestar?")
    assert store.entries(DAY)[0].classification == "oportunidade"


def test_classifying_an_unknown_id_is_ignored(tmp_path):
    """The form seeds ids from an older report; a miss must not fail the night."""
    assert LedgerStore(tmp_path).classify(DAY, "ghost", "devaneio") is None


def test_morning_shows_yesterday_and_keeps_open_questions_visible(tmp_path):
    store = LedgerStore(tmp_path)
    old = date(2026, 9, 12)
    store.record(
        LedgerRecord(
            entry_id="old-contest",
            day=old.isoformat(),
            revision=1,
            kind=KIND_CONTEST,
            target="t",
            status=STATUS_REVIEW,
            summary="contestar classificação de 12/09",
        )
    )
    _record(store, status=STATUS_COMPLETED, summary="PR aberto", link="https://x/1")

    entries, drafts = morning_view(tmp_path, date(2026, 9, 15))
    assert [e.summary for e in entries] == ["PR aberto"]
    assert entries[0].link == "https://x/1"
    # Two days old and still unanswered: a question that scrolls away was never asked.
    assert [d.reason for d in drafts] == ["contestar classificação de 12/09"]


def test_a_corrupt_ledger_is_refused_rather_than_guessed(tmp_path):
    store = LedgerStore(tmp_path)
    _record(store)
    (tmp_path / "ledger" / f"{DAY.isoformat()}.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        store.entries(DAY)
