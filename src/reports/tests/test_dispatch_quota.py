from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from swarm_reports.dispatch.quota import (
    EstimatedBudget,
    ProviderQuota,
    QuotaExhausted,
    QuotaWindow,
    ResetPolicy,
    UnknownQuota,
    UsageLedger,
    daily_budget,
    five_hour_admission,
)
from swarm_reports.dispatch.statefile import StateError

NOW = datetime(2026, 9, 14, 3, 0, 0)  # 3am: the overnight window
DAYS = {"seven_day": 3, "monthly": 14}
ACCOUNT = "owner@anthropic-max"


def _quota(five_hour=None, seven_day=None, monthly=None, *, provider="claude", account=ACCOUNT) -> ProviderQuota:
    def w(value):
        return QuotaWindow.reported(value) if value is not None else QuotaWindow.missing()

    return ProviderQuota(
        provider=provider,
        account=account,
        five_hour=w(five_hour),
        seven_day=w(seven_day),
        monthly=w(monthly),
        probed_at=NOW.isoformat(),
    )


# --- daily budget: tighter long window wins ---------------------------------


def test_weekly_almost_empty_wins_over_generous_monthly():
    budget = daily_budget(_quota(90.0, 3.0, 60.0), days_remaining=DAYS, now=NOW)
    # seven_day: 3/3 = 1.0 pct/day; monthly: 60/14 ~= 4.28 pct/day -> tighter wins
    assert budget.budget_pct == pytest.approx(1.0)
    assert budget.source == "computed"
    assert budget.basis == ("seven_day",)


def test_five_hour_headroom_never_raises_the_daily_budget():
    budget = daily_budget(_quota(95.0, 10.0, 100.0), days_remaining=DAYS, now=NOW)
    assert budget.budget_pct == pytest.approx(min(10.0 / 3, 100.0 / 14))


def test_one_unread_long_bucket_still_uses_the_reported_one():
    budget = daily_budget(_quota(50.0, None, 42.0), days_remaining=DAYS, now=NOW)
    assert budget.source == "computed"
    assert budget.basis == ("monthly",)
    assert budget.budget_pct == pytest.approx(42.0 / 14)


def test_fractional_days_remaining_are_allowed_mid_cycle():
    """A billing cycle that resets this afternoon leaves less than a full day."""
    budget = daily_budget(_quota(None, 10.0, None), days_remaining={"seven_day": 0.5}, now=NOW)
    assert budget.budget_pct == pytest.approx(20.0)


@pytest.mark.parametrize("days", [0, -1, float("inf"), float("nan"), "3", True])
def test_invalid_days_remaining_are_rejected(days):
    with pytest.raises(ValueError):
        daily_budget(_quota(None, 10.0, None), days_remaining={"seven_day": days}, now=NOW)


def test_missing_days_remaining_for_a_reported_window_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        daily_budget(_quota(None, 10.0, None), days_remaining={"monthly": 30}, now=NOW)


def test_days_remaining_not_required_for_windows_the_provider_did_not_report():
    budget = daily_budget(_quota(None, None, 42.0), days_remaining={"monthly": 14}, now=NOW)
    assert budget.basis == ("monthly",)


# --- finding 4a: a provider that reports nothing gets no invented budget -----


def test_provider_with_no_long_window_refuses_to_invent_a_budget():
    with pytest.raises(UnknownQuota):
        daily_budget(_quota(None, None, None), days_remaining=DAYS, now=NOW)


def test_unknown_provider_uses_only_an_explicitly_declared_estimate(tmp_path):
    estimate = EstimatedBudget(daily_pct=8.0, declared_by="config:quota.unknown_daily_budget_pct")
    budget = daily_budget(
        _quota(None, None, None),
        days_remaining=DAYS,
        now=NOW,
        ledger=UsageLedger(tmp_path),
        date_key="2026-09-14",
        estimated=estimate,
    )
    assert budget.source == "declared_estimate"
    assert budget.budget_pct == pytest.approx(8.0)
    assert "config:quota.unknown_daily_budget_pct" in budget.note


def test_an_estimate_must_name_who_declared_it():
    with pytest.raises(ValueError):
        EstimatedBudget(daily_pct=8.0, declared_by="   ")


