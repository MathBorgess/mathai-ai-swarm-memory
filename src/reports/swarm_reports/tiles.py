"""The fixed metric band at the top of the report.

Every tile is derived from persisted state through the F1 formulas. The earlier F2
band could only ever print "sem validação": it called `compute_completion(note, None)`,
which returns `None` by contract, and hardcoded the 7-day tile. A band that cannot
show a number is not a metric, so each tile here reads the validated evening ids that
F4 records and the carryover that survives a day rollover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from swarm_reports.metrics.daily import ChecklistItem, DailyNote
from swarm_reports.metrics.execution import (
    EveningSubmission,
    compute_completion,
    compute_date_drift,
)
from swarm_reports.metrics.posts import parse_post_markdown
from swarm_reports.metrics.res import (
    ResWeights,
    compute_engagement,
    compute_res,
    load_res_weights,
    period_res_summary,
)
from swarm_reports.metrics.state import (
    ReportsState,
    carryover_first_planned,
    validated_task_ids,
)
from swarm_reports.rendering.html import MetricTile

NO_DATA = "sem validação"
WINDOW_DAYS = 7


@dataclass(frozen=True)
class CompletionWindow:
    average: float | None
    closed_days: int
    planned_days: int


@dataclass(frozen=True)
class DriftSummary:
    total_days: int
    open_items: int


@dataclass(frozen=True)
class ResWindow:
    guided_average: float | None
    guided_per_week: float
    spontaneous_average: float | None
    spontaneous_per_week: float
    #: Posts inside the window whose note has no `guided` flag: countable, not attributable.
    unflagged: int


def completion_for_day(state: ReportsState, day: date) -> float | None:
    """Conclusion for one day, or None when the evening never closed it."""
    bucket = state.days.get(day.isoformat())
    if bucket is None or bucket.evening_absent or not bucket.evening_validated:
        return None
    frozen_ids = bucket.frozen_ids()
    if not frozen_ids:
        return None
    note = DailyNote(
        day=day,
        frontmatter={},
        items=[],
        evening_validated=True,
        evening_absent=False,
    )
    submission = EveningSubmission(
        day=day,
        validated_complete_ids=frozenset(bucket.evening_validated_ids),
    )
    return compute_completion(note, submission, frozen_snapshot_ids=frozen_ids)


def completion_window(
    state: ReportsState,
    day: date,
    *,
    days: int = WINDOW_DAYS,
) -> CompletionWindow:
    """Mean conclusion over the validated days in `[day - days, day - 1]`.

    Days that were planned but never closed are excluded from the mean — averaging a
    missing evening as zero would confuse "did not do it" with "did not report it" —
    but they are counted so the tile can show the adherence denominator.
    """
    planned = 0
    values: list[float] = []
    for offset in range(1, days + 1):
        target = day - timedelta(days=offset)
        bucket = state.days.get(target.isoformat())
        if bucket is None or not bucket.frozen_checklist:
            continue
        planned += 1
        value = completion_for_day(state, target)
        if value is not None:
            values.append(value)
    average = sum(values) / len(values) if values else None
    return CompletionWindow(average=average, closed_days=len(values), planned_days=planned)


def open_drift(state: ReportsState, day: date) -> DriftSummary:
    """Days-open summed over every frozen item never validated as done.

    History matters: an item frozen four days ago whose evening never closed still
    accumulates drift, and its `first_planned` comes from carryover rather than from
    the day it happens to reappear on.
    """
    done = validated_task_ids(state)
    earliest: dict[str, date] = {}
    p0: set[str] = set()
    for key, bucket in state.days.items():
        try:
            bucket_day = date.fromisoformat(key)
        except ValueError:
            continue
        if bucket_day > day:
            continue
        for item in bucket.frozen_checklist:
            if item.task_id in done:
                continue
            first = carryover_first_planned(state, item.task_id, item.first_planned)
            current = earliest.get(item.task_id)
            if current is None or first < current:
                earliest[item.task_id] = first
            if item.is_p0:
                p0.add(item.task_id)

    items = [
        ChecklistItem(
            task_id=task_id,
            text="",
            done=False,
            frozen=True,
            added_after_freeze=False,
            is_p0=task_id in p0,
            classification=None,
            first_planned=first,
            line_number=0,
        )
        for task_id, first in sorted(earliest.items())
    ]
    drift = compute_date_drift(items, day, validated_complete_ids=done)
    return DriftSummary(total_days=sum(drift.values()), open_items=len(drift))


def res_window(
    posts_dir: Path,
    weights: ResWeights,
    day: date,
    *,
    days: int = WINDOW_DAYS,
) -> ResWindow:
    """RES over a real rolling window, guided and spontaneous kept apart.

    Averaging the two group means together (the earlier F2 behaviour) answers no
    question the design asks: the point of the split is to compare them.
    """
    window_start = day - timedelta(days=days)
    scores: list[tuple[bool, float | None]] = []
    unflagged = 0
    if posts_dir.is_dir():
        for path in sorted(posts_dir.glob("*.md")):
            try:
                post = parse_post_markdown(path.read_text(encoding="utf-8"), filename=path.name)
            except (ValueError, KeyError, TypeError):
                continue
            if post.posted_on is None or not (window_start < post.posted_on <= day):
                continue
            if post.guided is None:
                unflagged += 1
                continue
            score = _post_res(post, weights)
            scores.append((bool(post.guided), score))

    summary = period_res_summary(scores, period_days=days)
    guided = summary["guided"]
    spontaneous = summary["spontaneous"]
    return ResWindow(
        guided_average=guided["average_res"],
        guided_per_week=guided["posts_per_week"],
        spontaneous_average=spontaneous["average_res"],
        spontaneous_per_week=spontaneous["posts_per_week"],
        unflagged=unflagged,
    )


def _post_res(post, weights: ResWeights) -> float | None:
    metrics = post.metrics
    if metrics is None or metrics.reach is None:
        return None
    try:
        engagement = compute_engagement(weights.signal_weights(post.platform), metrics.signals)
    except (KeyError, ValueError):
        return None
    try:
        return compute_res(engagement, metrics.reach, metrics.outside_fraction)
    except ValueError:
        return None


def build_metric_tiles(
    state: ReportsState,
    day: date,
    *,
    wiki_dir: Path,
    weights_path: Path,
) -> list[MetricTile]:
    yesterday = completion_for_day(state, day - timedelta(days=1))
    window = completion_window(state, day)
    drift = open_drift(state, day)

    tiles = [
        MetricTile("Ontem", _percent(yesterday)),
        MetricTile(
            "7d conclusão",
            f"{_percent(window.average)} · {window.closed_days}/{window.planned_days} fechados",
        ),
        MetricTile("Drift", f"{drift.total_days} d · {drift.open_items} abertos"),
    ]

    try:
        weights = load_res_weights(weights_path)
    except (OSError, ValueError, KeyError):
        tiles.append(MetricTile("RES 7d", f"{NO_DATA} (pesos)"))
        return tiles

    res = res_window(wiki_dir / "brand" / "posts", weights, day)
    tiles.append(
        MetricTile("RES guiado 7d", f"{_res(res.guided_average)} · {res.guided_per_week:.1f}/sem")
    )
    tiles.append(
        MetricTile(
            "RES espontâneo 7d",
            f"{_res(res.spontaneous_average)} · {res.spontaneous_per_week:.1f}/sem",
        )
    )
    if res.unflagged:
        tiles.append(MetricTile("Posts sem flag guided", str(res.unflagged)))
    return tiles


def _percent(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"{value * 100:.0f}%"


def _res(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"{value:.1f}"
