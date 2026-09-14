"""Morning report orchestration.

The run has two clearly separated halves, and conflating them was the F2 bug:

**Dispatch, exactly once per day and durable.** Reading Linear/Calendar or invoking the
planner provider, choosing the P0 subset, freezing the checklist, committing it to the
wiki. Re-running the morning must not touch any of this, and in particular must not
re-invoke the provider — the previous version called the planner again on the duplicate
path *and then returned without rendering*, so a rerun both burned quota and produced
nothing.

**Refresh, every single run.** The agenda, "since the last round", the ledger and the
metric band all change during the day, and the HTML is regenerated from the *frozen*
checklist snapshot even when the incoming plan now says something different.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from swarm_reports.config import ReportsConfig
from swarm_reports.evening_schema import (
    SCHEMA_VERSION,
    EveningChecklistItem,
    EveningLedgerClassification,
    EveningPayload,
)
from swarm_reports.metrics.state import (
    FrozenItem,
    ReportsState,
    apply_morning_freeze,
    carryover_first_planned,
)
from swarm_reports.plan import AgendaEntry, MorningPlan, load_plan_file
from swarm_reports.providers import run_planner_provider
from swarm_reports.rendering.html import MorningViewModel, render_morning_html
from swarm_reports.sources import PlannerSources, UnavailableSources, build_plan_from_sources
from swarm_reports.storage import (
    PHASE_COMPLETED,
    PHASE_FROZEN,
    PHASE_PLANNED,
    MorningProgress,
    StateTransaction,
    atomic_write_text,
    day_run_lock,
    load_progress,
    record_morning_complete,
    save_progress,
)
from swarm_reports.tiles import build_metric_tiles
from swarm_reports.wiki.freeze import apply_wiki_freeze
from swarm_reports.wiki.publish import QueuedPublisher, WikiPublisher


@dataclass(frozen=True)
class MorningResult:
    html_path: Path
    plan: MorningPlan
    freeze_applied: bool
    run_id: str
    #: The checklist was already frozen; the HTML was still regenerated.
    freeze_reused: bool = False
    planner_dispatched: bool = False
    freeze_commit: str | None = None
    freeze_branch: str | None = None
    phase: str = PHASE_COMPLETED

    @property
    def skipped_duplicate(self) -> bool:
        return self.freeze_reused


def datetime_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def _parse_day(value: str | None, tz_name: str) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime_now(tz_name).date()


def html_output_path(config: ReportsConfig, day: date) -> Path:
    return config.output_dir / f"{day.isoformat()}.html"


def _plan_to_frozen_items(plan: MorningPlan, state: ReportsState) -> list[FrozenItem]:
    """Freeze the **whole** checklist; `is_p0` marks the (max 3) selected subset.

    Freezing only `p0_items` silently dropped every ordinary item, which then never
    appeared in the denominator or in the evening form.
    """
    items: list[FrozenItem] = []
    for entry in plan.checklist:
        task_id = str(entry["task_id"])
        declared = date.fromisoformat(str(entry.get("first_planned") or plan.day.isoformat()))
        items.append(
            FrozenItem(
                task_id=task_id,
                text=str(entry.get("text") or ""),
                # Carryover wins: an item that slipped from Monday keeps Monday's date.
                first_planned=carryover_first_planned(state, task_id, declared),
                is_p0=bool(entry.get("is_p0")),
            )
        )
    return items


def _with_real_ledger(plan: MorningPlan, state_dir: Path, day: date) -> MorningPlan:
    """Replace the plan's declared ledger with what the night actually recorded.

    The planner can only guess at what the mechanism did overnight; the ledger knows.
    Plan-declared entries are kept as a fallback for ids the ledger has never seen, so a
    skill that wants to surface something extra still can.
    """
    from swarm_reports.evening.ledger import morning_view

    recorded, pending = morning_view(state_dir, day)
    known = {entry.entry_id for entry in recorded}
    plan.ledger = recorded + [e for e in plan.ledger if e.entry_id not in known]
    seen = {draft.draft_id for draft in plan.review_drafts}
    plan.review_drafts = plan.review_drafts + [d for d in pending if d.draft_id not in seen]
    return plan


def _plan_with_frozen_checklist(
    plan: MorningPlan,
    frozen: list[FrozenItem],
    *,
    agenda: list[AgendaEntry] | None = None,
) -> MorningPlan:
    """Render from the frozen snapshot, keeping today's refreshed context."""
    checklist = [
        {
            "task_id": item.task_id,
            "text": item.text,
            "first_planned": item.first_planned.isoformat(),
            "is_p0": item.is_p0,
            "source_pointer": "",
        }
        for item in frozen
    ]
    return MorningPlan(
        day=plan.day,
        checklist=checklist,
        handoffs=plan.handoffs,
        sources=plan.sources,
        agenda=list(agenda) if agenda is not None else plan.agenda,
        review_drafts=plan.review_drafts,
        ledger=plan.ledger,
        lesson=plan.lesson,
        optional_post_draft=plan.optional_post_draft,
        discovery_placeholder=plan.discovery_placeholder,
        confirmed_empty=plan.confirmed_empty,
    )


