"""Provider eligibility and weighted round-robin task routing.

Mirrors skills-catalog/skills/handoff/references/{routing,quota}.md so the
dispatcher and the interactive handoff skill agree on the same rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence

Bucket = Literal["empty", "low", "ok", "unknown"]

DEFAULT_LOW_THRESHOLD = 20.0
DEFAULT_STALE_AFTER = timedelta(hours=6)
UNKNOWN_WEIGHT = 50.0
DEFAULT_ORDER: tuple[str, ...] = ("cursor", "claude", "codex")


MIN_WEIGHT = 1e-6


@dataclass(frozen=True)
class ProviderState:
    provider: str
    remaining_pct: float | None
    probed_at: datetime | None = None
    stale: bool = False  # set by classify(); distinguishes "old probe" from "probe failed"

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string")
        pct = self.remaining_pct
        if pct is None:
            return
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(float(pct)):
            raise ValueError(f"remaining_pct must be a finite number or None, got {pct!r}")
        if not 0.0 <= float(pct) <= 100.0:
            raise ValueError(f"remaining_pct out of range 0..100: {pct!r}")


def classify(
    provider: str,
    remaining_pct: float | None,
    *,
    probed_at: datetime | None = None,
    now: datetime | None = None,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> ProviderState:
    """Bucket a raw probe reading, marking it stale if it is too old to trust."""
    stale = False
    pct = remaining_pct
    if pct is not None and probed_at is not None and now is not None:
        if now - probed_at > stale_after:
            stale = True
            pct = None  # a stale reading is treated as unknown, not as current fact
    return ProviderState(provider=provider, remaining_pct=pct, probed_at=probed_at, stale=stale)


def _bucket(state: ProviderState, low_threshold: float) -> Bucket:
    if state.remaining_pct is None:
        return "unknown"
    if state.remaining_pct <= 0:
        return "empty"
    if state.remaining_pct < low_threshold:
        return "low"
    return "ok"


def eligible(
    states: Sequence[ProviderState], *, low_threshold: float = DEFAULT_LOW_THRESHOLD
) -> list[ProviderState]:
    """ok ∪ unknown, falling back to the richest "low" provider, never "empty"."""
    buckets = {s.provider: _bucket(s, low_threshold) for s in states}
    ok_or_unknown = [s for s in states if buckets[s.provider] in ("ok", "unknown")]
    if ok_or_unknown:
        return ok_or_unknown
    low = [s for s in states if buckets[s.provider] == "low"]
    if low:
        return [max(low, key=lambda s: s.remaining_pct or 0.0)]
    return []


def _weight(state: ProviderState, low_threshold: float) -> float:
    if state.remaining_pct is None:
        return UNKNOWN_WEIGHT
    # a weight of exactly 0 would divide by zero in the round-robin; `eligible`
    # already excludes empty providers, so this is a floor, not a policy
    return max(float(state.remaining_pct), MIN_WEIGHT)


def _rotation_order(parent: str, order: Sequence[str]) -> list[str]:
    if parent not in order:
        return list(order)
    idx = order.index(parent)
    return list(order[idx + 1 :]) + list(order[: idx + 1])


def route(
    task_ids: Sequence[str],
    states: Sequence[ProviderState],
    *,
    parent: str,
    order: Sequence[str] = DEFAULT_ORDER,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assign each task_id to a provider name. Empty result if nothing is eligible."""
    overrides = overrides or {}
    seen_providers = {s.provider for s in states}
    for task_id, forced in (overrides or {}).items():
        if forced not in seen_providers:
            raise ValueError(f"override for {task_id!r} names unknown provider {forced!r}")
    elig = eligible(states, low_threshold=low_threshold)
    if not elig:
        return {}
    weight = {s.provider: _weight(s, low_threshold) for s in elig}
    rotation = [p for p in _rotation_order(parent, order) if p in weight]
    # providers eligible but not named in `order` still participate, appended stably
    rotation += [p for p in weight if p not in rotation]

    assigned: dict[str, int] = {p: 0 for p in weight}
    result: dict[str, str] = {}
    for task_id in task_ids:
        override = overrides.get(task_id)
        if override is not None:
            result[task_id] = override
            assigned[override] = assigned.get(override, 0) + 1
            continue
        pick = min(rotation, key=lambda p: (assigned[p] / weight[p], rotation.index(p)))
        result[task_id] = pick
        assigned[pick] += 1
    return result
