"""Load `reports-policy.json` into typed structures for runtime integration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from swarm_reports.dispatch.merge_policy import RepoPolicy, TauIntentState


@dataclass(frozen=True)
class QuotaPolicy:
    low_threshold_pct: float
    stale_after_hours: float
    accounts: dict[str, str]
    estimated_daily_budget_pct: dict[str, tuple[float, str]]
    treat_expired_reset_as_refilled: bool
    reset_declared_by: str
    probes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingPolicy:
    order: tuple[str, ...]
    model_probe: dict[str, bool]


@dataclass(frozen=True)
class DigestPolicy:
    max_cards_per_pr: int
    top_n: int
    allow_external_comments: bool
    reviewer: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HandoffPolicy:
    default_cost_pct: float
    #: argv prefix for every launch; stdin receives JSON `{provider, model, task_id, prompt}`.
    command: tuple[str, ...] | None
    providers: dict = field(default_factory=dict)
    repo_dir: str | None = None
    timeout_seconds: float = 120


@dataclass(frozen=True)
class DispatchPolicy:
    state_dir: Path
    signal_max_age_hours: float
    repos: tuple[RepoPolicy, ...]
    tau_intent: TauIntentState | None
    quota: QuotaPolicy
    routing: RoutingPolicy
    digest: DigestPolicy
    handoff: HandoffPolicy

    def repo_policy(self, repo: str) -> RepoPolicy | None:
        for policy in self.repos:
            if policy.repo == repo:
                return policy
        return None


def _repo_from_json(entry: dict[str, Any]) -> RepoPolicy:
    return RepoPolicy(
        repo=str(entry["repo"]),
        kind=str(entry.get("kind") or "code"),
        allowed_paths=tuple(entry.get("allowed_paths") or ()),
        protected_paths=tuple(entry.get("protected_paths") or ()),
        skills_paths=tuple(entry.get("skills_paths") or ()),
        weights_paths=tuple(entry.get("weights_paths") or ()),
        is_tau_intent=bool(entry.get("is_tau_intent")),
    )


def _tau_from_json(raw: dict[str, Any] | None) -> TauIntentState | None:
    if not raw:
        return None
    activated = raw.get("activated_on")
    return TauIntentState(
        activated_on=date.fromisoformat(activated) if activated else None,
        digest_proved=bool(raw.get("digest_proved")),
        digest_evidence=str(raw.get("digest_evidence") or ""),
    )


def load_dispatch_policy(path: Path) -> DispatchPolicy:
    if not path.is_absolute():
        raise ValueError("dispatch policy path must be absolute")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("dispatch policy root must be an object")

    quota_raw = data.get("quota") or {}
    estimates: dict[str, tuple[float, str]] = {}
    for provider, value in (quota_raw.get("estimated_daily_budget_pct") or {}).items():
        if isinstance(value, (int, float)):
            estimates[str(provider)] = (float(value), f"config:{provider}")
        elif isinstance(value, dict):
            pct = value.get("pct")
            declared = value.get("declared_by")
            if pct is None or not declared:
                raise ValueError(f"estimated_daily_budget_pct[{provider}] needs pct and declared_by")
            estimates[str(provider)] = (float(pct), str(declared))

    reset_raw = quota_raw.get("five_hour_reset_policy") or {}
    handoff_raw = data.get("handoff") or {}
    cmd = handoff_raw.get("command")
    handoff_cmd: tuple[str, ...] | None = None
    if cmd is not None:
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ValueError("handoff.command must be a non-empty argv list")
        handoff_cmd = tuple(cmd)

    digest_raw = data.get("digest") or {}
    routing_raw = data.get("routing") or {}

    return DispatchPolicy(
        state_dir=Path(str(data.get("state_dir") or "")).expanduser(),
        signal_max_age_hours=float(data.get("signal_max_age_hours") or 6),
        repos=tuple(_repo_from_json(entry) for entry in data.get("repos") or []),
        tau_intent=_tau_from_json(data.get("tau_intent")),
        quota=QuotaPolicy(
            low_threshold_pct=float(quota_raw.get("low_threshold_pct") or 20.0),
            stale_after_hours=float(quota_raw.get("stale_after_hours") or 6),
            accounts={str(k): str(v) for k, v in (quota_raw.get("accounts") or {}).items()},
            estimated_daily_budget_pct=estimates,
            treat_expired_reset_as_refilled=bool(
                reset_raw.get("treat_expired_reset_as_refilled")
            ),
            reset_declared_by=str(reset_raw.get("declared_by") or ""),
            probes=dict(quota_raw.get("probes") or {}),
        ),
        routing=RoutingPolicy(
            order=tuple(routing_raw.get("order") or ("cursor", "claude", "codex")),
            model_probe={str(k): bool(v) for k, v in (routing_raw.get("model_probe") or {}).items()},
        ),
        digest=DigestPolicy(
            max_cards_per_pr=int(digest_raw.get("max_cards_per_pr") or 3),
            top_n=int(digest_raw.get("top_n") or 5),
            allow_external_comments=bool(digest_raw.get("allow_external_comments")),
            reviewer=dict(digest_raw.get("reviewer") or {}),
        ),
        handoff=HandoffPolicy(
            default_cost_pct=float(handoff_raw.get("default_cost_pct") or 5.0),
            command=handoff_cmd,
            providers=dict(handoff_raw.get("providers") or {}),
            repo_dir=handoff_raw.get("repo_dir"),
            timeout_seconds=float(handoff_raw.get("timeout_seconds", 120)),
        ),
    )


__all__ = [
    "DispatchPolicy",
    "DigestPolicy",
    "HandoffPolicy",
    "QuotaPolicy",
    "RoutingPolicy",
    "load_dispatch_policy",
]
