"""Configured read-only quota transports. No credential scraping or guessed endpoints."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from .quota import ProviderQuota, QuotaWindow


def _stamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("quota timestamps require a timezone")
    return stamp.isoformat()


def read_probe(provider, account, spec, runner):
    """file/command JSON, or Claude statusline JSON captured by an operator hook.

    Canonical format: probed_at and five_hour/seven_day/monthly objects with
    state, remaining_pct, resets_at. Missing fields remain unknown.
    """
    missing = QuotaWindow.missing()
    if not spec:
        return ProviderQuota(provider, account, missing, missing, missing, datetime.now(timezone.utc).isoformat())
    if spec.get("path"):
        path = Path(spec["path"])
        if not path.is_absolute() or path.stat().st_size > 1_000_000:
            raise ValueError("quota path must be absolute and bounded")
        raw = json.loads(path.read_text())
        fallback_stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    elif spec.get("command"):
        cmd = spec["command"]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ValueError("quota probe command must be argv")
        out = runner.run(cmd, stdin=None, timeout=min(60, max(1, spec.get("timeout_seconds", 15))))
        if out.returncode or out.timed_out:
            raise ValueError("quota probe failed")
        raw = json.loads(out.stdout)
        fallback_stamp = None
    else:
        raise ValueError("quota probe requires path or command")
    stamp = _stamp(raw.get("probed_at") or fallback_stamp)
    windows = {}
    for name in ("five_hour", "seven_day", "monthly"):
        if spec.get("format") == "claude-statusline":
            item = (raw.get("rate_limits") or {}).get(name)
            if name == "monthly":
                windows[name] = QuotaWindow.unsupported()
                continue
            windows[name] = (QuotaWindow.reported(100 - float(item["used_percentage"]),
                              _stamp(item["resets_at"]) if item.get("resets_at") else None)
                             if item else missing)
        else:
            item = raw.get(name) or {"state": "missing"}
            windows[name] = QuotaWindow(state=item.get("state", "reported"),
                remaining_pct=item.get("remaining_pct"),
                resets_at=_stamp(item["resets_at"]) if item.get("resets_at") else None)
    return ProviderQuota(provider, account, probed_at=stamp, **windows)


def configured_probes(policy, runner):
    probes = []
    for provider in policy.routing.order:
        account = policy.quota.accounts.get(provider, provider)
        try:
            probes.append(read_probe(provider, account, policy.quota.probes.get(provider), runner))
        except (ValueError, KeyError, TypeError, OSError):
            probes.append(read_probe(provider, account, None, runner))
    return probes
