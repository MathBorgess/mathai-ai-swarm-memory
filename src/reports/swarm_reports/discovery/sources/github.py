"""GitHub discovery via the `gh` CLI: PRs (merged vs. changed) and commits.

Read-only: `gh api --method GET` only, argv list (no shell), exact-match repo
allowlist. Pagination is manual and bounded (`max_pages`); if more pages remain
after the bound, the batch is marked truncated and the checkpoint cursor is not
advanced past what was actually fetched.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.models import DiscoveryItem
from swarm_reports.discovery.sources.base import SourceError, SourceResult

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class ProcResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], int], ProcResult]


def _default_runner(argv: list[str], timeout: int) -> ProcResult:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return ProcResult(completed.returncode, completed.stdout, completed.stderr)


def _validate_repo(repo: str) -> None:
    """Reject anything that is not a plain `owner/name` pair before it reaches argv.

    `config.repos` IS the allowlist (exact members are iterated directly); this
    guards against a malformed/malicious entry slipping into that list, e.g. via
    path traversal or shell metacharacters, and reaching `gh api`.
    """
    if not REPO_RE.match(repo):
        raise SourceError(f"github: rejected malformed repo '{repo}'")


def _parse_gh_i_output(raw: str) -> tuple[dict[str, str], Any]:
    """Split `gh api -i` output into (headers, parsed JSON body)."""
    if "\r\n\r\n" in raw:
        head, _, body = raw.partition("\r\n\r\n")
    else:
        head, _, body = raw.partition("\n\n")
    headers: dict[str, str] = {}
    for line in head.splitlines()[1:]:  # skip HTTP status line
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    body = body.strip()
    parsed = json.loads(body) if body else []
    return headers, parsed


def _has_next_page(headers: dict[str, str]) -> bool:
    link = headers.get("link", "")
    return 'rel="next"' in link


@dataclass
class GithubConfig:
    repos: tuple[str, ...]
    max_pages: int = 3
    per_page: int = 30
    timeout_seconds: int = 20
    gh_bin: str = "gh"
    runner: Runner = field(default=_default_runner)


class GithubSource:
    name = "github"

    def __init__(self, config: GithubConfig) -> None:
        self.config = config

    def discover(self, checkpoint: SourceCheckpoint) -> SourceResult:
        if not self.config.repos:
            raise SourceError("github: no repos configured")
        items: list[DiscoveryItem] = []
        new_ids: list[str] = []
        truncated = False
        cursors: list[str] = [checkpoint.cursor] if checkpoint.cursor else []
        seen_in_run: set[str] = set(checkpoint.seen_ids)
        for repo in self.config.repos:
            _validate_repo(repo)
            pr_max, pr_trunc = self._discover_prs(repo, checkpoint, items, new_ids, seen_in_run)
            commit_max, commit_trunc = self._discover_commits(repo, checkpoint, items, new_ids, seen_in_run)
            truncated = truncated or pr_trunc or commit_trunc
            for candidate in (pr_max, commit_max):
                if candidate:
                    cursors.append(candidate)
        new_cursor = max(cursors) if cursors else checkpoint.cursor
        if truncated:
            new_cursor = checkpoint.cursor
        return SourceResult(items=items, new_cursor=new_cursor, new_ids=new_ids, truncated=truncated)

    def _call(self, path: str, params: dict[str, str]) -> tuple[dict[str, str], Any]:
        argv = [self.config.gh_bin, "api", "-i", "--method", "GET", path]
        for key, value in params.items():
            argv.extend(["-f", f"{key}={value}"])
        result = self.config.runner(argv, self.config.timeout_seconds)
        if result.returncode != 0:
            raise SourceError(f"github: gh api failed for {path}: {result.stderr.strip()[:300]}")
        return _parse_gh_i_output(result.stdout)

    def _discover_prs(
        self,
        repo: str,
        checkpoint: SourceCheckpoint,
        items: list[DiscoveryItem],
        new_ids: list[str],
        seen_in_run: set[str],
    ) -> tuple[str | None, bool]:
        max_observed: str | None = None
        truncated = False
        for page in range(1, self.config.max_pages + 1):
            headers, body = self._call(
                f"repos/{repo}/pulls",
                {
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": str(self.config.per_page),
                    "page": str(page),
                },
            )
            if not isinstance(body, list) or not body:
                break
            for pr in body:
                updated_at = str(pr.get("updated_at") or "")
                merged_at = pr.get("merged_at")
                number = pr.get("number")
                if not updated_at or number is None:
                    continue
                if checkpoint.cursor and updated_at <= checkpoint.cursor:
                    continue
                if max_observed is None or updated_at > max_observed:
                    max_observed = updated_at
                if merged_at:
                    kind, ts, suffix = "pr_merged", str(merged_at), "merged"
                else:
                    kind, ts, suffix = "pr_updated", updated_at, "updated"
                item_id = f"github:{repo}:pr:{number}:{suffix}"
                if item_id in seen_in_run:
                    continue
                seen_in_run.add(item_id)
                items.append(
                    DiscoveryItem(
                        kind=kind,
                        id=item_id,
                        source=f"github:{repo}",
                        url=pr.get("html_url"),
                        title=f"#{number} {pr.get('title') or ''}".strip(),
                        observed_at=ts,
                        meta={
                            "repo": repo,
                            "number": number,
                            "author": (pr.get("user") or {}).get("login"),
                        },
                    )
                )
                new_ids.append(item_id)
            if not _has_next_page(headers) or len(body) < self.config.per_page:
                break
            if page == self.config.max_pages:
                truncated = True
        return max_observed, truncated

    def _discover_commits(
        self,
        repo: str,
        checkpoint: SourceCheckpoint,
        items: list[DiscoveryItem],
        new_ids: list[str],
        seen_in_run: set[str],
    ) -> tuple[str | None, bool]:
        max_observed: str | None = None
        truncated = False
        params: dict[str, str] = {"per_page": str(self.config.per_page)}
        if checkpoint.cursor:
            params["since"] = checkpoint.cursor
        for page in range(1, self.config.max_pages + 1):
            page_params = dict(params, page=str(page))
            headers, body = self._call(f"repos/{repo}/commits", page_params)
            if not isinstance(body, list) or not body:
                break
            for commit in body:
                sha = commit.get("sha")
                commit_info = commit.get("commit") or {}
                committer = commit_info.get("committer") or {}
                date = str(committer.get("date") or "")
                if not sha or not date:
                    continue
                if checkpoint.cursor and date <= checkpoint.cursor:
                    continue
                if max_observed is None or date > max_observed:
                    max_observed = date
                item_id = f"github:{repo}:commit:{sha}"
                if item_id in seen_in_run:
                    continue
                seen_in_run.add(item_id)
                items.append(
                    DiscoveryItem(
                        kind="commit",
                        id=item_id,
                        source=f"github:{repo}",
                        url=commit.get("html_url"),
                        title=str(commit_info.get("message") or "").splitlines()[0][:200],
                        observed_at=date,
                        meta={"repo": repo, "sha": sha, "author": (commit.get("author") or {}).get("login")},
                    )
                )
                new_ids.append(item_id)
            if not _has_next_page(headers) or len(body) < self.config.per_page:
                break
            if page == self.config.max_pages:
                truncated = True
        return max_observed, truncated