# --- finding 4b: unsupported vs missing windows ------------------------------


def test_unsupported_window_is_not_the_same_as_unread():
    """Cursor documents no 5h window (F0 §2.3); Claude may simply not report one."""
    cursor = ProviderQuota(
        provider="cursor",
        account="owner@cursor",
        five_hour=QuotaWindow.unsupported(),
        seven_day=QuotaWindow.unsupported(),
        monthly=QuotaWindow.reported(40.0),
        probed_at=NOW.isoformat(),
    )
    assert five_hour_admission(cursor, now=NOW).admit is True
    assert "no five-hour window" in five_hour_admission(cursor, now=NOW).reason
    assert five_hour_admission(_quota(None, 5.0, None), now=NOW).assumed is True
    budget = daily_budget(cursor, days_remaining={"monthly": 10}, now=NOW)
    assert budget.basis == ("monthly",)  # the unsupported weekly bucket is not invented


def test_a_window_cannot_be_reported_without_a_number():
    with pytest.raises(ValueError):
        QuotaWindow(remaining_pct=None, state="reported")


def test_a_missing_window_cannot_smuggle_a_number():
    with pytest.raises(ValueError):
        QuotaWindow(remaining_pct=30.0, state="missing")


@pytest.mark.parametrize("value", [150.0, -1.0, float("nan"), float("inf"), "50", True])
def test_out_of_range_or_non_finite_remaining_pct_rejected(value):
    with pytest.raises(ValueError):
        QuotaWindow.reported(value)


# --- finding 4c: the five-hour window is an admission gate -------------------


def test_empty_five_hour_window_blocks_a_launch_before_its_reset():
    quota = ProviderQuota(
        provider="claude",
        account=ACCOUNT,
        five_hour=QuotaWindow.reported(0.0, resets_at="2026-09-14T05:00:00"),
        seven_day=QuotaWindow.reported(50.0),
        monthly=QuotaWindow.missing(),
        probed_at=NOW.isoformat(),
    )
    admission = five_hour_admission(quota, now=NOW)
    assert admission.admit is False
    assert "until 2026-09-14T05:00:00" in admission.reason
    # the long-window budget is untouched: the gate is about timing, not budget
    assert daily_budget(quota, days_remaining=DAYS, now=NOW).budget_pct == pytest.approx(50.0 / 3)


def test_expired_reset_is_not_read_as_refilled_without_a_fresh_probe():
    """`resets_at` in the past means the snapshot is stale, not that quota returned."""
    quota = ProviderQuota(
        provider="claude",
        account=ACCOUNT,
        five_hour=QuotaWindow.reported(0.0, resets_at="2026-09-14T02:00:00"),
        seven_day=QuotaWindow.reported(50.0),
        monthly=QuotaWindow.missing(),
        probed_at=NOW.isoformat(),
    )
    admission = five_hour_admission(quota, now=NOW)
    assert admission.admit is False
    assert admission.needs_reprobe is True


def test_expired_reset_may_be_treated_as_refilled_only_under_a_declared_policy():
    quota = ProviderQuota(
        provider="claude",
        account=ACCOUNT,
        five_hour=QuotaWindow.reported(0.0, resets_at="2026-09-14T02:00:00"),
        seven_day=QuotaWindow.reported(50.0),
        monthly=QuotaWindow.missing(),
        probed_at=NOW.isoformat(),
    )
    policy = ResetPolicy(treat_expired_reset_as_refilled=True, declared_by="owner:2026-09-14")
    admission = five_hour_admission(quota, now=NOW, policy=policy)
    assert admission.admit is True
    assert admission.assumed is True and admission.needs_reprobe is True


def test_a_refill_policy_must_name_who_declared_it():
    with pytest.raises(ValueError):
        ResetPolicy(treat_expired_reset_as_refilled=True)


