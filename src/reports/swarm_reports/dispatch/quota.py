"""Provider quota snapshots and daily budget pacing.

Design (see mathai-wiki estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md,
section "Providers e quota"):

- Daily budget = remaining / days_remaining, the *more restrictive* of the
  seven_day and monthly buckets. The five_hour window is a *timing*
  permission only: if it resets before the morning run, it may be spent
  overnight, but that usage still debits the long-window daily budget,
  because real provider usage is cumulative across windows.
- A provider that does not report quota (`remaining_pct is None`) is treated
  as "not zero": it falls back to a local capped usage ledger instead of
  being assumed exhausted or unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

BucketName = Literal["five_hour", "seven_day", "monthly"]
BudgetSource = Literal["computed", "unknown_fallback"]

DEFAULT_UNKNOWN_BUDGET_PCT = 10.0
"""Conservative daily allotment (percent) assumed when no bucket reports a number."""


@dataclass(frozen=True)
class QuotaWindow:
    """A single quota bucket as reported (or not) by a provider probe."""

    remaining_pct: float | None  # None = provider did not report this bucket
    resets_at: str | None = None  # ISO 8601, provider-local meaning

    def __post_init__(self) -> None:
        if self.remaining_pct is not None and not (0.0 <= self.remaining_pct <= 100.0):
            raise ValueError(f"remaining_pct out of range: {self.remaining_pct!r}")


@dataclass(frozen=True)
class ProviderQuota:
    """Full snapshot for one provider at probe time."""

    provider: str
    five_hour: QuotaWindow
    seven_day: QuotaWindow
    monthly: QuotaWindow
    probed_at: str  # ISO 8601 datetime, America/Recife wall clock


@dataclass(frozen=True)
class DailyBudget:
    provider: str
    budget_pct: float
    five_hour_admissible: bool
    source: BudgetSource


class UsageLedger:
    """Local, capped usage counter keyed by (provider, date).

    Used only when a provider does not report quota. Resets automatically
    per calendar date key; there is no cross-date carryover.
    """

    def __init__(self, *, daily_cap_pct: float = DEFAULT_UNKNOWN_BUDGET_PCT) -> None:
        self._daily_cap_pct = daily_cap_pct
        self._used: dict[tuple[str, str], float] = {}

    def record_usage(self, provider: str, date_key: str, amount_pct: float) -> None:
        if amount_pct < 0:
            raise ValueError("amount_pct must be non-negative")
        key = (provider, date_key)
        self._used[key] = min(self._daily_cap_pct, self._used.get(key, 0.0) + amount_pct)

    def remaining_pct(self, provider: str, date_key: str) -> float:
        used = self._used.get((provider, date_key), 0.0)
        return max(0.0, self._daily_cap_pct - used)


def _long_bucket_daily_pct(window: QuotaWindow, days_remaining: int) -> float | None:
    if window.remaining_pct is None:
        return None
    days = max(days_remaining, 1)
    return window.remaining_pct / days


def daily_budget(
    quota: ProviderQuota,
    *,
    days_remaining: Mapping[str, int],
    now: datetime,
    ledger: UsageLedger | None = None,
    date_key: str | None = None,
) -> DailyBudget:
    """Compute the normalized daily budget (percent) for a provider.

    ``days_remaining`` must have "seven_day" and "monthly" keys (days left in
    each billing/reset period, >= 1). The tighter of the two wins.
    """
    long_pcts = [
        pct
        for pct in (
            _long_bucket_daily_pct(quota.seven_day, days_remaining["seven_day"]),
            _long_bucket_daily_pct(quota.monthly, days_remaining["monthly"]),
        )
        if pct is not None
    ]

    if long_pcts:
        budget_pct = min(long_pcts)
        source: BudgetSource = "computed"
    else:
        key = date_key or now.date().isoformat()
        budget_pct = ledger.remaining_pct(quota.provider, key) if ledger else DEFAULT_UNKNOWN_BUDGET_PCT
        source = "unknown_fallback"

    five_hour_admissible = quota.five_hour.remaining_pct is None or quota.five_hour.remaining_pct > 0.0

    return DailyBudget(
        provider=quota.provider,
        budget_pct=budget_pct,
        five_hour_admissible=five_hour_admissible,
        source=source,
    )
