from __future__ import annotations

from datetime import datetime

import pytest
from swarm_reports.dispatch.quota import (
    DEFAULT_UNKNOWN_BUDGET_PCT,
    ProviderQuota,
    QuotaWindow,
    UsageLedger,
    daily_budget,
)

NOW = datetime(2026, 9, 14, 3, 0, 0)  # 3am: overnight window
DAYS = {"seven_day": 3, "monthly": 14}


def _quota(five_hour=None, seven_day=None, monthly=None) -> ProviderQuota:
    return ProviderQuota(
        provider="claude",
        five_hour=QuotaWindow(five_hour),
        seven_day=QuotaWindow(seven_day),
        monthly=QuotaWindow(monthly),
        probed_at=NOW.isoformat(),
    )


def test_weekly_almost_empty_wins_over_generous_monthly():
    quota = _quota(five_hour=90.0, seven_day=3.0, monthly=60.0)
    budget = daily_budget(quota, days_remaining=DAYS, now=NOW)
    # seven_day: 3/3 = 1.0 pct/day; monthly: 60/14 ~= 4.28 pct/day -> tighter wins
    assert budget.budget_pct == pytest.approx(1.0)
    assert budget.source == "computed"


def test_short_vs_weekly_five_hour_admissible_but_does_not_raise_daily_budget():
    quota = _quota(five_hour=95.0, seven_day=10.0, monthly=100.0)
    budget = daily_budget(quota, days_remaining=DAYS, now=NOW)
    assert budget.five_hour_admissible is True
    # daily budget still gated by the tighter long window, not by 5h headroom
    assert budget.budget_pct == pytest.approx(min(10.0 / 3, 100.0 / 14))


def test_five_hour_exhausted_blocks_overnight_admission():
    quota = _quota(five_hour=0.0, seven_day=50.0, monthly=50.0)
    budget = daily_budget(quota, days_remaining=DAYS, now=NOW)
    assert budget.five_hour_admissible is False


def test_unknown_provider_falls_back_to_local_ledger_not_zero_not_unlimited():
    quota = _quota(five_hour=None, seven_day=None, monthly=None)
    ledger = UsageLedger(daily_cap_pct=DEFAULT_UNKNOWN_BUDGET_PCT)
    budget = daily_budget(quota, days_remaining=DAYS, now=NOW, ledger=ledger, date_key="2026-09-14")
    assert budget.source == "unknown_fallback"
    assert budget.budget_pct == pytest.approx(DEFAULT_UNKNOWN_BUDGET_PCT)
    assert budget.budget_pct > 0

    ledger.record_usage("claude", "2026-09-14", 6.0)
    budget2 = daily_budget(quota, days_remaining=DAYS, now=NOW, ledger=ledger, date_key="2026-09-14")
    assert budget2.budget_pct == pytest.approx(DEFAULT_UNKNOWN_BUDGET_PCT - 6.0)


def test_ledger_usage_caps_at_zero_and_never_goes_negative():
    ledger = UsageLedger(daily_cap_pct=5.0)
    ledger.record_usage("codex", "2026-09-14", 4.0)
    ledger.record_usage("codex", "2026-09-14", 4.0)
    assert ledger.remaining_pct("codex", "2026-09-14") == 0.0


def test_ledger_resets_on_new_date_key():
    ledger = UsageLedger(daily_cap_pct=10.0)
    ledger.record_usage("codex", "2026-09-14", 9.0)
    assert ledger.remaining_pct("codex", "2026-09-15") == 10.0


def test_one_unknown_long_bucket_still_uses_the_reported_one():
    quota = _quota(five_hour=50.0, seven_day=None, monthly=42.0)
    budget = daily_budget(quota, days_remaining=DAYS, now=NOW)
    assert budget.source == "computed"
    assert budget.budget_pct == pytest.approx(42.0 / 14)


def test_out_of_range_remaining_pct_rejected():
    with pytest.raises(ValueError):
        QuotaWindow(150.0)