def _evening_seed(plan: MorningPlan, owner_id: str) -> EveningPayload:
    """Seed the form from the full frozen checklist, not only the P0 subset."""
    return EveningPayload(
        schema_version=SCHEMA_VERSION,
        day=plan.day,
        owner_id=owner_id,
        checklist=[
            EveningChecklistItem(task_id=str(item["task_id"]), done=False)
            for item in plan.checklist
        ],
        ledger_classifications=[
            EveningLedgerClassification(entry_id=entry.entry_id) for entry in plan.ledger
        ],
    )


def _dispatch_planner(
    config: ReportsConfig,
    report_day: date,
    plan_path: Path | None,
    sources: PlannerSources | None,
    progress: MorningProgress,
    state_dir: Path,
) -> tuple[MorningPlan, bool]:
    """Produce the plan once. Returns `(plan, dispatched_now)`.

    A cached raw provider payload is re-validated instead of re-invoked: after the
    subprocess has run we no longer know what it did, so relaunching it is unsafe.
    """
    # An explicit `--plan` file is a read-only input with no cost, so a rerun always
    # picks up a refreshed one. The frozen checklist still wins over whatever it says.
    if plan_path is not None:
        plan = load_plan_file(plan_path)
        if plan.day != report_day:
            raise ValueError("plan day does not match --date")
        return plan, False

    if progress.plan_json is not None:
        return MorningPlan.from_json(progress.plan_json), False

    if progress.provider_raw is not None:
        plan = MorningPlan.from_json(progress.provider_raw)
        if plan.day != report_day:
            raise ValueError("cached planner output does not match the requested day")
        return plan, False

    if config.planner_provider is not None:
        result = run_planner_provider(
            config.planner_provider,
            day=report_day,
            wiki_dir=config.wiki_dir,
        )
        # Checkpoint the raw output before validating it or touching the wiki.
        progress.provider_raw = result.raw
        save_progress(state_dir, progress)
        return result.plan, True

    return build_plan_from_sources(report_day, sources or UnavailableSources()), False


def _refresh_agenda(
    report_day: date,
    sources: PlannerSources | None,
    fallback: list[AgendaEntry],
) -> list[AgendaEntry]:
    """Read-only agenda refresh. Never re-invokes the planner provider."""
    if sources is None:
        return fallback
    try:
        candidates, ref = sources.calendar_candidates(report_day)
    except Exception:  # noqa: BLE001 - a broken calendar must not block the report
        return fallback
    if not ref.confirms_empty:
        return fallback
    return [
        AgendaEntry(
            title=item.text,
            when=item.deadline.isoformat() if item.deadline else "",
            source_pointer=item.source_pointer,
        )
        for item in candidates
    ]


