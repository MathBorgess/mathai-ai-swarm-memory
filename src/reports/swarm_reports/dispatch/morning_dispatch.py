"""Morning handoff dispatch: quota, routing, durable claims, deferred visibility."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from swarm_reports.dispatch.claims import ClaimStore, DuplicateLaunch, RecoveryRequired
from swarm_reports.dispatch.policy_config import DispatchPolicy
from swarm_reports.dispatch.priority import Job, fit_to_budget
from swarm_reports.dispatch.providers import (
    KNOWN_CLIS,
    ProviderCLI,
    Runner,
    probe_model,
)
from swarm_reports.dispatch.quota import (
    EstimatedBudget,
    ProviderQuota,
    QuotaWindow,
    ResetPolicy,
    UsageLedger,
    daily_budget,
    five_hour_admission,
)
from swarm_reports.dispatch.routing import ProviderState, classify, route
from swarm_reports.dispatch.runtime_store import DeferredHandoff, RuntimeState, save_runtime
from swarm_reports.plan import HandoffCard


@dataclass(frozen=True)
class ProviderProbe:
    provider: str
    remaining_pct: float | None
    probed_at: datetime | None
    note: str = ""


@dataclass(frozen=True)
class MorningDispatchResult:
    launched: list[str]
    deferred: list[DeferredHandoff]
    dispatch_note: str
    costs_unknown: bool


def _handoff_jobs(handoffs: Sequence[HandoffCard], default_cost: float) -> list[Job]:
    jobs: list[Job] = []
    for card in handoffs:
        kind = "p0" if card.task_id.startswith("p0:") else "draft"
        jobs.append(Job(task_id=card.task_id, kind=kind, cost_pct=default_cost, estimated=True))
    return jobs


def _quota_from_probe(probe: ProviderProbe, account: str) -> ProviderQuota:
    if probe.remaining_pct is None:
        window = QuotaWindow.missing()
    else:
        window = QuotaWindow.reported(probe.remaining_pct)
    probed_at = (probe.probed_at or datetime.now(timezone.utc)).isoformat()
    return ProviderQuota(
        provider=probe.provider,
        account=account,
        five_hour=window,
        seven_day=window,
        monthly=QuotaWindow.unsupported(),
        probed_at=probed_at,
    )


def run_morning_handoff_dispatch(
    *,
    policy: DispatchPolicy,
    state_dir,
    day: str,
    handoffs: Sequence[HandoffCard],
    probes: Sequence[ProviderProbe],
    runner: Runner,
    now: datetime | None = None,
) -> MorningDispatchResult:
    """Admit handoffs under the daily budget, launch at most once per task_id."""
    now = now or datetime.now(timezone.utc)
    claims = ClaimStore(state_dir)
    ledger = UsageLedger(state_dir)

    states = [
        classify(
            p.provider,
            p.remaining_pct,
            probed_at=p.probed_at,
            now=now,
            low_threshold=policy.quota.low_threshold_pct,
            stale_after=timedelta(hours=policy.quota.stale_after_hours),
        )
        for p in probes
    ]
    if not states:
        states = [ProviderState(provider=name, remaining_pct=None) for name in policy.routing.order]

    # Budget from the most restrictive long window across probed providers.
    budgets: list[float] = []
    notes: list[str] = []
    costs_unknown = False
    for probe in probes:
        account = policy.quota.accounts.get(probe.provider, probe.provider)
        estimate = policy.quota.estimated_daily_budget_pct.get(probe.provider)
        est = (
            EstimatedBudget(daily_pct=estimate[0], declared_by=estimate[1]) if estimate else None
        )
        quota = _quota_from_probe(probe, account)
        try:
            five_hour_admission(
                quota,
                now=now,
                policy=ResetPolicy(
                    treat_expired_reset_as_refilled=policy.quota.treat_expired_reset_as_refilled,
                    declared_by=policy.quota.reset_declared_by or None,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - admission failure is a note, not a crash
            notes.append(f"{probe.provider}: five_hour blocked ({exc})")
        try:
            budget = daily_budget(
                quota,
                days_remaining={"seven_day": 3, "monthly": 14},
                now=now,
                ledger=ledger,
                date_key=day,
                estimated=est,
            )
            budgets.append(budget.budget_pct)
            if budget.estimated:
                costs_unknown = True
            notes.append(f"{probe.provider}: {budget.note}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{probe.provider}: no budget ({exc})")
            costs_unknown = True

    budget_pct = min(budgets) if budgets else 0.0
    fit = fit_to_budget(_handoff_jobs(handoffs, policy.handoff.default_cost_pct), budget_pct)

    launched: list[str] = []
    deferred: list[DeferredHandoff] = []
    title_by_id = {h.task_id: h.title for h in handoffs}

    for job in fit.deferred:
        deferred.append(
            DeferredHandoff(
                task_id=job.task_id,
                title=title_by_id.get(job.task_id, job.task_id),
                reason="quota diária insuficiente",
            )
        )

    parent = policy.routing.order[0] if policy.routing.order else "cursor"
    admitted_ids = [job.task_id for job in fit.admitted]
    routing = route(
        admitted_ids,
        states,
        parent=parent,
        order=policy.routing.order,
        low_threshold=policy.quota.low_threshold_pct,
    )
    for job in fit.admitted:
        card = next((h for h in handoffs if h.task_id == job.task_id), None)
        if card is None:
            continue
        provider = routing.get(job.task_id)
        if not provider:
            deferred.append(DeferredHandoff(job.task_id, card.title, "nenhum provider elegível"))
            continue
        try:
            claim = claims.try_claim(day, job.task_id, provider, now=now)
        except (DuplicateLaunch, RecoveryRequired):
            continue
        cli = KNOWN_CLIS.get(provider)
        if cli is None:
            claims.mark_failed(
                day,
                job.task_id,
                f"unknown provider {provider}",
                attempt=claim.attempt,
                now=now,
            )
            deferred.append(
                DeferredHandoff(job.task_id, card.title, f"provider desconhecido: {provider}")
            )
            continue
        model = probe_model(cli, runner) if policy.routing.model_probe.get(provider, False) else "default"
        if policy.handoff.command is None:
            claims.mark_done(day, job.task_id, attempt=claim.attempt, now=now)
            launched.append(job.task_id)
            notes.append(f"{job.task_id}: handoff sem comando configurado (registrado apenas)")
            continue
        payload = {
            "day": day,
            "provider": provider,
            "model": model,
            "task_id": job.task_id,
            "prompt": card.copy_prompt or card.objective,
        }
        argv = list(policy.handoff.command)
        result = runner.run(argv, stdin=json.dumps(payload), timeout=120.0)
        if result.timed_out or result.returncode != 0:
            claims.mark_failed(
                day,
                job.task_id,
                f"exit {result.returncode}: {result.stderr[:200]}",
                attempt=claim.attempt,
                now=now,
            )
            deferred.append(DeferredHandoff(job.task_id, card.title, "falha no adaptador"))
            continue
        claims.mark_done(day, job.task_id, attempt=claim.attempt, now=now)
        launched.append(job.task_id)

    runtime = RuntimeState(
        deferred=deferred,
        dispatch_note="; ".join(notes)[:2000],
        costs_unknown=costs_unknown,
    )
    save_runtime(state_dir, runtime)
    return MorningDispatchResult(
        launched=launched,
        deferred=deferred,
        dispatch_note=runtime.dispatch_note,
        costs_unknown=costs_unknown,
    )


__all__ = ["MorningDispatchResult", "ProviderProbe", "run_morning_handoff_dispatch"]
