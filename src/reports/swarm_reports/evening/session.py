"""The night session: one stored revision in, durable effects out.

There is exactly one engine function, `run_evening_session`. `POST /evening` reaches it
through the outbox, `report evening --input` reaches it directly after submitting the
same canonical payload, and the outbox dispatcher reaches it with the stored revision
number. A second implementation of "what the night does" would drift from the first one
on precisely the day the owner needed them to agree.

Ordering, and why it is this one:

1. **Serialize per day.** A dedicated session lock, not the revision lock, so a phone
   still gets `saved-pending` in milliseconds while a git push is in flight.
2. **Refuse a stale revision.** Revision 1 arriving after revision 2 has been applied is
   dropped, because the owner's later answer is the true one.
3. **State, then vault, then publish.** `reports-state.json` is what the metric band
   reads, so the day counts as closed the moment the owner's answer is durable. The
   vault write and the pull request are separate effects with separate ledger rows: a
   pull request that failed to open is `failed` in the ledger and the next morning shows
   it, instead of a green line that never happened.
4. **Checkpoint after every effect.** A crash between the git commit and the checkpoint
   replays identical bytes, finds nothing staged and reuses the commit.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from swarm_reports.config import ReportsConfig
from swarm_reports.evening.ledger import (
    KIND_CONTEST,
    KIND_POST_NOTE,
    KIND_REFLECTION,
    KIND_SUGGESTION,
    KIND_TOMORROW_PLAN,
    KIND_WIKI_COMMIT,
    KIND_WIKI_PR,
    STATUS_ACKNOWLEDGED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REVIEW,
    LedgerRecord,
    LedgerStore,
    entry_id_for,
)
from swarm_reports.evening.reflection import Reflection, run_reflection
from swarm_reports.evening_schema import EveningPayload
from swarm_reports.metrics.daily import ChecklistItem, DailyNote
from swarm_reports.metrics.execution import (
    EveningSubmission,
    UnplannedCompletion,
    compute_completion,
    compute_scope_penalty,
)
from swarm_reports.metrics.res import load_res_weights
from swarm_reports.metrics.state import carryover_first_planned, mark_evening_validated
from swarm_reports.server.outbox import OutboxJob
from swarm_reports.server.revisions import RevisionStore
from swarm_reports.storage import StateTransaction, atomic_write_text
from swarm_reports.tiles import NO_DATA, completion_window, open_drift, res_window
from swarm_reports.wiki.night import NightEdits, PostUpdate, apply_night_edits
from swarm_reports.wiki.notes import TomorrowItem, UnplannedRecord
from swarm_reports.wiki.publish import (
    PublishFailed,
    PublishRequest,
    WikiPublisher,
    build_publisher,
)

SESSION_FILE = "session.json"
SESSION_LOCK = ".session.lock"
PLANS_DIR = "plans"

STATUS_APPLIED = "applied"
STATUS_ALREADY_APPLIED = "already-applied"
STATUS_SUPERSEDED = "superseded"
STATUS_REPUBLISHED = "republished"

PHASE_STATE = "state"
PHASE_WIKI = "wiki"
PHASE_PUBLISH = "publish"
PHASE_DONE = "completed"

PUBLISH_SKIPPED = "skipped"


class EveningRejected(ValueError):
    """The revision cannot be processed at all (wrong owner, day never frozen)."""


@dataclass
class RunRecord:
    revision: int
    phase: str = ""
    content_hash: str = ""
    wiki_branch: str | None = None
    wiki_commit: str | None = None
    publish_status: str = ""
    pull_request_url: str | None = None
    ledger_ids: list[str] = field(default_factory=list)
    finished_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "phase": self.phase,
            "content_hash": self.content_hash,
            "wiki_branch": self.wiki_branch,
            "wiki_commit": self.wiki_commit,
            "publish_status": self.publish_status,
            "pull_request_url": self.pull_request_url,
            "ledger_ids": list(self.ledger_ids),
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunRecord:
        return cls(
            revision=int(data.get("revision") or 0),
            phase=str(data.get("phase") or ""),
            content_hash=str(data.get("content_hash") or ""),
            wiki_branch=data.get("wiki_branch"),
            wiki_commit=data.get("wiki_commit"),
            publish_status=str(data.get("publish_status") or ""),
            pull_request_url=data.get("pull_request_url"),
            ledger_ids=[str(x) for x in (data.get("ledger_ids") or [])],
            finished_at=data.get("finished_at"),
        )


@dataclass
class SessionProgress:
    day: str
    applied_revision: int = 0
    runs: dict[str, RunRecord] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "applied_revision": self.applied_revision,
            "runs": {key: run.to_json() for key, run in self.runs.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionProgress:
        runs_raw = data.get("runs") or {}
        return cls(
            day=str(data.get("day") or ""),
            applied_revision=int(data.get("applied_revision") or 0),
            runs={
                str(key): RunRecord.from_json(value)
                for key, value in runs_raw.items()
                if isinstance(value, dict)
            },
        )


@dataclass(frozen=True)
class EveningOutcome:
    day: str
    revision: int
    status: str
    wiki_branch: str | None = None
    wiki_commit: str | None = None
    pull_request_url: str | None = None
    publish_status: str = ""
    ledger_ids: tuple[str, ...] = ()
    completion: float | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status in (STATUS_APPLIED, STATUS_REPUBLISHED)


def session_dir(state_dir: Path, day: date | str) -> Path:
    key = day if isinstance(day, str) else day.isoformat()
    return state_dir / "evening" / key


@contextmanager
def session_lock(state_dir: Path, day: date | str) -> Iterator[None]:
    directory = session_dir(state_dir, day)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / SESSION_LOCK, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_session(state_dir: Path, day: date | str) -> SessionProgress:
    key = day if isinstance(day, str) else day.isoformat()
    path = session_dir(state_dir, key) / SESSION_FILE
    if not path.exists():
        return SessionProgress(day=key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt evening session state at {path}: {exc.msg}") from exc
    progress = SessionProgress.from_json(data if isinstance(data, dict) else {})
    progress.day = key
    return progress


def save_session(state_dir: Path, progress: SessionProgress) -> None:
    atomic_write_text(
        session_dir(state_dir, progress.day) / SESSION_FILE,
        json.dumps(progress.to_json(), indent=2, sort_keys=True) + "\n",
    )


def run_evening_session(
    config: ReportsConfig,
    *,
    day: date,
    revision: int,
    expected_hash: str | None = None,
    store: RevisionStore | None = None,
    publisher: WikiPublisher | None = None,
    publish: bool = True,
    now: datetime | None = None,
) -> EveningOutcome:
    revisions = store or RevisionStore(config.state_dir)
    ledger = LedgerStore(config.state_dir)
    stamp = now or datetime.now(timezone.utc)
    key = day.isoformat()

    with session_lock(config.state_dir, day):
        progress = load_session(config.state_dir, day)

        if revision < progress.applied_revision:
            return EveningOutcome(
                day=key,
                revision=revision,
                status=STATUS_SUPERSEDED,
                detail=f"revision {progress.applied_revision} already applied for {key}",
            )

        previous = progress.runs.get(str(revision))
        if previous is not None and previous.phase == PHASE_DONE:
            if previous.publish_status != STATUS_FAILED or not publish:
                return EveningOutcome(
                    day=key,
                    revision=revision,
                    status=STATUS_ALREADY_APPLIED,
                    wiki_branch=previous.wiki_branch,
                    wiki_commit=previous.wiki_commit,
                    pull_request_url=previous.pull_request_url,
                    publish_status=previous.publish_status,
                    ledger_ids=tuple(previous.ledger_ids),
                )
            # The vault edit landed but the pull request did not. Retry just that.
            return _retry_publish(config, ledger, progress, previous, stamp, publisher)

        revision_record = revisions.read_revision(day, revision)
        if expected_hash and revision_record.content_hash != expected_hash:
            raise EveningRejected(
                f"revision {revision} hash {revision_record.content_hash} != {expected_hash}"
            )
        if revision_record.owner_id != config.owner_id:
            raise EveningRejected("stored revision belongs to another owner")
        payload = revision_record.payload()
        if payload.day != day:
            raise EveningRejected("stored revision day does not match the job day")

        run = RunRecord(revision=revision, content_hash=revision_record.content_hash)
        plan = _build_plan(config, payload, revision_record.content_hash)

        # 1. State first: the owner's answer is the metric source of truth.
        with StateTransaction(config.state_dir).locked() as state:
            mark_evening_validated(
                state,
                day,
                validated_ids=plan.done_ids,
                unplanned_ids=[item.task_id for item in plan.unplanned],
                revision=revision,
            )
        run.phase = PHASE_STATE
        progress.runs[str(revision)] = run
        save_session(config.state_dir, progress)

        evolution, completion = _evolution_lines(config, day, plan)
        reflection = _reflect(config, day, plan, evolution)

        # 2. Vault edit on an isolated branch.
        night = apply_night_edits(
            config.wiki_dir,
            NightEdits(
                day=day,
                revision=revision,
                done_ids=plan.done_ids,
                classifications=plan.classifications,
                unplanned=plan.unplanned,
                evolution_lines=evolution,
                tomorrow=plan.tomorrow,
                tomorrow_day=day + timedelta(days=1),
                posts=plan.posts,
            ),
            worktree_parent=config.state_dir / "wiki-worktrees",
            local_only=config.evening.wiki_local_only,
            remote=config.evening.publish.remote,
            lint_command=config.evening.lint_command,
        )
        run.wiki_branch = night.branch
        run.wiki_commit = night.commit_sha
        run.phase = PHASE_WIKI
        save_session(config.state_dir, progress)

        _write_proposed_plan(config, day + timedelta(days=1), plan)

        ledger_ids = _record_ledger(
            ledger,
            day=day,
            revision=revision,
            content_hash=revision_record.content_hash,
            night=night,
            plan=plan,
            reflection=reflection,
            now=stamp,
        )
        run.ledger_ids = ledger_ids

        # 3. Publish last, and record what actually happened.
        pr_entry = entry_id_for(day, revision, KIND_WIKI_PR, night.branch)
        publish_status, pr_url, detail = _publish(
            config,
            night=night,
            day=day,
            revision=revision,
            publisher=publisher,
            publish=publish,
            lint_ok=night.lint_ok,
        )
        run.publish_status = publish_status
        run.pull_request_url = pr_url
        if publish_status == STATUS_FAILED:
            ledger.set_status(day, pr_entry, STATUS_FAILED, detail=detail, now=stamp)
        elif publish_status == PUBLISH_SKIPPED:
            ledger.set_status(day, pr_entry, STATUS_PENDING, detail=detail, now=stamp)
        else:
            ledger.set_status(day, pr_entry, STATUS_COMPLETED, link=pr_url, detail=detail, now=stamp)

        run.phase = PHASE_DONE
        run.finished_at = stamp.isoformat()
        progress.applied_revision = max(progress.applied_revision, revision)
        save_session(config.state_dir, progress)

        return EveningOutcome(
            day=key,
            revision=revision,
            status=STATUS_APPLIED,
            wiki_branch=night.branch,
            wiki_commit=night.commit_sha,
            pull_request_url=pr_url,
            publish_status=publish_status,
            ledger_ids=tuple(ledger_ids),
            completion=completion,
            detail=detail,
        )


def handle_outbox_job(
    config: ReportsConfig,
    job: OutboxJob,
    *,
    publisher: WikiPublisher | None = None,
    publish: bool = True,
) -> EveningOutcome:
    """Dispatcher entry point. Reads the revision the job names, nothing else."""
    return run_evening_session(
        config,
        day=date.fromisoformat(job.day),
        revision=job.revision,
        expected_hash=job.content_hash or None,
        publisher=publisher,
        publish=publish,
    )


def make_dispatcher(
    config: ReportsConfig,
    *,
    publisher: WikiPublisher | None = None,
    publish: bool = True,
):
    """In-process handler for `build_server(dispatcher=...)` and `serve`."""

    def dispatch(job: OutboxJob) -> None:
        handle_outbox_job(config, job, publisher=publisher, publish=publish)

    return dispatch


# --------------------------------------------------------------------- computation


@dataclass(frozen=True)
class _DayPlan:
    payload: EveningPayload
    frozen_ids: list[str]
    done_ids: set[str]
    classifications: dict[str, str | None]
    unplanned: list[UnplannedRecord]
    posts: list[PostUpdate]
    tomorrow: list[TomorrowItem]
    open_p0: list[str]
    contests: list[UnplannedRecord]
    scope_penalty: int
    content_hash: str


def _build_plan(
    config: ReportsConfig,
    payload: EveningPayload,
    content_hash: str,
) -> _DayPlan:
    day = payload.day
    state = StateTransaction(config.state_dir)
    from swarm_reports.metrics.state import load_state

    current = load_state(state.state_path)
    bucket = current.days.get(day.isoformat())
    if bucket is None or not bucket.morning_freeze_applied:
        raise EveningRejected("no frozen morning exists for that date")

    frozen_ids = bucket.frozen_ids()
    unknown = [item.task_id for item in payload.checklist if item.task_id not in set(frozen_ids)]
    if unknown:
        raise EveningRejected(f"checklist refers to ids outside the freeze: {unknown[:3]}")

    done_ids = {item.task_id for item in payload.checklist if item.done}
    classifications = {
        item.task_id: item.classification
        for item in payload.checklist
        if item.classification is not None
    }

    unplanned = [
        UnplannedRecord(
            task_id=item.task_id,
            text=item.text,
            done=item.done,
            classification=item.classification,
            first_planned=carryover_first_planned(current, item.task_id, day),
        )
        for item in payload.unplanned
    ]

    open_p0 = sorted(
        item.task_id
        for item in bucket.frozen_checklist
        if item.is_p0 and item.task_id not in done_ids
    )

    # A contest is a deterministic observation, not an opinion: the owner called an
    # out-of-plan item an opportunity on a day that still had a P0 open. It is offered
    # as a question in the next morning and never rewrites what the owner chose.
    contests = [
        item
        for item in unplanned
        if open_p0 and item.done and item.classification == "oportunidade"
    ]

    scope_penalty = compute_scope_penalty(
        EveningSubmission(
            day=day,
            validated_complete_ids=frozenset(done_ids),
            unplanned_completed=tuple(
                UnplannedCompletion(
                    task_id=item.task_id,
                    classification=item.classification,
                    completed_on=day,
                )
                for item in unplanned
                if item.done
            ),
        ),
        frozenset(open_p0),
    )

    posts = [
        PostUpdate(
            url=post.url,
            platform=post.platform,
            guided=post.guided,
            checkpoint=post.checkpoint,
            metrics=post.metrics.to_json(),
            posted_on=day,
        )
        for post in payload.posts
    ]

    carried = [
        item
        for item in bucket.frozen_checklist
        if item.task_id not in done_ids
    ]
    carried.sort(key=lambda item: (item.first_planned, item.task_id))
    p0_budget = config.evening.max_p0_tomorrow
    tomorrow: list[TomorrowItem] = []
    for item in carried:
        keep_p0 = item.is_p0 and p0_budget > 0
        if keep_p0:
            p0_budget -= 1
        tomorrow.append(
            TomorrowItem(task_id=item.task_id, text=item.text, is_p0=keep_p0)
        )

    return _DayPlan(
        payload=payload,
        frozen_ids=frozen_ids,
        done_ids=done_ids,
        classifications=classifications,
        unplanned=unplanned,
        posts=posts,
        tomorrow=tomorrow,
        open_p0=open_p0,
        contests=contests,
        scope_penalty=scope_penalty,
        content_hash=content_hash,
    )


def _evolution_lines(
    config: ReportsConfig,
    day: date,
    plan: _DayPlan,
) -> tuple[list[str], float | None]:
    """Every line here comes from the F1 formulas. None of it is written by a model."""
    note = DailyNote(day=day, frontmatter={}, items=[], evening_validated=True)
    completion = compute_completion(
        note,
        EveningSubmission(day=day, validated_complete_ids=frozenset(plan.done_ids)),
        frozen_snapshot_ids=plan.frozen_ids,
    )

    from swarm_reports.metrics.state import load_state

    state = load_state(StateTransaction(config.state_dir).state_path)
    window = completion_window(state, day + timedelta(days=1))
    drift = open_drift(state, day)

    done_count = len([task_id for task_id in plan.frozen_ids if task_id in plan.done_ids])
    lines = [
        f"- Conclusão: {_percent(completion)} ({done_count}/{len(plan.frozen_ids)} do "
        f"checklist congelado)",
        f"- 7d conclusão: {_percent(window.average)} "
        f"({window.closed_days}/{window.planned_days} dias fechados)",
        f"- Drift aberto: {drift.total_days} d em {drift.open_items} itens",
    ]
    if plan.open_p0:
        lines.append(f"- P0 em aberto: {len(plan.open_p0)}")
    if plan.unplanned:
        done_unplanned = sum(1 for item in plan.unplanned if item.done)
        lines.append(
            f"- Fora do plano: {len(plan.unplanned)} itens ({done_unplanned} concluídos, "
            f"penalidade de escopo {plan.scope_penalty})"
        )
    lines.append(_res_line(config, day))
    return lines, completion


def _res_line(config: ReportsConfig, day: date) -> str:
    try:
        weights = load_res_weights(config.weights_path)
    except (OSError, ValueError, KeyError):
        return f"- RES 7d: {NO_DATA} (pesos)"
    window = res_window(config.wiki_dir / "brand" / "posts", weights, day)
    return (
        f"- RES 7d: guiado {_res(window.guided_average)} "
        f"({window.guided_per_week:.1f}/sem) · espontâneo {_res(window.spontaneous_average)} "
        f"({window.spontaneous_per_week:.1f}/sem)"
    )


def _reflect(
    config: ReportsConfig,
    day: date,
    plan: _DayPlan,
    evolution: list[str],
) -> Reflection:
    if config.evening.reflection is None:
        return Reflection()
    request = {
        "day": day.isoformat(),
        "metrics": list(evolution),
        "notes": plan.payload.notes,
        "open_p0": list(plan.open_p0),
        "unplanned": [
            {"text": item.text, "done": item.done, "classification": item.classification}
            for item in plan.unplanned
        ],
    }
    return run_reflection(config.evening.reflection, request)


def _write_proposed_plan(config: ReportsConfig, tomorrow: date, plan: _DayPlan) -> None:
    """A `MorningPlan`-shaped proposal the next morning can read with `--plan`.

    It is an input to the morning, never a freeze: the morning refreshes it against live
    Linear and Calendar before anything becomes a denominator.
    """
    payload = {
        "day": tomorrow.isoformat(),
        "checklist": [
            {
                "task_id": item.task_id,
                "text": item.text,
                "is_p0": item.is_p0,
                "source_pointer": item.source_pointer,
            }
            for item in plan.tomorrow
        ],
        "sources": [
            {"kind": "evening", "pointer": f"revision:{plan.content_hash[:12]}", "status": "ok"}
        ],
        "confirmed_empty": not plan.tomorrow,
    }
    atomic_write_text(
        config.state_dir / PLANS_DIR / f"{tomorrow.isoformat()}.proposed.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


# ------------------------------------------------------------------------- effects


def _record_ledger(
    ledger: LedgerStore,
    *,
    day: date,
    revision: int,
    content_hash: str,
    night,
    plan: _DayPlan,
    reflection: Reflection,
    now: datetime,
) -> list[str]:
    provenance = {
        "source": "evening-form",
        "day": day.isoformat(),
        "revision": revision,
        "content_hash": content_hash,
    }
    ids: list[str] = []

    def add(kind: str, target: str, status: str, summary: str, *, detail: str = "", link=None):
        entry_id = entry_id_for(day, revision, kind, target)
        ledger.record(
            LedgerRecord(
                entry_id=entry_id,
                day=day.isoformat(),
                revision=revision,
                kind=kind,
                target=target,
                status=status,
                summary=summary,
                provenance=provenance,
                link=link,
                detail=detail,
            ),
            now=now,
        )
        ids.append(entry_id)
        return entry_id

    lint_note = ""
    if night.lint_ok is not None:
        lint_note = f"lint {'verde' if night.lint_ok else 'vermelho'}: {night.lint_detail}"
    add(
        KIND_WIKI_COMMIT,
        f"daily/{day.isoformat()}.md",
        STATUS_COMPLETED,
        f"writeback da noite em {night.branch} ({night.commit_sha[:12]})",
        detail=lint_note,
    )
    # Written `pending` before the attempt; `_publish` decides what it becomes.
    add(
        KIND_WIKI_PR,
        night.branch,
        STATUS_PENDING,
        f"PR do writeback {day.isoformat()} (base main, merge é do dono até F5)",
    )
    for rel in night.created_posts:
        add(KIND_POST_NOTE, rel, STATUS_COMPLETED, f"nota de post espontânea criada: {rel}")
    if plan.tomorrow:
        p0 = sum(1 for item in plan.tomorrow if item.is_p0)
        add(
            KIND_TOMORROW_PLAN,
            (day + timedelta(days=1)).isoformat(),
            STATUS_COMPLETED,
            f"plano de amanhã proposto: {len(plan.tomorrow)} itens, {p0} P0 "
            f"(a manhã ainda congela o plano real)",
        )
    for item in plan.contests:
        add(
            KIND_CONTEST,
            item.task_id,
            STATUS_REVIEW,
            f"'{item.text}' foi classificado como oportunidade com "
            f"{len(plan.open_p0)} P0 em aberto — contestar?",
            detail="sugestão; a classificação do dono continua valendo",
        )
    for checkpoint_id in plan.payload.pending_checkpoint_ids:
        add(
            KIND_POST_NOTE,
            checkpoint_id,
            STATUS_PENDING,
            f"checkpoint {checkpoint_id} sem números; segue pendente",
        )
    for action in plan.payload.review_actions:
        # Approving a draft marks it ready. It is never published, sent or posted here.
        add(
            "draft-review",
            action.draft_id,
            STATUS_ACKNOWLEDGED if action.action != "reject" else STATUS_COMPLETED,
            f"draft {action.draft_id}: {action.action} (nada foi publicado)",
        )
    if reflection.text:
        add(KIND_REFLECTION, day.isoformat(), STATUS_COMPLETED, reflection.text[:400])
    for suggestion in reflection.suggestions:
        add(
            KIND_SUGGESTION,
            f"{suggestion.kind}:{suggestion.target}" if suggestion.target else suggestion.kind,
            STATUS_REVIEW,
            suggestion.summary,
            detail=suggestion.detail,
        )

    for entry in plan.payload.ledger_classifications:
        if entry.classification is None:
            continue
        for offset in range(0, 31):
            target_day = day - timedelta(days=offset)
            if ledger.classify(target_day, entry.entry_id, entry.classification, now=now):
                break

    return ids


def _publish(
    config: ReportsConfig,
    *,
    night,
    day: date,
    revision: int,
    publisher: WikiPublisher | None,
    publish: bool,
    lint_ok: bool | None,
) -> tuple[str, str | None, str]:
    if not publish:
        return PUBLISH_SKIPPED, None, "publicação desativada nesta execução (--no-publish)"
    transport = publisher or build_publisher(
        config.evening.publish, config.state_dir / "wiki-publish-queue"
    )
    if transport is None:
        return PUBLISH_SKIPPED, None, "evening.publish.mode = none"

    request = PublishRequest(
        wiki_dir=config.wiki_dir,
        worktree=night.worktree,
        branch=night.branch,
        day=day,
        commit_sha=night.commit_sha,
        title=f"reports: evening writeback {day.isoformat()}",
        body=_pull_request_body(day, revision, night, lint_ok),
    )
    try:
        outcome = transport.publish(request)
    except PublishFailed as exc:
        return STATUS_FAILED, None, str(exc)[:500]
    except Exception as exc:  # noqa: BLE001 - any transport error is a failed effect
        return STATUS_FAILED, None, f"{type(exc).__name__}: {exc}"[:500]
    if outcome.pull_request_url:
        _maybe_apply_pr_autonomy(config, outcome.pull_request_url, lint_ok)
        return STATUS_COMPLETED, outcome.pull_request_url, outcome.detail
    if outcome.pushed:
        return STATUS_PENDING, None, outcome.detail
    return STATUS_PENDING, None, outcome.detail


def _maybe_apply_pr_autonomy(config: ReportsConfig, pr_url: str, lint_ok: bool | None) -> None:
    if config.dispatch_policy_path is None:
        return
    try:
        from swarm_reports.dispatch.evening_autonomy import process_evening_pr
        from swarm_reports.dispatch.gh_cli import GhCliTransport
        from swarm_reports.dispatch.policy_config import load_dispatch_policy

        policy = load_dispatch_policy(config.dispatch_policy_path)
        process_evening_pr(
            policy=policy,
            state_dir=config.state_dir,
            pr_url=pr_url,
            lint_ok=lint_ok,
            gh=GhCliTransport(),
        )
    except Exception:  # noqa: BLE001 - autonomy is best-effort; ledger already records the PR
        return


def _pull_request_body(day: date, revision: int, night, lint_ok: bool | None) -> str:
    lint_line = {
        None: "Lint do vault não foi executado nesta configuração.",
        True: "Lint do vault verde.",
        False: f"Lint do vault vermelho: {night.lint_detail}",
    }[lint_ok]
    return (
        f"Writeback da noite de {day.isoformat()} (revisão {revision}).\n\n"
        f"Arquivos: {', '.join(night.changed_paths)}\n"
        f"{lint_line}\n\n"
        "## Decisões que merecem pergunta\n\n"
        "(preenchido pelo mecanismo quando a política F5 está configurada)\n"
    )


def _retry_publish(
    config: ReportsConfig,
    ledger: LedgerStore,
    progress: SessionProgress,
    run: RunRecord,
    stamp: datetime,
    publisher: WikiPublisher | None,
) -> EveningOutcome:
    """The vault commit already landed; only the pull request is missing."""
    from swarm_reports.wiki.night import night_target

    day = date.fromisoformat(progress.day)
    _branch, worktree = night_target(config.wiki_dir, day, config.state_dir / "wiki-worktrees")
    branch = run.wiki_branch or _branch
    night = _CommittedNight(
        branch=branch,
        commit_sha=run.wiki_commit or "",
        worktree=worktree,
        changed_paths=(f"daily/{progress.day}.md",),
        lint_ok=None,
        lint_detail="",
    )
    status, url, detail = _publish(
        config,
        night=night,
        day=day,
        revision=run.revision,
        publisher=publisher,
        publish=True,
        lint_ok=None,
    )
    run.publish_status = status
    run.pull_request_url = url
    entry_id = entry_id_for(day, run.revision, KIND_WIKI_PR, branch)
    ledger.set_status(
        day,
        entry_id,
        STATUS_PENDING if status == PUBLISH_SKIPPED else status,
        link=url,
        detail=detail,
        now=stamp,
    )
    save_session(config.state_dir, progress)
    return EveningOutcome(
        day=progress.day,
        revision=run.revision,
        status=STATUS_REPUBLISHED,
        wiki_branch=branch,
        wiki_commit=run.wiki_commit,
        pull_request_url=url,
        publish_status=status,
        ledger_ids=tuple(run.ledger_ids),
        detail=detail,
    )


@dataclass(frozen=True)
class _CommittedNight:
    branch: str
    commit_sha: str
    worktree: Path
    changed_paths: tuple[str, ...]
    lint_ok: bool | None
    lint_detail: str


def _percent(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"{value * 100:.0f}%"


def _res(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"{value:.1f}"
