"""Revision, dedup, transaction and outbox behaviour for F3."""

from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from swarm_reports.evening_schema import parse_evening_payload
from swarm_reports.metrics.state import load_state
from swarm_reports.server.outbox import (
    Outbox,
    OutboxJob,
    OutboxWorker,
    PendingDispatch,
)
from swarm_reports.server.revisions import RevisionStore
from swarm_reports.server.submit import (
    SubmitRejected,
    latest_revision_response,
    submit_evening,
    submit_from_file,
)
from swarm_reports.storage import StateTransaction
from tests.conftest import DAY, payload_dict


def submit(config, **extra):
    return submit_evening(config, parse_evening_payload(payload_dict(**extra)))


def test_first_submission_is_revision_one(reports_config):
    outcome = submit(reports_config)
    assert outcome.revision == 1
    assert outcome.changed is True
    store = RevisionStore(reports_config.state_dir)
    assert store.latest(DAY).revision == 1


def test_identical_resend_creates_no_revision_and_no_job(reports_config):
    first = submit(reports_config)
    outbox = Outbox(reports_config.state_dir)
    assert len(outbox.pending()) == 1

    second = submit(reports_config)
    assert second.revision == first.revision
    assert second.changed is False
    assert len(outbox.pending()) == 1
    assert len(RevisionStore(reports_config.state_dir).history(DAY)) == 1


def test_resend_differing_only_by_transport_timestamp_is_identical(reports_config):
    first = submit(reports_config, client_saved_at="2026-09-14T22:00:00Z")
    second = submit(reports_config, client_saved_at="2026-09-14T23:45:00Z")
    assert second.changed is False
    assert second.revision == first.revision


def test_changed_submission_creates_a_new_revision(reports_config):
    submit(reports_config)
    changed = submit(reports_config, notes="fechei o dia")
    assert changed.revision == 2
    assert changed.changed is True
    assert len(Outbox(reports_config.state_dir).pending()) == 2


def test_reverting_to_a_previous_content_is_a_new_revision(reports_config):
    """A→B→A is a real change of mind; hashing against history would swallow it."""
    a1 = submit(reports_config, notes="A")
    b = submit(reports_config, notes="B")
    a2 = submit(reports_config, notes="A")
    assert (a1.revision, b.revision, a2.revision) == (1, 2, 3)
    assert a2.content_hash == a1.content_hash
    assert a2.changed is True
    history = RevisionStore(reports_config.state_dir).history(DAY)
    assert [r.revision for r in history] == [1, 2, 3]


def test_revisions_are_immutable(reports_config):
    submit(reports_config, notes="A")
    store = RevisionStore(reports_config.state_dir)
    original = json.loads(
        (store.day_dir(DAY) / "revisions" / "0001.json").read_text(encoding="utf-8")
    )
    submit(reports_config, notes="B")
    after = json.loads(
        (store.day_dir(DAY) / "revisions" / "0001.json").read_text(encoding="utf-8")
    )
    assert after == original


def test_revision_records_the_server_owner_not_the_body(reports_config):
    submit(reports_config)
    assert RevisionStore(reports_config.state_dir).latest(DAY).owner_id == "owner"


def test_submission_for_a_foreign_owner_is_rejected(reports_config):
    with pytest.raises(SubmitRejected) as exc:
        submit_evening(reports_config, parse_evening_payload(payload_dict(owner_id="attacker")))
    assert exc.value.code == "owner_mismatch"


def test_submission_for_an_unfrozen_day_is_rejected(reports_config):
    body = payload_dict(day="2026-09-20", checklist=[])
    with pytest.raises(SubmitRejected) as exc:
        submit_evening(reports_config, parse_evening_payload(body))
    assert exc.value.code == "day_not_frozen"


def test_submission_with_ids_outside_the_freeze_is_rejected(reports_config):
    body = payload_dict(checklist=[{"task_id": "MAT-999", "done": True}])
    with pytest.raises(SubmitRejected) as exc:
        submit_evening(reports_config, parse_evening_payload(body))
    assert exc.value.code == "unknown_task_ids"


