"""Apply the night's vault edits on an isolated branch.

Same discipline as the morning freeze, for the same reason: the owner's live checkout
may hold uncommitted private text, and a writeback that ran there would either read it
or destroy it. So the night fetches `origin/main`, works in a per-day worktree under
`state_dir`, commits, and hands the branch to a publisher.

The commit is idempotent by construction. Every edit is computed from one stored
revision, so re-running the same revision after a crash re-writes identical bytes and
`git diff --cached --quiet` reports nothing to commit; the existing HEAD is returned
instead of a second commit. A *newer* revision produces different bytes and stacks one
more commit on the same day branch, which is what a pull request should show.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from swarm_reports.metrics.frontmatter import split_frontmatter
from swarm_reports.wiki.freeze import (
    freeze_branch_name,
    freeze_worktree_path,
    git_ok,
    prepare_worktree,
    run_git,
)
from swarm_reports.wiki.notes import (
    TomorrowItem,
    UnplannedRecord,
    post_note_filename,
    render_new_post_note,
    update_daily_note,
    update_post_note,
)

DAILY_TEMPLATE_REL = "daily/_template.md"
POSTS_REL = "brand/posts"


@dataclass(frozen=True)
class PostUpdate:
    url: str
    platform: str
    guided: bool
    checkpoint: str
    metrics: dict | None
    posted_on: date


@dataclass(frozen=True)
class NightEdits:
    day: date
    revision: int
    done_ids: set[str]
    classifications: dict[str, str | None]
    unplanned: list[UnplannedRecord]
    evolution_lines: list[str]
    tomorrow: list[TomorrowItem]
    tomorrow_day: date
    posts: list[PostUpdate] = field(default_factory=list)


@dataclass(frozen=True)
class NightResult:
    branch: str
    commit_sha: str
    committed: bool
    worktree: Path
    base_ref: str
    changed_paths: tuple[str, ...]
    created_posts: tuple[str, ...]
    lint_ok: bool | None = None
    lint_detail: str = ""


def night_branch_name(day: date) -> str:
    return f"codex/reports-evening-{day.isoformat()}"


def night_worktree_path(worktree_parent: Path, day: date) -> Path:
    return (worktree_parent / f"evening-{day.isoformat()}").resolve()


def night_target(wiki_dir: Path, day: date, worktree_parent: Path) -> tuple[str, Path]:
    """Which branch and worktree the night writes to.

    When the morning's freeze branch for this day still exists, the evening stacks on
    it: one branch and one pull request per calendar day for `daily/<day>.md`, which is
    also the only arrangement git allows — a branch can be checked out in exactly one
    worktree, and the freeze already owns that one. Once the freeze has been merged and
    its branch deleted, the evening opens its own branch off `main` instead.
    """
    freeze = freeze_branch_name(day)
    if git_ok(wiki_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{freeze}"):
        return freeze, freeze_worktree_path(worktree_parent, day)
    return night_branch_name(day), night_worktree_path(worktree_parent, day)


def apply_night_edits(
    wiki_dir: Path,
    edits: NightEdits,
    *,
    worktree_parent: Path,
    local_only: bool = False,
    remote: str = "origin",
    lint_command: tuple[str, ...] | None = None,
) -> NightResult:
    day = edits.day
    branch, wt_path = night_target(wiki_dir, day, worktree_parent)
    base_ref = prepare_worktree(
        wiki_dir, day, wt_path, branch, local_only=local_only, remote=remote
    )

    changed: list[str] = []
    created: list[str] = []

    daily_rel = f"daily/{day.isoformat()}.md"
    daily_path = wt_path / daily_rel
    if not daily_path.exists():
        raise RuntimeError(
            f"{daily_rel} is missing on {branch}; the morning freeze must land first"
        )
    original = daily_path.read_text(encoding="utf-8")
    updated = update_daily_note(
        original,
        day=day,
        revision=edits.revision,
        done_ids=set(edits.done_ids),
        classifications=dict(edits.classifications),
        unplanned=list(edits.unplanned),
        evolution_lines=list(edits.evolution_lines),
        tomorrow=list(edits.tomorrow),
        tomorrow_day=edits.tomorrow_day,
    )
    if updated != original:
        daily_path.write_text(updated, encoding="utf-8")
    changed.append(daily_rel)

    posts_dir = wt_path / POSTS_REL
    for post in edits.posts:
        existing = _find_post_note(posts_dir, post.url)
        if existing is None:
            name = post_note_filename(post.url, post.posted_on)
            target = posts_dir / name
            posts_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                render_new_post_note(
                    url=post.url,
                    platform=post.platform,
                    posted_on=post.posted_on,
                    guided=post.guided,
                    checkpoint=post.checkpoint,
                    metrics=post.metrics,
                ),
                encoding="utf-8",
            )
            created.append(f"{POSTS_REL}/{name}")
            changed.append(f"{POSTS_REL}/{name}")
            continue
        body = existing.read_text(encoding="utf-8")
        merged = update_post_note(
            body,
            checkpoint=post.checkpoint,
            metrics=post.metrics,
            url=post.url,
            platform=post.platform,
        )
        if merged != body:
            existing.write_text(merged, encoding="utf-8")
        changed.append(f"{POSTS_REL}/{existing.name}")

    lint_ok: bool | None = None
    lint_detail = ""
    if lint_command:
        lint_ok, lint_detail = _run_lint(wt_path, lint_command)

    run_git(wt_path, "add", "--", *sorted(set(changed)))
    if git_ok(wt_path, "diff", "--cached", "--quiet"):
        # Same revision replayed after a crash: the bytes already match HEAD.
        return NightResult(
            branch=branch,
            commit_sha=run_git(wt_path, "rev-parse", "HEAD"),
            committed=False,
            worktree=wt_path,
            base_ref=base_ref,
            changed_paths=tuple(sorted(set(changed))),
            created_posts=tuple(created),
            lint_ok=lint_ok,
            lint_detail=lint_detail,
        )

    run_git(
        wt_path,
        "commit",
        "--quiet",
        "-m",
        f"reports: evening writeback {day.isoformat()} (revision {edits.revision})",
    )
    return NightResult(
        branch=branch,
        commit_sha=run_git(wt_path, "rev-parse", "HEAD"),
        committed=True,
        worktree=wt_path,
        base_ref=base_ref,
        changed_paths=tuple(sorted(set(changed))),
        created_posts=tuple(created),
        lint_ok=lint_ok,
        lint_detail=lint_detail,
    )


def _find_post_note(posts_dir: Path, url: str) -> Path | None:
    """Match on the recorded `url:` first, then on the deterministic filename.

    Matching by URL is what keeps a 48h checkpoint from creating a second note for a
    post the morning already drafted under a human-readable slug.
    """
    if not posts_dir.is_dir():
        return None
    needle = url.strip()
    for path in sorted(posts_dir.glob("*.md")):
        try:
            frontmatter, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if str(frontmatter.get("url") or "").strip() == needle:
            return path
    return None


def _run_lint(worktree: Path, command: tuple[str, ...]) -> tuple[bool, str]:
    """Run the vault lint inside the worktree. Never through a shell."""
    try:
        proc = subprocess.run(
            list(command),
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]
    detail = (proc.stdout or proc.stderr or "").strip()[:500]
    return proc.returncode == 0, detail