def test_mixing_an_aware_reset_time_with_a_naive_clock_is_an_explicit_error():
    """A statusline may report `resets_at` with an offset; the run clock is naive."""
    quota = ProviderQuota(
        provider="claude",
        account=ACCOUNT,
        five_hour=QuotaWindow.reported(0.0, resets_at="2026-09-14T05:00:00-03:00"),
        seven_day=QuotaWindow.reported(50.0),
        monthly=QuotaWindow.missing(),
        probed_at=NOW.isoformat(),
    )
    with pytest.raises(ValueError) as exc:
        five_hour_admission(quota, now=NOW)
    assert "America/Recife" in str(exc.value)


def test_an_aware_reset_time_works_with_an_aware_clock():
    from datetime import timezone

    recife = timezone(timedelta(hours=-3))
    quota = ProviderQuota(
        provider="claude",
        account=ACCOUNT,
        five_hour=QuotaWindow.reported(0.0, resets_at="2026-09-14T05:00:00-03:00"),
        seven_day=QuotaWindow.reported(50.0),
        monthly=QuotaWindow.missing(),
        probed_at=NOW.isoformat(),
    )
    admission = five_hour_admission(quota, now=NOW.replace(tzinfo=recife))
    assert admission.admit is False


def test_empty_five_hour_window_with_no_reset_time_blocks_and_asks_for_a_reprobe():
    admission = five_hour_admission(_quota(0.0, 50.0, None), now=NOW)
    assert admission.admit is False and admission.needs_reprobe is True


# --- finding 4d: the ledger is durable and account-scoped -------------------


def test_ledger_survives_a_restart(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 4.0, budget_pct=10.0, reservation_id="r1")
    assert UsageLedger(tmp_path).committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(4.0)


def test_ledger_survives_a_restart_in_another_process(tmp_path):
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from swarm_reports.dispatch.quota import UsageLedger
        UsageLedger(Path({str(tmp_path)!r})).reserve(
            {ACCOUNT!r}, "2026-09-14", 3.5, budget_pct=10.0, reservation_id="from-child")
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
    assert UsageLedger(tmp_path).committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(3.5)


def test_known_budget_is_debited_by_prior_spend_even_when_the_snapshot_has_not_moved(tmp_path):
    """The provider snapshot lags: two runs can read the same 30% an hour apart."""
    ledger = UsageLedger(tmp_path)
    quota = _quota(90.0, 30.0, None)
    first = daily_budget(quota, days_remaining={"seven_day": 3}, now=NOW, ledger=ledger, date_key="2026-09-14")
    assert first.budget_pct == pytest.approx(10.0)

    ledger.reserve(ACCOUNT, "2026-09-14", 6.0, budget_pct=first.budget_pct, reservation_id="r1")
    ledger.debit("r1")

    second = daily_budget(quota, days_remaining={"seven_day": 3}, now=NOW, ledger=ledger, date_key="2026-09-14")
    assert second.gross_pct == pytest.approx(10.0)
    assert second.committed_pct == pytest.approx(6.0)
    assert second.budget_pct == pytest.approx(4.0)


def test_two_harnesses_on_one_account_share_the_budget(tmp_path):
    """Same subscription, two CLIs: the key is the account, not the provider name."""
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 7.0, budget_pct=10.0, reservation_id="desktop")
    other_harness = _quota(90.0, 30.0, None, provider="claude-code-vps")
    budget = daily_budget(
        other_harness, days_remaining={"seven_day": 3}, now=NOW, ledger=ledger, date_key="2026-09-14"
    )
    assert budget.budget_pct == pytest.approx(3.0)


def test_separate_accounts_do_not_share_the_budget(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 7.0, budget_pct=10.0, reservation_id="a")
    other = _quota(90.0, 30.0, None, account="second-subscription")
    budget = daily_budget(other, days_remaining={"seven_day": 3}, now=NOW, ledger=ledger, date_key="2026-09-14")
    assert budget.budget_pct == pytest.approx(10.0)


def test_ledger_does_not_carry_over_to_the_next_day(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 9.0, budget_pct=10.0, reservation_id="r1")
    assert ledger.committed_pct(ACCOUNT, "2026-09-15") == 0.0


def test_reservation_beyond_the_budget_is_refused_not_capped(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 8.0, budget_pct=10.0, reservation_id="r1")
    with pytest.raises(QuotaExhausted):
        ledger.reserve(ACCOUNT, "2026-09-14", 3.0, budget_pct=10.0, reservation_id="r2")
    assert ledger.committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(8.0)