def test_state_records_the_revision_but_not_validation(reports_config):
    """Validating the evening and writing `daily/` is F4's job, not the server's."""
    submit(reports_config)
    state = load_state(StateTransaction(reports_config.state_dir).state_path)
    bucket = state.days[DAY.isoformat()]
    assert bucket.evening_revision == 1
    assert bucket.evening_validated is False


def test_latest_revision_response_shape(reports_config):
    empty = latest_revision_response(reports_config, DAY)
    assert empty == {"day": DAY.isoformat(), "revision": 0, "payload": None}
    submit(reports_config, notes="x")
    body = latest_revision_response(reports_config, DAY)
    assert body["revision"] == 1
    assert body["payload"]["notes"] == "x"
    # The page feeds this straight back into the form, so it must revalidate.
    assert parse_evening_payload(body["payload"]).notes == "x"


# --------------------------------------------------------------------------------------
# Concurrency and crash recovery
# --------------------------------------------------------------------------------------


def test_concurrent_identical_submissions_produce_one_job(reports_config):
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            submit(reports_config)
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(RevisionStore(reports_config.state_dir).history(DAY)) == 1
    assert len(Outbox(reports_config.state_dir).pending()) == 1


def _process_worker(state_dir: str, output_dir: str, wiki_dir: str) -> None:
    from swarm_reports.config import ReportsConfig
    from tests.conftest import WEIGHTS

    config = ReportsConfig(
        wiki_dir=Path(wiki_dir),
        state_dir=Path(state_dir),
        weights_path=WEIGHTS,
        output_dir=Path(output_dir),
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=None,
        public_origin="http://127.0.0.1:9",
    )
    submit_evening(config, parse_evening_payload(payload_dict()))


def test_transaction_is_atomic_across_processes(reports_config):
    args = (
        str(reports_config.state_dir),
        str(reports_config.output_dir),
        str(reports_config.wiki_dir),
    )
    procs = [multiprocessing.Process(target=_process_worker, args=args) for _ in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    assert len(RevisionStore(reports_config.state_dir).history(DAY)) == 1


def test_revisions_are_serialized_per_day(reports_config):
    """Different days must not block each other, the same day must."""
    from tests.conftest import freeze_day

    freeze_day(reports_config.state_dir, day=DAY.replace(day=15), ids=("MAT-1",))
    submit(reports_config)
    other = payload_dict(day="2026-09-15", checklist=[{"task_id": "MAT-1", "done": True}])
    outcome = submit_evening(reports_config, parse_evening_payload(other))
    assert outcome.revision == 1
    store = RevisionStore(reports_config.state_dir)
    assert store.latest(DAY).revision == 1
    assert store.latest("2026-09-15").revision == 1


def test_crash_between_revision_and_index_loses_nothing(reports_config, monkeypatch):
    """The index is written last, so an interrupted save simply is not visible yet."""
    store = RevisionStore(reports_config.state_dir)
    outbox = Outbox(reports_config.state_dir)

    def explode(*args, **kwargs):
        raise RuntimeError("power cut before the index landed")

    monkeypatch.setattr(RevisionStore, "commit_index", explode)
    with pytest.raises(RuntimeError):
        submit_evening(
            reports_config, parse_evening_payload(payload_dict()), store=store, outbox=outbox
        )
    assert store.latest(DAY) is None
    assert (store.day_dir(DAY) / "revisions" / "0001.json").is_file()

    monkeypatch.undo()
    outcome = submit_evening(
        reports_config, parse_evening_payload(payload_dict()), store=store, outbox=outbox
    )
    assert outcome.revision == 1
    assert outcome.changed is True
    assert len(outbox.pending()) == 1


def test_pending_outbox_survives_a_restart(reports_config):
    submit(reports_config)
    # A brand new Outbox object is exactly what a restarted process sees.
    reopened = Outbox(reports_config.state_dir)
    pending = reopened.pending()
    assert [job.revision for job in pending] == [1]

    dispatched: list[OutboxJob] = []
    worker = OutboxWorker(reopened, dispatched.append)
    assert worker.drain_once() == 1
    assert [job.revision for job in dispatched] == [1]
    assert reopened.pending() == []
    assert reopened.is_done(pending[0])


def test_only_one_claimer_wins(reports_config):
    submit(reports_config)
    outbox = Outbox(reports_config.state_dir)
    job = outbox.pending()[0]
    assert outbox.claim(job) is True
    assert outbox.claim(job) is False


def test_failed_dispatch_retries_without_duplicating_a_completed_effect(reports_config):
    submit(reports_config)
    outbox = Outbox(reports_config.state_dir, max_attempts=3)
    effects: list[int] = []
    attempts = {"n": 0}

    def flaky(job: OutboxJob) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("handler died")
        effects.append(job.revision)

    worker = OutboxWorker(outbox, flaky)
    assert worker.drain_once() == 0
    assert effects == []
    assert outbox.pending()[0].attempts == 1

    assert worker.drain_once() == 1
    assert effects == [1]

    # A third pass must not repeat the effect that already landed.
    outbox.enqueue(outbox.pending()[0] if outbox.pending() else _completed_job(outbox))
    assert worker.drain_once() == 0
    assert effects == [1]


def _completed_job(outbox: Outbox) -> OutboxJob:
    done = sorted((outbox.root / "done").glob("*.json"))
    return OutboxJob.from_json(json.loads(done[0].read_text(encoding="utf-8")))


def test_job_is_parked_after_max_attempts(reports_config):
    submit(reports_config)
    outbox = Outbox(reports_config.state_dir, max_attempts=2)

    def always_fails(job: OutboxJob) -> None:
        raise RuntimeError("nope")

    worker = OutboxWorker(outbox, always_fails)
    worker.drain_once()
    worker.drain_once()
    assert outbox.pending() == []
    assert list((outbox.root / "failed").glob("*.json"))


def test_missing_evening_handler_leaves_the_job_pending(reports_config):
    submit(reports_config)
    outbox = Outbox(reports_config.state_dir, max_attempts=2)
    worker = OutboxWorker(outbox)  # LeavePendingDispatcher by default
    # Polling before F4 exists must not park the day's evening in `failed/`, so "not
    # wired yet" does not consume the retry budget the way a real failure does.
    for _ in range(5):
        assert worker.drain_once() == 0
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0].attempts == 0
    assert "no evening handler configured" in pending[0].last_error
    assert not list((outbox.root / "failed").glob("*.json"))


