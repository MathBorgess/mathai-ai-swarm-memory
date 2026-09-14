"""The one path an evening submission takes.

`POST /evening` and `report evening --input file.json` both land here, so the copy-prompt
fallback — the thing the owner uses when the VPS is down — produces byte-identical state
to the form. Two code paths would eventually disagree, and the disagreement would only
show up on the day the server was already broken.

Identity is never read from the body. `owner_id` in the payload is a consistency check;
the value that gets stored comes from the config, which is fed by Cloudflare Access or a
loopback-only bind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from swarm_reports.config import ReportsConfig
from swarm_reports.evening_schema import EveningPayload, parse_evening_payload
from swarm_reports.metrics.state import load_state
from swarm_reports.server.outbox import Outbox, OutboxJob
from swarm_reports.server.revisions import RevisionStore
from swarm_reports.storage import StateTransaction


class SubmitRejected(ValueError):
    """Client error: a stable code the HTTP layer can map to 400 without leaking detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SubmitOutcome:
    day: str
    revision: int
    changed: bool
    content_hash: str


def submit_evening(
    config: ReportsConfig,
    payload: EveningPayload,
    *,
    store: RevisionStore | None = None,
    outbox: Outbox | None = None,
    now: datetime | None = None,
) -> SubmitOutcome:
    if payload.owner_id != config.owner_id:
        # Not authorization — that already happened. This catches a page rendered for a
        # different owner, which would otherwise write into the wrong day bucket.
        raise SubmitRejected("owner_mismatch", "payload owner_id does not match this server")

    state = load_state(StateTransaction(config.state_dir).state_path)
    bucket = state.days.get(payload.day.isoformat())
    if bucket is None or not bucket.morning_freeze_applied:
        raise SubmitRejected("day_not_frozen", "no frozen morning exists for that date")

    frozen_ids = set(bucket.frozen_ids())
    unknown = [item.task_id for item in payload.checklist if item.task_id not in frozen_ids]
    if unknown:
        raise SubmitRejected("unknown_task_ids", "checklist refers to ids outside the freeze")

    revision_store = store or RevisionStore(config.state_dir)
    job_outbox = outbox or Outbox(config.state_dir)
    stored = payload.with_owner(config.owner_id)
    stamp = (now or datetime.now(timezone.utc)).isoformat()

    with revision_store.day_lock(stored.day):
        outcome = revision_store.save(stored, now=now)
        if outcome.changed:
            # Revision file, then job, then index: see revisions.py for why the order is
            # what makes a crash between "saved" and "callback" recoverable.
            job_outbox.enqueue(
                OutboxJob(
                    day=stored.day.isoformat(),
                    revision=outcome.revision,
                    content_hash=outcome.content_hash,
                    enqueued_at=stamp,
                )
            )
            revision_store.commit_index(
                stored.day, outcome.revision, outcome.content_hash, stamp
            )

    if outcome.changed:
        with StateTransaction(config.state_dir).locked() as live:
            # The revision exists; validating it and updating `daily/` is F4's job, so
            # `evening_validated` stays false and the metric band still says so.
            live.get_day(stored.day).evening_revision = outcome.revision

    return SubmitOutcome(
        day=stored.day.isoformat(),
        revision=outcome.revision,
        changed=outcome.changed,
        content_hash=outcome.content_hash,
    )


def submit_from_file(
    config: ReportsConfig,
    path: Path,
    *,
    day: date | None = None,
) -> SubmitOutcome:
    """The copy-prompt seam: the same JSON the browser would have POSTed."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    payload = parse_evening_payload(raw)
    if day is not None and payload.day != day:
        raise SubmitRejected("day_mismatch", "--date does not match the payload day")
    return submit_evening(config, payload)


def latest_revision_response(config: ReportsConfig, day: date) -> dict[str, object]:
    """Body for `GET /evening/revision?day=...`, shaped for the page's restore logic."""
    store = RevisionStore(config.state_dir)
    latest = store.latest(day)
    if latest is None:
        return {"day": day.isoformat(), "revision": 0, "payload": None}
    return {
        "day": day.isoformat(),
        "revision": latest.revision,
        "content_hash": latest.content_hash,
        "payload": latest.content,
    }
