"""Mutation harness: break one guard at a time, assert the suite notices.

Not a test. Run it by hand (`python3 mutation_check.py`) to check that the
dispatch tests fail for the right reasons instead of passing vacuously.
Each entry names the invariant whose removal must turn the suite red.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PKG = ROOT / "swarm_reports" / "dispatch"

# (invariant, file, old snippet, new snippet)
MUTATIONS: list[tuple[str, Path, str, str]] = [
    (
        "merge_policy: T0 is suggestion-only",
        PKG / "merge_policy.py",
        'if "t0" in tiers:\n        return MergeDecision(\n            "suggestion_only",',
        'if False:\n        return MergeDecision(\n            "suggestion_only",',
    ),
    (
        "merge_policy: allowlist applies to every path",
        PKG / "merge_policy.py",
        "outside = [p for p in paths if not matches_any(p, policy.allowed_paths)]",
        "outside = []",
    ),
    (
        "merge_policy: unknown tier fails closed",
        PKG / "merge_policy.py",
        'if "unknown" in tiers:',
        "if False:",
    ),
    (
        "merge_policy: lint/CI signal must match the current head",
        PKG / "merge_policy.py",
        "if not signal.head_sha or signal.head_sha != current_head_sha:",
        "if False:",
    ),
    (
        "merge_policy: signal staleness blocks",
        PKG / "merge_policy.py",
        "if age > max_age:",
        "if False:",
    ),
    (
        "merge_policy: a missing signal blocks",
        PKG / "merge_policy.py",
        "if signal is None:\n        return f\"{label} signal missing\"",
        "if signal is None:\n        return None",
    ),
    (
        "merge_policy: renames classify the old path too",
        PKG / "merge_policy.py",
        "if file.previous_filename:\n        pairs.append((normalize_path(file.previous_filename), \"removed\"))",
        "if False:\n        pairs.append((normalize_path(file.previous_filename), \"removed\"))",
    ),
    (
        "merge_policy: malformed paths are rejected, not normalized",
        PKG / "merge_policy.py",
        'if "\\\\" in path:\n        raise MalformedPath(f"backslash in path: {path!r}")',
        'if False:\n        raise MalformedPath(f"backslash in path: {path!r}")',
    ),
    (
        "merge_policy: tau-intent needs an activation date",
        PKG / "merge_policy.py",
        "if state.activated_on is None:",
        "if False:",
    ),
    (
        "merge_policy: tau-intent needs digest evidence after the hold",
        PKG / "merge_policy.py",
        "if not state.digest_proved or not state.digest_evidence.strip():",
        "if False:",
    ),
    (
        "merge_policy: existing fontes/ edit is T0",
        PKG / "merge_policy.py",
        'return "t2" if status in ("added", "copied") else "t0"',
        'return "t2"',
    ),
    (
        "merge_policy: deletion is T0",
        PKG / "merge_policy.py",
        'if status == "removed":\n        return "t0"',
        'if False:\n        return "t0"',
    ),
    (
        "claims: a completion may only close its own attempt",
        PKG / "claims.py",
        "if attempt != claim.attempt:",
        "if False:",
    ),
    (
        "claims: a live process blocks a relaunch",
        PKG / "claims.py",
        "if alive is True:",
        "if False:",
    ),
    (
        "claims: an unverifiable process needs operator takeover",
        PKG / "claims.py",
        "if alive is None and not allow_takeover:",
        "if False:",
    ),
    (
        "claims: a done claim blocks a relaunch",
        PKG / "claims.py",
        'if existing.status == "done":',
        "if False:",
    ),
    (
        "claims: on-disk state is type-validated",
        PKG / "claims.py",
        "if status not in _STATUSES:",
        "if False:",
    ),
    (
        "statefile: task ids are hashed, not escaped",
        PKG / "statefile.py",
        'digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:32]',
        'digest = task_id.replace("/", "_")',
    ),
    (
        "statefile: state writes are 0600",
        PKG / "statefile.py",
        "os.fchmod(fd, 0o600)",
        "os.fchmod(fd, 0o644)",
    ),
    (
        "quota: budget is net of what the ledger already committed",
        PKG / "quota.py",
        "budget_pct=max(0.0, gross - committed),",
        "budget_pct=gross,",
    ),
    (
        "quota: no long window and no declared estimate raises",
        PKG / "quota.py",
        "if estimated is None:\n            raise UnknownQuota(",
        "if False:\n            raise UnknownQuota(",
    ),
    (
        "quota: reservations cannot exceed the budget",
        PKG / "quota.py",
        "if used + amount_pct > budget_pct + 1e-9:",
        "if False:",
    ),
    (
        "quota: the tighter long window governs",
        PKG / "quota.py",
        "basis_name, gross = min(per_day, key=lambda item: item[1])",
        "basis_name, gross = max(per_day, key=lambda item: item[1])",
    ),
    (
        "quota: an expired five-hour reset is not a refill",
        PKG / "quota.py",
        "if policy.treat_expired_reset_as_refilled:",
        "if True:",
    ),
    (
        "quota: an empty five-hour window blocks before its reset",
        PKG / "quota.py",
        "if now < resets_at:",
        "if False:",
    ),
    (
        "quota: a refund cannot erase a debited reservation",
        PKG / "quota.py",
        'if entries[rid]["state"] == "debited":',
        "if False:",
    ),
    (
        "quota: a retried debit does not double-charge",
        PKG / "quota.py",
        'if entry["state"] == "debited":\n                return',
        'if False:\n                return',
    ),
    (
        "gh_adapter: a merge is head-pinned",
        PKG / "gh_adapter.py",
        'if self.op == "merge" and not self.expected_head_oid:',
        "if False:",
    ),
    (
        "gh_adapter: --match-head-commit is in the argv",
        PKG / "gh_adapter.py",
        '"--match-head-commit",\n        str(request.expected_head_oid),',
        "",
    ),
    (
        "gh_adapter: no unsolicited external comment",
        PKG / "gh_adapter.py",
        "if allow_external_comment:",
        "if True:",
    ),
    (
        "digest: card locations are verified against the diff",
        PKG / "digest.py",
        "ok = any(start <= line <= end for start, end in ranges)",
        "ok = True",
    ),
    (
        "digest: an unverified card gets no URL",
        PKG / "digest.py",
        "if not card.verified_line:\n        return None",
        "if False:\n        return None",
    ),
    (
        "digest: at most 3 cards per PR",
        PKG / "digest.py",
        "return ordered[:max_per_pr]",
        "return ordered",
    ),
    (
        "digest: card kinds are schema-validated",
        PKG / "digest.py",
        "if not isinstance(kind, str) or kind.lower() not in _KIND_PRIORITY:",
        "if False:",
    ),
    (
        "priority: no backfill past the first job that does not fit",
        PKG / "priority.py",
        "if full or spent + job.cost_pct > budget_pct + 1e-9:",
        "if spent + job.cost_pct > budget_pct + 1e-9:",
    ),
    (
        "priority: P0 before drafts before improvements",
        PKG / "priority.py",
        "ordered = sorted(jobs, key=lambda j: _ORDER[j.kind])",
        "ordered = list(jobs)",
    ),
    (
        "providers: model-list headers are not parsed as ids",
        PKG / "providers.py",
        "if any(hint in lowered for hint in _HEADER_HINTS):",
        "if False:",
    ),
    (
        "providers: a CLI with no model surface is never probed",
        PKG / "providers.py",
        "if not cli.can_probe_models:\n        return DEFAULT_MODEL",
        "if False:\n        return DEFAULT_MODEL",
    ),
    (
        "providers: secrets are redacted from CLI output",
        PKG / "providers.py",
        "out = pattern.sub(REDACTED, out)",
        "out = out",
    ),
    (
        "routing: an empty provider is never eligible",
        PKG / "routing.py",
        'if state.remaining_pct <= 0:\n        return "empty"',
        'if False:\n        return "empty"',
    ),
    (
        "routing: a stale probe is treated as unknown",
        PKG / "routing.py",
        "if now - probed_at > stale_after:",
        "if False:",
    ),
]


def run_suite() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    if not run_suite():
        print("BASELINE IS RED — fix the suite before mutation testing")
        return 2

    survivors: list[str] = []
    skipped: list[str] = []
    for name, path, old, new in MUTATIONS:
        original = path.read_text()
        if original.count(old) != 1:
            skipped.append(f"{name} (snippet matched {original.count(old)}x)")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            caught = not run_suite()
        finally:
            path.write_text(original)
        print(f"{'caught  ' if caught else 'SURVIVED'}  {name}")
        if not caught:
            survivors.append(name)

    print(f"\n{len(MUTATIONS) - len(skipped) - len(survivors)}/{len(MUTATIONS) - len(skipped)} mutations caught")
    for entry in skipped:
        print(f"skipped: {entry}")
    for entry in survivors:
        print(f"SURVIVED: {entry}")
    return 1 if survivors or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
