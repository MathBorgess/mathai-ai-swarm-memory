"""Freeze daily checklist in an isolated wiki git worktree (branch + commit)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from swarm_reports.metrics.daily import FROZEN_BEGIN, parse_daily_markdown
from swarm_reports.metrics.state import FrozenItem

FROZEN_END = re.compile(r"<!--\s*swarm:frozen-checklist-end\s*-->", re.IGNORECASE)
HOJE_HEADING = re.compile(r"^##\s+Hoje\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class FreezeResult:
    applied: bool
    commit_sha: str | None
    snapshot: str
    branch: str
    daily_path: Path


def freeze_branch_name(day: date) -> str:
    return f"codex/reports-freeze-{day.isoformat()}"


def _run_git(cwd: Path, *args: str) -> str:
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


def _daily_rel(day: date) -> str:
    return f"daily/{day.isoformat()}.md"


def _has_frozen_block(text: str) -> bool:
    return bool(FROZEN_BEGIN.search(text))


def _build_frozen_block(
    items: list[FrozenItem],
    *,
    snapshot: str,
    commit_sha: str,
) -> str:
    lines = [
        f"<!-- swarm:frozen-checklist-begin snapshot={snapshot} commit={commit_sha} -->",
    ]
    for item in items:
        lines.append(
            f"<!-- swarm:task-meta id={item.task_id} first_planned={item.first_planned.isoformat()} frozen=true -->"
        )
        suffix = ""
        if item.is_p0:
            suffix = " <!-- swarm:p0 -->"
        checkbox = "- [ ] "
        body = item.text
        if item.is_p0 and "P0" not in body.upper():
            body = f"**P0** {body}"
        lines.append(f"{checkbox}{body}{suffix}")
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


def apply_wiki_freeze(
    wiki_dir: Path,
    day: date,
    items: list[FrozenItem],
    *,
    timezone: str,
    dry_run: bool = False,
    worktree_parent: Path | None = None,
) -> FreezeResult:
    daily_rel = _daily_rel(day)
    daily_path = wiki_dir / daily_rel
    if not daily_path.exists():
        raise FileNotFoundError(f"missing wiki daily note: {daily_rel}")

    original = daily_path.read_text(encoding="utf-8")
    tz = ZoneInfo(timezone)
    snapshot = datetime.now(tz).replace(microsecond=0).isoformat()
    branch = freeze_branch_name(day)
    parent = worktree_parent or (wiki_dir.parent / f".wiki-freeze-{day.isoformat()}")
    wt_path = parent / "wiki-freeze"

    if dry_run:
        return FreezeResult(
            applied=True,
            commit_sha=None,
            snapshot=snapshot,
            branch=branch,
            daily_path=daily_path,
        )

    try:
        _run_git(wiki_dir, "fetch", "--all", "--quiet")
    except RuntimeError:
        pass
    try:
        refs = _run_git(wiki_dir, "show-ref", "--verify", f"refs/heads/{branch}")
    except RuntimeError:
        refs = ""
    if not refs:
        base = _run_git(wiki_dir, "rev-parse", "HEAD")
        parent.mkdir(parents=True, exist_ok=True)
        if wt_path.exists():
            _run_git(wt_path, "checkout", branch)
        else:
            _run_git(wiki_dir, "worktree", "add", "-B", branch, str(wt_path), base)

    wt_daily = wt_path / daily_rel
    if not wt_daily.exists():
        wt_daily.parent.mkdir(parents=True, exist_ok=True)
        wt_daily.write_text(original, encoding="utf-8")

    body = wt_daily.read_text(encoding="utf-8")
    if _has_frozen_block(body):
        note = parse_daily_markdown(body, day)
        return FreezeResult(
            applied=False,
            commit_sha=note.frozen_snapshot_commit,
            snapshot=note.frozen_snapshot_at or "",
            branch=branch,
            daily_path=wt_daily,
        )
    commit_placeholder = "pending"
    block = _build_frozen_block(items, snapshot=snapshot, commit_sha=commit_placeholder)
    updated = _inject_hoje_block(body, block)
    wt_daily.write_text(updated, encoding="utf-8")

    _run_git(wt_path, "add", daily_rel)
    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=wt_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode == 0:
        raise RuntimeError("freeze produced no staged changes")
    if diff_proc.returncode not in (1,):
        raise RuntimeError(f"git diff --cached failed: {diff_proc.stderr.strip()}")
    _run_git(
        wt_path,
        "commit",
        "-m",
        f"reports: freeze morning checklist {day.isoformat()}",
    )
    commit_sha = _run_git(wt_path, "rev-parse", "HEAD")

    # Record exact commit in marker (amend file content)
    final_block = _build_frozen_block(items, snapshot=snapshot, commit_sha=commit_sha)
    final_body = _inject_hoje_block(body, final_block)
    wt_daily.write_text(final_body, encoding="utf-8")
    _run_git(wt_path, "add", daily_rel)
    _run_git(
        wt_path,
        "commit",
        "--amend",
        "--no-edit",
    )

    return FreezeResult(
        applied=True,
        commit_sha=commit_sha,
        snapshot=snapshot,
        branch=branch,
        daily_path=wt_daily,
    )
