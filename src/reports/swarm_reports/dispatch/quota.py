"""Provider quota snapshots, durable usage ledger, and daily budget pacing.

Design source: mathai-wiki `estudos/context-engineering/2026-09-13-daily-reports-ciclo-self-improvement.md`,
section "Providers e quota". Provider facts: `docs/operations/daily-reports-f0.md`
(branch `codex/daily-reports-f0`), §2.1–2.4.

The rules, and why each one is shaped the way it is:

- **Daily budget = remaining ÷ days remaining, tighter of the long windows.**
  Only windows a provider actually *reported* take part. Per F0 §2.3 Cursor
  has no documented five-hour window at all, and per F0 §2.1 Claude has no
  Cursor-style monthly pool — so "absent" has two distinct meanings and they
  are stored distinctly (``missing`` vs ``unsupported``). Neither is ever
  read as 0% or as 100%.
- **No invented numbers.** A provider that reports nothing has no computable
  budget. This module then requires an explicitly declared estimate
  (``EstimatedBudget``, carrying who declared it); with none supplied it
  raises ``UnknownQuota``. Assuming "about 10% is probably left" is how a
  dispatcher spends a quota the owner was saving.
- **A provider snapshot lags reality.** Two runs an hour apart can see the
  same percentages while the first run already spent quota. So the budget is
  always reduced by what today's durable ledger has already reserved or
  debited for that *account*, snapshot movement or not.
- **Accounts, not providers.** Claude Code, claude.ai and the desktop app
  share one limit (F0 §2.1). The ledger key is the account, so two harnesses
  on the same subscription cannot each spend a full daily budget.
- **The five-hour window is an admission gate, not a bucket.** It can block a
  launch that the daily budget would allow, and an overnight reset can open a
  launch window — but spending inside it still debits the daily budget, and
  an expired ``resets_at`` is never read as "full again" without a fresh
  probe or an explicitly declared reset policy.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from .statefile import (
    StateError,
    file_lock,
    read_json,
    validate_id,
    validate_iso_date,
    write_json_atomic,
)

BucketName = Literal["five_hour", "seven_day", "monthly"]
WindowState = Literal["reported", "missing", "unsupported"]
BudgetSource = Literal["computed", "declared_estimate"]

LEDGER_VERSION = 1
LONG_WINDOWS: tuple[BucketName, ...] = ("seven_day", "monthly")


class UnknownQuota(Exception):
    """No long window reported and no estimate declared: refuse to guess."""


class QuotaExhausted(Exception):
    """A reservation would exceed the remaining daily budget."""


def _check_pct(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"{field_name} out of range 0..100: {value!r}")
    return value


@dataclass(frozen=True)
class QuotaWindow:
    """One quota bucket as reported (or not) by a provider probe.

    ``state`` separates the two ways a number can be absent:
    ``missing`` — the provider has this window but the probe did not read it
    (e.g. Claude's statusline fields depend on the plan, F0 §2.1);
    ``unsupported`` — the provider documents no such window (Cursor's 5h,
    F0 §2.3). Only ``reported`` windows are arithmetic.
    """

    remaining_pct: float | None = None
    resets_at: str | None = None  # ISO 8601, provider-local meaning
    state: WindowState = "missing"

    def __post_init__(self) -> None:
        if self.state not in ("reported", "missing", "unsupported"):
            raise ValueError(f"unknown window state {self.state!r}")
        if self.state == "reported":
            if self.remaining_pct is None:
                raise ValueError("state='reported' requires a remaining_pct")
            object.__setattr__(self, "remaining_pct", _check_pct(self.remaining_pct, field_name="remaining_pct"))
        elif self.remaining_pct is not None:
            raise ValueError(f"state={self.state!r} must not carry a remaining_pct")
        if self.resets_at is not None:
            datetime.fromisoformat(self.resets_at)  # raises on garbage

    @classmethod
    def reported(cls, remaining_pct: float, resets_at: str | None = None) -> "QuotaWindow":
        return cls(remaining_pct=remaining_pct, resets_at=resets_at, state="reported")

    @classmethod
    def missing(cls, resets_at: str | None = None) -> "QuotaWindow":
        return cls(state="missing", resets_at=resets_at)

    @classmethod
    def unsupported(cls) -> "QuotaWindow":
        return cls(state="unsupported")


@dataclass(frozen=True)
class ProviderQuota:
    """Full snapshot for one provider account at probe time."""

    provider: str
    account: str  # billing identity; two CLIs on one subscription share it
    five_hour: QuotaWindow
    seven_day: QuotaWindow
    monthly: QuotaWindow
    probed_at: str  # ISO 8601 datetime, America/Recife wall clock

    def __post_init__(self) -> None:
        validate_id(self.provider, field="provider")
        validate_id(self.account, field="account")
        datetime.fromisoformat(self.probed_at)


@dataclass(frozen=True)
class EstimatedBudget:
    """An explicitly declared daily allotment for a provider that reports nothing.

    ``declared_by`` must name the config key or human decision behind the
    number. A budget nobody claims authorship of is an invented one.
    """

    daily_pct: float
    declared_by: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "daily_pct", _check_pct(self.daily_pct, field_name="daily_pct"))
        validate_id(self.declared_by, field="declared_by")


@dataclass(frozen=True)
class DailyBudget:
    provider: str
    account: str
    budget_pct: float  # already net of today's ledger commitments
    gross_pct: float  # before the ledger debit, for the report
    committed_pct: float
    source: BudgetSource
    basis: tuple[BucketName, ...]  # which windows produced the number
    note: str = ""


@dataclass(frozen=True)
class FiveHourAdmission:
    admit: bool
    reason: str
    needs_reprobe: bool = False
    assumed: bool = False  # True when admission rests on an unread window


@dataclass(frozen=True)
class ResetPolicy:
    """How to read a five-hour window whose ``resets_at`` has passed.

    Default: do not. An expired timestamp means the snapshot is stale, not
    that the window refilled — the probe has to say so.
    """

    treat_expired_reset_as_refilled: bool = False
    declared_by: str = ""

    def __post_init__(self) -> None:
        if self.treat_expired_reset_as_refilled and not self.declared_by.strip():
            raise ValueError("an expired-reset refill policy must name who declared it")


class UsageLedger:
    """Durable reserve → debit/refund ledger, keyed by (account, date).

    In-memory counters were the previous shape and they reset on every
    restart, which turns "budget spent" into "budget available" after a
    crash or a redeploy. This one is a single JSON file under ``state_dir``,
    guarded by a crash-safe lock and written atomically at 0600.
    """

    def __init__(self, state_dir: Path, *, daily_cap_pct: float | None = None) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "quota-ledger.json"
        self._lock = self._dir / "quota-ledger.lock"
        self._cap = None if daily_cap_pct is None else _check_pct(daily_cap_pct, field_name="daily_cap_pct")

    # --- storage ---------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        payload = read_json(self._path)
        if payload is None:
            return {"version": LEDGER_VERSION, "entries": {}}
        version = payload.get("version")
        if version != LEDGER_VERSION:
            raise StateError(f"unsupported ledger version {version!r}")
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise StateError("ledger 'entries' must be an object")
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                raise StateError(f"ledger entry {key!r} is not an object")
            for req, types in (("account", str), ("date", str), ("amount_pct", (int, float)), ("state", str)):
                if req not in entry or isinstance(entry[req], bool) or not isinstance(entry[req], types):
                    raise StateError(f"ledger entry {key!r} field {req!r} invalid")
            if entry["state"] not in ("reserved", "debited"):
                raise StateError(f"ledger entry {key!r} has unknown state {entry['state']!r}")
            try:
                _check_pct(float(entry["amount_pct"]), field_name="amount_pct")
                validate_iso_date(entry["date"])
            except ValueError as exc:
                raise StateError(f"ledger entry {key!r}: {exc}") from exc
        return {"version": LEDGER_VERSION, "entries": entries}

    def _save(self, payload: Mapping[str, Any]) -> None:
        write_json_atomic(self._path, payload)

    # --- reads -----------------------------------------------------------
    def committed_pct(self, account: str, date_key: str) -> float:
        validate_id(account, field="account")
        validate_iso_date(date_key)
        entries = self._load()["entries"]
        return sum(
            float(e["amount_pct"])
            for e in entries.values()
            if e["account"] == account and e["date"] == date_key
        )

    def remaining_pct(self, account: str, date_key: str) -> float:
        if self._cap is None:
            raise ValueError("remaining_pct needs a declared daily_cap_pct")
        return max(0.0, self._cap - self.committed_pct(account, date_key))

    def entries_for(self, account: str, date_key: str) -> dict[str, dict[str, Any]]:
        entries = self._load()["entries"]
        return {k: v for k, v in entries.items() if v["account"] == account and v["date"] == date_key}

    # --- writes ----------------------------------------------------------
    def reserve(
        self,
        account: str,
        date_key: str,
        amount_pct: float,
        *,
        budget_pct: float,
        reservation_id: str | None = None,
    ) -> str:
        """Hold ``amount_pct`` against ``budget_pct``, or raise QuotaExhausted.

        The budget check happens *inside* the lock, so two dispatchers racing
        on the same account cannot both see room for the last slice.
        """
        validate_id(account, field="account")
        validate_iso_date(date_key)
        amount_pct = _check_pct(amount_pct, field_name="amount_pct")
        budget_pct = _check_pct(budget_pct, field_name="budget_pct")
        reservation_id = reservation_id or uuid.uuid4().hex
        validate_id(reservation_id, field="reservation_id")
        with file_lock(self._lock):
            payload = self._load()
            entries = payload["entries"]
            if reservation_id in entries:
                raise ValueError(f"reservation {reservation_id!r} already exists")
            used = sum(
                float(e["amount_pct"])
                for e in entries.values()
                if e["account"] == account and e["date"] == date_key
            )
            if used + amount_pct > budget_pct + 1e-9:
                raise QuotaExhausted(
                    f"{account} {date_key}: {used:.3f}% committed + {amount_pct:.3f}% "
                    f"exceeds budget {budget_pct:.3f}%"
                )
            entries[reservation_id] = {
                "account": account,
                "date": date_key,
                "amount_pct": amount_pct,
                "state": "reserved",
            }
            self._save(payload)
        return reservation_id

    def _update(self, reservation_id: str, mutate) -> None:
        with file_lock(self._lock):
            payload = self._load()
            entries = payload["entries"]
            if reservation_id not in entries:
                raise LookupError(f"no reservation {reservation_id!r}")
            mutate(entries, reservation_id)
            self._save(payload)

    def refund(self, reservation_id: str) -> None:
        """Release an unspent reservation (job never launched)."""

        def mutate(entries, rid):
            if entries[rid]["state"] == "debited":
                raise ValueError(f"reservation {rid!r} already debited; refund would lose spend")
            del entries[rid]

        self._update(reservation_id, mutate)

    def debit(self, reservation_id: str, actual_pct: float | None = None) -> None:
        """Convert a reservation into spend, optionally correcting the amount."""
        amount = None if actual_pct is None else _check_pct(actual_pct, field_name="actual_pct")

        def mutate(entries, rid):
            entry = entries[rid]
            if entry["state"] == "debited":
                return  # idempotent: a retried completion must not double-charge
            entry["state"] = "debited"
            if amount is not None:
                entry["amount_pct"] = amount

        self._update(reservation_id, mutate)


def _validate_days(days_remaining: Mapping[str, float], window: BucketName) -> float:
    if window not in days_remaining:
        raise ValueError(f"days_remaining is missing {window!r}")
    days = days_remaining[window]
    if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(float(days)):
        raise ValueError(f"days_remaining[{window!r}] must be a finite number, got {days!r}")
    if days <= 0:
        raise ValueError(f"days_remaining[{window!r}] must be > 0, got {days!r}")
    return float(days)  # fractional days are legal: a cycle can reset mid-day


def daily_budget(
    quota: ProviderQuota,
    *,
    days_remaining: Mapping[str, int],
    now: datetime,
    ledger: UsageLedger | None = None,
    date_key: str | None = None,
    estimated: EstimatedBudget | None = None,
) -> DailyBudget:
    """Daily budget (percent) for a provider account, net of today's ledger.

    ``days_remaining`` needs a key for every long window the provider
    *reported*; unsupported or unread windows are not required and not
    invented.
    """
    key = validate_iso_date(date_key) if date_key else now.date().isoformat()

    per_day: list[tuple[BucketName, float]] = []
    for name in LONG_WINDOWS:
        window: QuotaWindow = getattr(quota, name)
        if window.state != "reported":
            continue
        days = _validate_days(days_remaining, name)
        per_day.append((name, float(window.remaining_pct) / days))

    note = ""
    if per_day:
        basis_name, gross = min(per_day, key=lambda item: item[1])
        basis = (basis_name,)
        source: BudgetSource = "computed"
    else:
        if estimated is None:
            raise UnknownQuota(
                f"{quota.provider}/{quota.account}: no long window reported and no EstimatedBudget "
                f"declared — refusing to assume a daily budget"
            )
        gross = estimated.daily_pct
        basis = ()
        source = "declared_estimate"
        note = f"estimate declared by {estimated.declared_by}"

    committed = ledger.committed_pct(quota.account, key) if ledger is not None else 0.0
    return DailyBudget(
        provider=quota.provider,
        account=quota.account,
        budget_pct=max(0.0, gross - committed),
        gross_pct=gross,
        committed_pct=committed,
        source=source,
        basis=basis,
        note=note,
    )


def five_hour_admission(
    quota: ProviderQuota, *, now: datetime, policy: ResetPolicy | None = None
) -> FiveHourAdmission:
    """Can a launch start *right now*, as far as the short window is concerned?"""
    policy = policy or ResetPolicy()
    window = quota.five_hour

    if window.state == "unsupported":
        return FiveHourAdmission(True, "provider documents no five-hour window")
    if window.state == "missing":
        return FiveHourAdmission(
            True,
            "five-hour window not reported by the probe; the long-window daily budget governs",
            assumed=True,
        )

    remaining = float(window.remaining_pct)
    if remaining > 0.0:
        return FiveHourAdmission(True, f"five-hour window has {remaining:.1f}% left")

    if window.resets_at is None:
        return FiveHourAdmission(False, "five-hour window empty and no reset time known", needs_reprobe=True)

    resets_at = datetime.fromisoformat(window.resets_at)
    if (resets_at.tzinfo is None) != (now.tzinfo is None):
        # A provider may report `resets_at` with a UTC offset while the caller
        # works in naive America/Recife wall clock. Comparing the two raises
        # TypeError deep inside a morning run; say so here instead.
        raise ValueError(
            f"cannot compare five-hour reset {window.resets_at!r} with now={now.isoformat()!r}: "
            "one is timezone-aware and the other is naive; normalize both to America/Recife"
        )
    if now < resets_at:
        return FiveHourAdmission(False, f"five-hour window empty until {window.resets_at}")

    if policy.treat_expired_reset_as_refilled:
        return FiveHourAdmission(
            True,
            f"reset time {window.resets_at} passed; refill assumed per policy declared by {policy.declared_by}",
            needs_reprobe=True,
            assumed=True,
        )
    return FiveHourAdmission(
        False,
        f"reset time {window.resets_at} passed but the snapshot still reads empty; re-probe before launching",
        needs_reprobe=True,
    )


__all__ = [
    "BucketName",
    "DailyBudget",
    "EstimatedBudget",
    "FiveHourAdmission",
    "ProviderQuota",
    "QuotaExhausted",
    "QuotaWindow",
    "ResetPolicy",
    "UnknownQuota",
    "UsageLedger",
    "daily_budget",
    "five_hour_admission",
]