def run_morning(
    config: ReportsConfig,
    *,
    day: date | None = None,
    plan_path: Path | None = None,
    sources: PlannerSources | None = None,
    dry_run: bool = False,
    replay: bool = False,
    run_id: str | None = None,
    publisher: WikiPublisher | None = None,
    local_only_wiki: bool = False,
    force_replan: bool = False,
) -> MorningResult:
    report_day = day or _parse_day(None, config.timezone)
    rid = run_id or str(uuid.uuid4())
    config.output_dir.mkdir(parents=True, exist_ok=True)

    if dry_run or replay:
        return _render_only(config, report_day, plan_path, sources, rid)

    with day_run_lock(config.state_dir, report_day.isoformat()):
        progress = load_progress(config.state_dir, report_day)
        progress.attempts += 1
        progress.run_id = rid
        progress.last_error = None
        if force_replan:
            progress.provider_raw = None
            progress.plan_json = None

        try:
            plan, dispatched = _dispatch_planner(
                config, report_day, plan_path, sources, progress, config.state_dir
            )
            blocker = plan.freeze_blocker()
            if blocker:
                # Refusing here is the point: freezing is irreversible for the day.
                raise ValueError(f"refusing to freeze: {blocker}")
            progress.plan_json = plan.to_json()
            progress.enter(PHASE_PLANNED)
            save_progress(config.state_dir, progress)

            freeze_applied = False
            freeze_reused = False
            txn = StateTransaction(config.state_dir)
            with txn.locked() as state:
                bucket = state.get_day(report_day)
                if bucket.morning_freeze_applied:
                    frozen_items = list(bucket.frozen_checklist)
                    freeze_reused = True
                    progress.freeze_commit = bucket.frozen_at_commit or progress.freeze_commit
                    progress.freeze_snapshot = bucket.frozen_at_snapshot or progress.freeze_snapshot
                else:
                    items = _plan_to_frozen_items(plan, state)
                    freeze_result = apply_wiki_freeze(
                        config.wiki_dir,
                        report_day,
                        items,
                        timezone=config.timezone,
                        worktree_parent=config.state_dir / "wiki-worktrees",
                        local_only=local_only_wiki,
                        known_commit=progress.freeze_commit,
                        known_snapshot=progress.freeze_snapshot,
                        publisher=publisher
                        or QueuedPublisher(config.state_dir / "wiki-publish-queue"),
                    )
                    apply_morning_freeze(
                        state,
                        report_day,
                        items,
                        commit=freeze_result.commit_sha,
                        snapshot=freeze_result.snapshot,
                    )
                    frozen_items = list(state.get_day(report_day).frozen_checklist)
                    freeze_applied = freeze_result.applied
                    progress.freeze_branch = freeze_result.branch
                    progress.freeze_commit = freeze_result.commit_sha
                    progress.freeze_snapshot = freeze_result.snapshot
                validated_ids = set(bucket.evening_validated_ids)
                metrics = build_metric_tiles(
                    state,
                    report_day,
                    wiki_dir=config.wiki_dir,
                    weights_path=config.weights_path,
                )
            progress.enter(PHASE_FROZEN)
            save_progress(config.state_dir, progress)

            render_plan = _with_real_ledger(
                _plan_with_frozen_checklist(
                    plan,
                    frozen_items,
                    agenda=_refresh_agenda(report_day, sources, plan.agenda),
                ),
                config.state_dir,
                report_day,
            )
            html = render_morning_html(
                MorningViewModel(
                    day=report_day,
                    owner_id=config.owner_id,
                    metrics=metrics,
                    plan=render_plan,
                    evening_seed=_evening_seed(render_plan, config.owner_id),
                    done_task_ids=sorted(validated_ids),
                    post_url=config.evening_post_url,
                    revision_url=config.evening_revision_url(report_day),
                )
            )
            html_path = html_output_path(config, report_day)
            atomic_write_text(html_path, html, mode=0o640)

            progress.html_path = str(html_path)
            progress.completed_at = datetime_now(config.timezone).isoformat()
            progress.enter(PHASE_COMPLETED)
            save_progress(config.state_dir, progress)
            record_morning_complete(config.state_dir, report_day.isoformat(), rid)

            return MorningResult(
                html_path=html_path,
                plan=render_plan,
                freeze_applied=freeze_applied,
                run_id=rid,
                freeze_reused=freeze_reused,
                planner_dispatched=dispatched,
                freeze_commit=progress.freeze_commit,
                freeze_branch=progress.freeze_branch,
                phase=progress.phase,
            )
        except Exception as exc:
            progress.last_error = f"{type(exc).__name__}: {exc}"
            save_progress(config.state_dir, progress)
            raise


def _render_only(
    config: ReportsConfig,
    report_day: date,
    plan_path: Path | None,
    sources: PlannerSources | None,
    rid: str,
) -> MorningResult:
    """`--dry-run` / `--replay`: render from whatever the plan says, mutate nothing."""
    if plan_path is not None:
        plan = load_plan_file(plan_path)
        if plan.day != report_day:
            raise ValueError("plan day does not match --date")
    elif config.planner_provider is not None:
        plan = run_planner_provider(
            config.planner_provider, day=report_day, wiki_dir=config.wiki_dir
        ).plan
    else:
        plan = build_plan_from_sources(report_day, sources or UnavailableSources())

    state = StateTransaction(config.state_dir)
    from swarm_reports.metrics.state import load_state

    current = load_state(state.state_path) if state.state_path.exists() else ReportsState()
    bucket = current.days.get(report_day.isoformat())
    if bucket is not None and bucket.frozen_checklist:
        plan = _plan_with_frozen_checklist(plan, list(bucket.frozen_checklist))
    plan = _with_real_ledger(plan, config.state_dir, report_day)
    metrics = build_metric_tiles(
        current, report_day, wiki_dir=config.wiki_dir, weights_path=config.weights_path
    )
    html = render_morning_html(
        MorningViewModel(
            day=report_day,
            owner_id=config.owner_id,
            metrics=metrics,
            plan=plan,
            evening_seed=_evening_seed(plan, config.owner_id),
            done_task_ids=sorted(bucket.evening_validated_ids) if bucket else [],
            post_url=config.evening_post_url,
            revision_url=config.evening_revision_url(report_day),
        )
    )
    html_path = html_output_path(config, report_day)
    atomic_write_text(html_path, html, mode=0o640)
    return MorningResult(
        html_path=html_path,
        plan=plan,
        freeze_applied=False,
        run_id=rid,
        phase="render-only",
    )
