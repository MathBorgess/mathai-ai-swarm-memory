"""Morning report orchestration (freeze + metrics + HTML)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from swarm_reports.config import ReportsConfig
from swarm_reports.evening_schema import EveningChecklistItem, EveningPayload, SCHEMA_VERSION
from swarm_reports.metrics.daily import parse_daily_markdown
from swarm_reports.metrics.execution import compute_completion
from swarm_reports.metrics.res import compute_engagement
from swarm_reports.metrics.posts import parse_post_markdown
from swarm_reports.metrics.res import compute_res, load_res_weights, period_res_summary
from swarm_reports.metrics.state import FrozenItem, apply_morning_freeze
from swarm_reports.plan import MorningPlan, load_plan_file
from swarm_reports.providers import run_planner_provider
from swarm_reports.rendering.html import MetricTile, MorningViewModel, render_morning_html
from swarm_reports.sources import UnavailableSources, build_plan_from_sources
from swarm_reports.storage import StateTransaction, morning_run_claim, record_morning_complete
from swarm_reports.wiki.freeze import apply_wiki_freeze


@dataclass(frozen=True)
class MorningResult:
    html_path: Path
    plan: MorningPlan
    freeze_applied: bool
    run_id: str
    skipped_duplicate: bool


def _parse_day(value: str | None, tz_name: str) -> date:
    from zoneinfo import ZoneInfo

    if value:
        return date.fromisoformat(value)
    return datetime_now(tz_name).date()


def datetime_now(tz_name: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name))


def _plan_to_frozen_items(plan: MorningPlan) -> list[FrozenItem]:
    items: list[FrozenItem] = []
    for entry in plan.p0_items:
        items.append(
            FrozenItem(
                task_id=str(entry["task_id"]),
                text=str(entry.get("text") or ""),
                first_planned=date.fromisoformat(str(entry.get("first_planned") or plan.day.isoformat())),
                is_p0=True,
            )
        )
    return items


def _load_weights(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"RES weights missing at {path}")
    return load_res_weights(path)


def _metric_completion(wiki_dir: Path, state_dir: Path, day: date) -> str:
    yesterday = day - timedelta(days=1)
    path = wiki_dir / "daily" / f"{yesterday.isoformat()}.md"
    if not path.exists():
        return "sem validação"
    note = parse_daily_markdown(path.read_text(encoding="utf-8"), yesterday)
    value = compute_completion(note, None)
    if value is None:
        return "sem validação"
    return f"{value * 100:.0f}%"


def _metric_res_7d(wiki_dir: Path, weights_path: Path, day: date) -> str:
    posts_dir = wiki_dir / "brand" / "posts"
    if not posts_dir.is_dir():
        return "sem validação"
    try:
        res_weights = _load_weights(weights_path)
    except (OSError, ValueError, KeyError):
        return "sem validação"
    scored: list[tuple[bool, float | None]] = []
    for path in sorted(posts_dir.glob("*.md")):
        try:
            post = parse_post_markdown(path.read_text(encoding="utf-8"))
        except (ValueError, KeyError):
            continue
        metrics = post.metrics
        if metrics is None:
            continue
        try:
            platform_weights = res_weights.signal_weights(post.platform)
            engagement = compute_engagement(platform_weights, metrics.signals)
        except (KeyError, ValueError):
            continue
        score = compute_res(engagement, metrics.reach or 0.0, metrics.outside_fraction)
        guided = bool(post.guided) if post.guided is not None else True
        scored.append((guided, score))
    if not scored:
        return "sem validação"
    summary = period_res_summary(scored, period_days=7)
    guided_avg = (summary.get("guided") or {}).get("average_res")
    spont_avg = (summary.get("spontaneous") or {}).get("average_res")
    values = [v for v in (guided_avg, spont_avg) if v is not None]
    if not values:
        return "sem validação"
    return f"{sum(values) / len(values):.1f}"


def _metric_drift(wiki_dir: Path, day: date) -> str:
    path = wiki_dir / "daily" / f"{day.isoformat()}.md"
    if not path.exists():
        return "sem validação"
    note = parse_daily_markdown(path.read_text(encoding="utf-8"), day)
    open_items = [item for item in note.items if not item.done and item.frozen]
    if not open_items:
        return "0"
    from swarm_reports.metrics.execution import compute_date_drift

    drift = compute_date_drift(open_items, day)
    total = sum(drift.values())
    return str(total)


def _evening_seed(plan: MorningPlan, owner_id: str) -> EveningPayload:
    checklist = [
        EveningChecklistItem(task_id=str(item["task_id"]), done=False)
        for item in plan.p0_items
    ]
    return EveningPayload(
        schema_version=SCHEMA_VERSION,
        day=plan.day,
        owner_id=owner_id,
        checklist=checklist,
    )


def run_morning(
    config: ReportsConfig,
    *,
    day: date | None = None,
    plan_path: Path | None = None,
    sources: UnavailableSources | None = None,
    dry_run: bool = False,
    replay: bool = False,
    run_id: str | None = None,
) -> MorningResult:
    report_day = day or _parse_day(None, config.timezone)
    rid = run_id or str(uuid.uuid4())
    config.output_dir.mkdir(parents=True, exist_ok=True)

    if dry_run or replay:
        return _render_only(
            config, report_day, plan_path, sources, rid, dry_run=dry_run, replay=replay
        )

    with morning_run_claim(config.state_dir, report_day.isoformat(), rid) as claimed:
        if not claimed:
            existing_html = config.output_dir / f"{report_day.isoformat()}.html"
            plan = _load_plan_for_render(config, report_day, plan_path, sources)
            return MorningResult(
                html_path=existing_html,
                plan=plan,
                freeze_applied=False,
                run_id=rid,
                skipped_duplicate=True,
            )

        plan = _load_plan_for_render(config, report_day, plan_path, sources)
        frozen_items = _plan_to_frozen_items(plan)
        freeze_applied = False
        commit_sha: str | None = None
        snapshot: str | None = None

        if not dry_run and not replay:
            txn = StateTransaction(config.state_dir)
            with txn.locked() as state:
                applied_state = apply_morning_freeze(
                    state,
                    report_day,
                    frozen_items,
                    commit=None,
                    snapshot=None,
                )
                if applied_state and frozen_items:
                    freeze_result = apply_wiki_freeze(
                        config.wiki_dir,
                        report_day,
                        frozen_items,
                        timezone=config.timezone,
                        dry_run=False,
                        worktree_parent=config.state_dir / "wiki-worktrees",
                    )
                    freeze_applied = freeze_result.applied
                    commit_sha = freeze_result.commit_sha
                    snapshot = freeze_result.snapshot
                    bucket = state.get_day(report_day)
                    bucket.frozen_at_commit = commit_sha
                    bucket.frozen_at_snapshot = snapshot
                elif applied_state and not frozen_items:
                    apply_morning_freeze(
                        state, report_day, frozen_items, commit="empty", snapshot=""
                    )
                    freeze_applied = True
        elif dry_run:
            freeze_applied = bool(frozen_items)

        metrics = [
            MetricTile("Ontem", _metric_completion(config.wiki_dir, config.state_dir, report_day)),
            MetricTile("7d conclusão", "sem validação"),
            MetricTile("Drift", _metric_drift(config.wiki_dir, report_day)),
            MetricTile("RES 7d", _metric_res_7d(config.wiki_dir, config.weights_path, report_day)),
        ]
        evening_seed = _evening_seed(plan, config.owner_id)
        html = render_morning_html(
            MorningViewModel(
                day=report_day,
                owner_id=config.owner_id,
                metrics=metrics,
                plan=plan,
                evening_seed=evening_seed,
                fetch_revision_url=None,
            )
        )
        html_path = config.output_dir / f"{report_day.isoformat()}.html"
        html_path.write_text(html, encoding="utf-8")
        if not dry_run:
            record_morning_complete(config.state_dir, report_day.isoformat(), rid)
        return MorningResult(
            html_path=html_path,
            plan=plan,
            freeze_applied=freeze_applied,
            run_id=rid,
            skipped_duplicate=False,
        )


def _render_only(
    config: ReportsConfig,
    report_day: date,
    plan_path: Path | None,
    sources: UnavailableSources | None,
    rid: str,
    *,
    dry_run: bool,
    replay: bool,
) -> MorningResult:
    plan = _load_plan_for_render(config, report_day, plan_path, sources)
    metrics = [
        MetricTile("Ontem", _metric_completion(config.wiki_dir, config.state_dir, report_day)),
        MetricTile("7d conclusão", "sem validação"),
        MetricTile("Drift", _metric_drift(config.wiki_dir, report_day)),
        MetricTile("RES 7d", _metric_res_7d(config.wiki_dir, config.weights_path, report_day)),
    ]
    html = render_morning_html(
        MorningViewModel(
            day=report_day,
            owner_id=config.owner_id,
            metrics=metrics,
            plan=plan,
            evening_seed=_evening_seed(plan, config.owner_id),
        )
    )
    html_path = config.output_dir / f"{report_day.isoformat()}.html"
    html_path.write_text(html, encoding="utf-8")
    return MorningResult(
        html_path=html_path,
        plan=plan,
        freeze_applied=False,
        run_id=rid,
        skipped_duplicate=False,
    )


def _load_plan_for_render(
    config: ReportsConfig,
    report_day: date,
    plan_path: Path | None,
    sources: UnavailableSources | None,
) -> MorningPlan:
    if plan_path is not None:
        plan = load_plan_file(plan_path)
        if plan.day != report_day:
            raise ValueError("plan day does not match --date")
        return plan
    if config.planner_provider is not None:
        result = run_planner_provider(
            config.planner_provider,
            day=report_day,
            wiki_dir=config.wiki_dir,
        )
        return result.plan
    src = sources or UnavailableSources()
    return build_plan_from_sources(report_day, src)
