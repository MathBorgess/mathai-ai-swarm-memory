"""Quota admission, durable reservations and one launch per frozen handoff."""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .claims import ClaimStore, DuplicateLaunch
from .policy_config import DispatchPolicy
from .providers import KNOWN_CLIS, Runner, SubprocessRunner, provider_argv
from .quota import EstimatedBudget, ProviderQuota, QuotaWindow, UsageLedger, daily_budget, five_hour_admission, ResetPolicy, QuotaExhausted
from .routing import ProviderState, route
from .runtime_store import DeferredHandoff, load_runtime, save_runtime
from swarm_reports.plan import HandoffCard

@dataclass(frozen=True)
class ProviderProbe:
    """Legacy injected probe: a percentage without window/reset facts stays unknown."""
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


def _quota_from_probe(probe: ProviderProbe, account: str, now: datetime) -> ProviderQuota:
    """Test harness only: a lone percentage is not a real probe snapshot."""
    probed_at = (probe.probed_at or now).isoformat()
    if probe.remaining_pct is None:
        missing = QuotaWindow.missing()
        return ProviderQuota(probe.provider, account, missing, missing, missing, probed_at)
    reset = (now + timedelta(days=7)).isoformat()
    window = QuotaWindow.reported(probe.remaining_pct, reset)
    return ProviderQuota(
        probe.provider,
        account,
        window,
        window,
        QuotaWindow.unsupported(),
        probed_at,
    )


