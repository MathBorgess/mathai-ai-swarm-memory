"""Freeze the daily checklist in an isolated wiki git worktree (branch + commit).

Invariants this module owes the rest of the cycle:

- The live vault checkout is never touched, so uncommitted owner text stays private
  and unread. The worktree starts from `origin/main` after an explicit fetch; a fetch
  failure aborts instead of quietly freezing against a stale base.
- One deterministic worktree per day, and the branch is verified after checkout. A
  single shared path reused across days will sooner or later commit onto the wrong branch.
- The frozen commit is immutable external state. The marker in the note carries the
  snapshot id only; the real sha is returned and recorded in `reports-state.json`.
  A marker that tried to name its own commit could only ever name an unreachable one.
- A missing daily note is created from the vault template, because a morning with no
  note is normal (the owner has not opened the day yet) and must not crash the run.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from swarm_reports.metrics.daily import FROZEN_BEGIN
from swarm_reports.metrics.state import FrozenItem
from swarm_reports.wiki.publish import PublishOutcome, PublishRequest, WikiPublisher

FROZEN_END = re.compile(r"<!--\s*swarm:frozen-checklist-end\s*-->", re.IGNORECASE)
HOJE_HEADING = re.compile(r"^##\s+Hoje\s*$", re.MULTILINE | re.IGNORECASE)
TEMPLATE_REL = "daily/_template.md"


@dataclass(frozen=True)
class FreezeResult:
    applied: bool
    commit_sha: str | None
    snapshot: str
    branch: str
    daily_path: Path
    worktree: Path | None = None
    base_ref: str | None = None
    publish: PublishOutcome | None = None


def freeze_branch_name(day: date) -> str:
    return f"codex/reports-freeze-{day.isoformat()}"


def freeze_worktree_path(worktree_parent: Path, day: date) -> Path:
    """One absolute path per day; never shared between branches."""
    return (worktree_parent / f"freeze-{day.isoformat()}").resolve()


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_ok(cwd: Path, *args: str) -> bool:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _daily_rel(day: date) -> str:
    return f"daily/{day.isoformat()}.md"


def _has_frozen_block(text: str) -> bool:
    return bool(FROZEN_BEGIN.search(text))


def _build_frozen_block(items: list[FrozenItem], *, snapshot: str) -> str:
    lines = [f"<!-- swarm:frozen-checklist-begin snapshot={snapshot} -->"]
    for item in items:
        lines.append(
            f"<!-- swarm:task-meta id={item.task_id} "
            f"first_planned={item.first_planned.isoformat()} frozen=true -->"
        )
        suffix = " <!-- swarm:p0 -->" if item.is_p0 else ""
        body = item.text
        if item.is_p0 and "P0" not in body.upper():
            body = f"**P0** {body}"
        lines.append(f"- [ ] {body}{suffix}")
    lines.append("<!-- swarm:frozen-checklist-end -->")
    return "\n".join(lines) + "\n"


def _inject_hoje_block(body: str, block: str) -> str:
    if _has_frozen_block(body):
        return body
    match = HOJE_HEADING.search(body)
    if not match:
        raise ValueError("daily note missing ## Hoje section")
    insert_at = match.end()
    prefix = body[:insert_at]
    if not prefix.endswith("\n"):
        prefix += "\n"
    rest = body[insert_at:]
    if rest and not rest.startswith("\n"):
        rest = "\n" + rest
    return prefix + "\n" + block + rest


def render_daily_from_template(template: str | None, day: date) -> str:
    """New-day note from the vault template, keeping its sections and order."""
    if template is None:
        return f"# {day.isoformat()}\n\n## Hoje\n\n## Evolução\n\n- \n\n## Amanhã (opcional)\n\n- \n"
    body = template.replace("{{date}}", day.isoformat())
    if not HOJE_HEADING.search(body):
        raise ValueError(f"{TEMPLATE_REL} has no ## Hoje section")
    if not body.endswith("\n"):
        body += "\n"
    return body


def prepare_worktree(
    wiki_dir: Path,
    day: date,
    wt_path: Path,
    branch: str,
    *,
    local_only: bool,
    remote: str,
) -> str:
    """Create or reuse the per-day worktree and return the base ref it started from.

    Shared with the F4 night session: both halves of the cycle must fetch before they
    edit and must never touch the live checkout.
    """
    if local_only:
        base = run_git(wiki_dir, "rev-parse", "HEAD")
        base_ref = "HEAD"
    else:
        if not git_ok(wiki_dir, "remote", "get-url", remote):
            raise RuntimeError(
                f"wiki has no '{remote}' remote; pass local_only=True only for tests"
            )
        # A silent fetch failure would freeze against a stale base and mislabel the
        # snapshot as isolated from origin/main. Governance requires a hard failure.
        run_git(wiki_dir, "fetch", "--quiet", remote, "main")
        base_ref = f"{remote}/main"
        base = run_git(wiki_dir, "rev-parse", base_ref)

    branch_exists = git_ok(wiki_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")

    if wt_path.exists():
        head = run_git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        if head != branch:
            raise RuntimeError(
                f"freeze worktree {wt_path} is on '{head}', expected '{branch}'"
            )
    else:
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        if branch_exists:
            # Reuse the existing freeze branch; `-B` here would reset away a real commit.
            run_git(wiki_dir, "worktree", "add", str(wt_path), branch)
        else:
            run_git(wiki_dir, "worktree", "add", "-b", branch, str(wt_path), base)
        head = run_git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        if head != branch:
            raise RuntimeError(f"freeze worktree checked out '{head}', expected '{branch}'")
    return base_ref


def apply_wiki_freeze(
    wiki_dir: Path,
    day: date,
    items: list[FrozenItem],
    *,
    timezone: str,
    dry_run: bool = False,
    worktree_parent: Path | None = None,
    local_only: bool = False,
    remote: str = "origin",
    known_commit: str | None = None,
    known_snapshot: str | None = None,
    publisher: WikiPublisher | None = None,
) -> FreezeResult:
    """Freeze `items` for `day`. Idempotent, and recoverable from `known_commit`."""
    daily_rel = _daily_rel(day)
    branch = freeze_branch_name(day)
    tz = ZoneInfo(timezone)
    snapshot = known_snapshot or datetime.now(tz).replace(microsecond=0).isoformat()

    if dry_run:
        return FreezeResult(
            applied=True,
            commit_sha=None,
            snapshot=snapshot,
            branch=branch,
            daily_path=wiki_dir / daily_rel,
        )

    parent = worktree_parent or (wiki_dir.parent / ".wiki-freeze")
    wt_path = freeze_worktree_path(parent, day)
    base_ref = prepare_worktree(
        wiki_dir, day, wt_path, branch, local_only=local_only, remote=remote
    )

    wt_daily = wt_path / daily_rel
    if wt_daily.exists():
        body = wt_daily.read_text(encoding="utf-8")
    else:
        template_path = wt_path / TEMPLATE_REL
        template = template_path.read_text(encoding="utf-8") if template_path.is_file() else None
        body = render_daily_from_template(template, day)
        wt_daily.parent.mkdir(parents=True, exist_ok=True)
        wt_daily.write_text(body, encoding="utf-8")

    if _has_frozen_block(body):
        head = run_git(wt_path, "rev-parse", "HEAD")
        recovered = known_commit or head
        if known_commit and known_commit != head:
            # Recover the exact recorded snapshot rather than trusting branch HEAD.
            run_git(wt_path, "checkout", "--quiet", known_commit, "--", daily_rel)
            recovered = known_commit
        return FreezeResult(
            applied=False,
            commit_sha=recovered,
            snapshot=snapshot,
            branch=branch,
            daily_path=wt_daily,
            worktree=wt_path,
            base_ref=base_ref,
        )

    block = _build_frozen_block(items, snapshot=snapshot)
    wt_daily.write_text(_inject_hoje_block(body, block), encoding="utf-8")

    run_git(wt_path, "add", "--", daily_rel)
    if git_ok(wt_path, "diff", "--cached", "--quiet"):
        raise RuntimeError("freeze produced no staged changes")
    run_git(wt_path, "commit", "--quiet", "-m", f"reports: freeze morning checklist {day.isoformat()}")
    commit_sha = run_git(wt_path, "rev-parse", "HEAD")

    outcome: PublishOutcome | None = None
    if publisher is not None:
        outcome = publisher.publish(
            PublishRequest(
                wiki_dir=wiki_dir,
                worktree=wt_path,
                branch=branch,
                day=day,
                commit_sha=commit_sha,
                title=f"reports: freeze morning checklist {day.isoformat()}",
                body=(
                    f"Frozen `## Hoje` checklist for {day.isoformat()} "
                    f"(snapshot `{snapshot}`, {len(items)} items).\n"
                ),
            )
        )

    return FreezeResult(
        applied=True,
        commit_sha=commit_sha,
        snapshot=snapshot,
        branch=branch,
        daily_path=wt_daily,
        worktree=wt_path,
        base_ref=base_ref,
        publish=outcome,
    )