def test_bounded_command_dispatcher_runs_without_a_shell(reports_config, tmp_path):
    import sys

    from swarm_reports.server.outbox import BoundedCommandDispatcher

    marker = tmp_path / "handler-ran.json"
    script = tmp_path / "handler.py"
    script.write_text(
        "import json,sys,pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    submit(reports_config)
    outbox = Outbox(reports_config.state_dir)
    dispatcher = BoundedCommandDispatcher(
        (sys.executable, str(script), str(marker)), timeout_seconds=30
    )
    assert OutboxWorker(outbox, dispatcher).drain_once() == 1
    assert json.loads(marker.read_text(encoding="utf-8"))["revision"] == 1


# --------------------------------------------------------------------------------------
# The copy-prompt CLI seam must land in exactly the same state as the POST.
# --------------------------------------------------------------------------------------


def test_cli_input_and_post_payload_are_equivalent(reports_config, tmp_path):
    from tests.conftest import write_payload

    path = write_payload(tmp_path / "copied.json", notes="do celular")
    cli_outcome = submit_from_file(reports_config, path)
    assert cli_outcome.revision == 1

    # The browser body is the same content plus transport metadata.
    posted = submit_evening(
        reports_config,
        parse_evening_payload(
            payload_dict(notes="do celular", client_saved_at="2026-09-14T22:10:00Z")
        ),
    )
    assert posted.changed is False
    assert posted.revision == 1
    assert posted.content_hash == cli_outcome.content_hash


def test_cli_input_rejects_a_day_mismatch(reports_config, tmp_path):
    from tests.conftest import write_payload

    path = write_payload(tmp_path / "copied.json")
    with pytest.raises(SubmitRejected) as exc:
        submit_from_file(reports_config, path, day=DAY.replace(day=15))
    assert exc.value.code == "day_mismatch"