def run_morning_handoff_dispatch(*, policy: DispatchPolicy, state_dir, day: str,
                                handoffs, probes, runner: Runner, now=None, p0_ids=()):
    now = now or datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    claims, ledger = ClaimStore(state_dir), UsageLedger(state_dir)
    budgets, states, notes = {}, [], []
    for quota in probes:
        if not isinstance(quota, ProviderQuota):
            account = policy.quota.accounts.get(quota.provider, quota.provider)
            quota = _quota_from_probe(quota, account, now)
        provider = quota.provider
        try:
            stamp = datetime.fromisoformat(quota.probed_at)
            if stamp.tzinfo is None or stamp > now + timedelta(minutes=1) or now - stamp > timedelta(hours=policy.quota.stale_after_hours):
                raise ValueError("stale quota; re-probe")
            gate = five_hour_admission(quota, now=now, policy=ResetPolicy(
                policy.quota.treat_expired_reset_as_refilled, policy.quota.reset_declared_by))
            if not gate.admit:
                raise ValueError("five-hour blocked; re-probe")
            days = {}
            for name in ("seven_day", "monthly"):
                window = getattr(quota, name)
                if window.state == "reported":
                    if not window.resets_at:
                        raise ValueError("reported long window requires reset timestamp")
                    remaining = (datetime.fromisoformat(window.resets_at) - now).total_seconds() / 86400
                    if remaining <= 0:
                        raise ValueError("expired long window; re-probe")
                    days[name] = max(1, remaining)
            estimate = policy.quota.estimated_daily_budget_pct.get(provider)
            budget = daily_budget(quota, days_remaining=days, now=now, ledger=ledger,
                date_key=day, estimated=EstimatedBudget(*estimate) if estimate else None)
            budgets[provider] = budget
            values = [w.remaining_pct for w in (quota.five_hour, quota.seven_day, quota.monthly) if w.state == "reported"]
            states.append(ProviderState(provider, min(values) if values else None, stamp))
            notes.append(f"{provider}: {budget.source}; {budget.budget_pct:.2f}% diário disponível; custo estimado")
        except Exception as exc:
            # Deliberately omit provider stderr and snapshot bodies.
            notes.append(f"{provider}: quota indisponível/bloqueada ({type(exc).__name__})")
    launched, deferred, routed_ids = [], [], []
    # Actual P0 membership, then drafts, then explicit improvement cards.
    ordered = sorted(enumerate(handoffs), key=lambda pair: (
        0 if pair[1].task_id in p0_ids else 2 if pair[1].task_id.startswith("improvement:") else 1, pair[0]))
    for _, card in ordered:
        if claims.load(day, card.task_id) is not None:
            # Failed/uncertain launches also require reconciliation, never blind retry.
            continue
        cost = policy.handoff.default_cost_pct
        usable = [s for s in states if budgets[s.provider].gross_pct - ledger.committed_pct(budgets[s.provider].account, day) >= cost]
        assignments = route(routed_ids + [card.task_id], usable,
            parent=policy.routing.order[-1], order=policy.routing.order,
            low_threshold=policy.quota.low_threshold_pct) if usable else {}
        provider = assignments.get(card.task_id)
        if not provider:
            deferred.append(DeferredHandoff(card.task_id, card.title, "quota diária insuficiente ou desconhecida"))
            continue
        spec = policy.handoff.providers.get(provider)
        if not policy.handoff.command and not spec:
            deferred.append(DeferredHandoff(card.task_id, card.title, "comando do provider não configurado"))
            continue
        argv = list(policy.handoff.command or ())
        payload = json.dumps({"day": day, "provider": provider, "model": "default",
                              "task_id": card.task_id, "prompt": card.copy_prompt or card.objective})
        if spec:
            if provider not in KNOWN_CLIS or not policy.handoff.repo_dir:
                deferred.append(DeferredHandoff(card.task_id, card.title, "repo_dir/provider não configurado"))
                continue
            argv = provider_argv(provider, spec.get("command"))
            payload = "Execute only this handoff in your isolated worktree. No deploy, merge or external publication. " + payload
        budget = budgets[provider]
        try:
            reservation = ledger.reserve(budget.account, day, cost, budget_pct=budget.gross_pct)
        except QuotaExhausted:
            deferred.append(DeferredHandoff(card.task_id, card.title, "quota reservada por outra execução"))
            continue
        try:
            claim = claims.try_claim(day, card.task_id, provider, now=now)
        except DuplicateLaunch:
            ledger.refund(reservation)
            continue
        active_runner = runner
        if spec and isinstance(runner, SubprocessRunner):
            from .statefile import state_key
            workspace = state_dir / "jobs" / state_key(day, card.task_id)
            workspace.parent.mkdir(parents=True, exist_ok=True)
            setup = runner.run(["git", "-C", policy.handoff.repo_dir, "worktree", "add", "--detach", str(workspace), "HEAD"], stdin=None, timeout=30)
            if setup.returncode:
                ledger.refund(reservation)
                claims.mark_failed(day, card.task_id, "workspace setup failed", attempt=claim.attempt, now=now)
                deferred.append(DeferredHandoff(card.task_id, card.title, "falha no worktree isolado"))
                continue
            active_runner = SubprocessRunner(cwd=workspace)
        # Spend remains reserved/debited on crashes and errors: upstream may already have charged.
        result = active_runner.run(argv, stdin=payload, timeout=policy.handoff.timeout_seconds)
        ledger.debit(reservation)
        if result.timed_out or result.returncode:
            claims.mark_failed(day, card.task_id, "provider failed; reconcile before retry", attempt=claim.attempt, now=now)
            deferred.append(DeferredHandoff(card.task_id, card.title, "falha no provider; reconciliar execução"))
            continue
        claims.mark_done(day, card.task_id, attempt=claim.attempt, now=now)
        launched.append(card.task_id)
        routed_ids.append(card.task_id)
        from swarm_reports.evening.ledger import LedgerStore, LedgerRecord, entry_id_for
        LedgerStore(state_dir).record(LedgerRecord(entry_id_for(day, 0, "handoff", card.task_id),
            day, 0, "handoff", card.task_id, "completed", f"Handoff concluído: {card.title}"))
    runtime = load_runtime(state_dir)
    runtime.deferred = deferred
    runtime.dispatch_note = "; ".join(notes)[:2000]
    runtime.costs_unknown = True  # per-task consumption is an explicit estimate, even with measured quota
    save_runtime(state_dir, runtime)
    return MorningDispatchResult(launched, deferred, runtime.dispatch_note, True)