def test_refund_releases_an_unspent_reservation(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 8.0, budget_pct=10.0, reservation_id="r1")
    ledger.refund("r1")
    assert ledger.committed_pct(ACCOUNT, "2026-09-14") == 0.0


def test_a_debited_reservation_cannot_be_refunded_away(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 8.0, budget_pct=10.0, reservation_id="r1")
    ledger.debit("r1", 5.0)
    with pytest.raises(ValueError):
        ledger.refund("r1")
    assert ledger.committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(5.0)


def test_debit_is_idempotent_so_a_retried_completion_does_not_double_charge(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 4.0, budget_pct=10.0, reservation_id="r1")
    ledger.debit("r1", 4.0)
    ledger.debit("r1", 4.0)
    assert ledger.committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(4.0)


def test_duplicate_reservation_id_is_rejected(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 1.0, budget_pct=10.0, reservation_id="r1")
    with pytest.raises(ValueError):
        ledger.reserve(ACCOUNT, "2026-09-14", 1.0, budget_pct=10.0, reservation_id="r1")


def test_concurrent_reservations_cannot_both_take_the_last_slice(tmp_path):
    """Weekly quota nearly empty: two dispatchers, one slice. Exactly one wins."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from swarm_reports.dispatch.quota import UsageLedger, QuotaExhausted
        ledger = UsageLedger(Path({str(tmp_path)!r}))
        rid = sys.argv[1]
        try:
            ledger.reserve({ACCOUNT!r}, "2026-09-14", 0.9, budget_pct=1.0, reservation_id=rid)
            print("reserved")
        except QuotaExhausted:
            print("refused")
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, f"r{i}"], stdout=subprocess.PIPE, text=True) for i in range(2)
    ]
    outs = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert outs == ["refused", "reserved"], outs
    assert UsageLedger(tmp_path).committed_pct(ACCOUNT, "2026-09-14") == pytest.approx(0.9)


def test_ledger_file_is_0600(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.reserve(ACCOUNT, "2026-09-14", 1.0, budget_pct=10.0, reservation_id="r1")
    assert stat.S_IMODE((tmp_path / "quota-ledger.json").stat().st_mode) == 0o600


def test_remaining_pct_requires_a_declared_cap(tmp_path):
    with pytest.raises(ValueError):
        UsageLedger(tmp_path).remaining_pct(ACCOUNT, "2026-09-14")
    capped = UsageLedger(tmp_path, daily_cap_pct=10.0)
    capped.reserve(ACCOUNT, "2026-09-14", 4.0, budget_pct=10.0, reservation_id="r1")
    assert capped.remaining_pct(ACCOUNT, "2026-09-14") == pytest.approx(6.0)


@pytest.mark.parametrize(
    "entries",
    [
        {"r1": {"account": ACCOUNT, "date": "2026-09-14", "amount_pct": "4", "state": "reserved"}},
        {"r1": {"account": ACCOUNT, "date": "not-a-date", "amount_pct": 4.0, "state": "reserved"}},
        {"r1": {"account": ACCOUNT, "date": "2026-09-14", "amount_pct": 400.0, "state": "reserved"}},
        {"r1": {"account": ACCOUNT, "date": "2026-09-14", "amount_pct": 4.0, "state": "spent"}},
        {"r1": ["not", "an", "object"]},
    ],
)
def test_corrupt_ledger_entries_are_rejected_not_read_as_zero_usage(tmp_path, entries):
    (tmp_path / "quota-ledger.json").write_text(json.dumps({"version": 1, "entries": entries}))
    with pytest.raises(StateError):
        UsageLedger(tmp_path).committed_pct(ACCOUNT, "2026-09-14")


def test_unsupported_ledger_version_is_rejected(tmp_path):
    (tmp_path / "quota-ledger.json").write_text(json.dumps({"version": 99, "entries": {}}))
    with pytest.raises(StateError):
        UsageLedger(tmp_path).committed_pct(ACCOUNT, "2026-09-14")
