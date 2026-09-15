"""Push + PR seam for the wiki freeze branch.

The freeze commit is external state: once it exists it must reach the remote and a
pull request, otherwise the vault governance in `mathai-wiki/CLAUDE.md` (branch + PR,
never a direct write) depends on the owner remembering to do it by hand. F2 owns the
seam and a durable queue; wiring the actual `gh` call is F4/F5 work.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PublishRequest:
    wiki_dir: Path
    worktree: Path
    branch: str
    day: date
    commit_sha: str
    title: str
    body: str

    def to_json(self) -> dict[str, str]:
        return {
            "wiki_dir": str(self.wiki_dir),
            "worktree": str(self.worktree),
            "branch": self.branch,
            "day": self.day.isoformat(),
            "commit_sha": self.commit_sha,
            "title": self.title,
            "body": self.body,
        }


@dataclass(frozen=True)
class PublishOutcome:
    pushed: bool
    pull_request_url: str | None
    detail: str


class WikiPublisher(Protocol):
    def publish(self, request: PublishRequest) -> PublishOutcome: ...


class QueuedPublisher:
    """Records the intent durably so nothing is lost while `gh` is not wired yet."""

    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = queue_dir

    def publish(self, request: PublishRequest) -> PublishOutcome:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "state": "pending-push-and-pr",
            **request.to_json(),
        }
        path = self.queue_dir / f"{request.day.isoformat()}-{request.commit_sha[:12]}.json"
        _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return PublishOutcome(
            pushed=False,
            pull_request_url=None,
            detail=f"queued at {path}",
        )


class GitPushPublisher:
    """Pushes the freeze branch and queues the PR request.

    Creating the pull request needs a GitHub credential, which lives on the VPS and
    belongs to F4/F5. Pushing is enough to make the snapshot recoverable off-box.
    """

    def __init__(self, queue_dir: Path, *, remote: str = "origin") -> None:
        self.queue_dir = queue_dir
        self.remote = remote

    def publish(self, request: PublishRequest) -> PublishOutcome:
        queued = QueuedPublisher(self.queue_dir).publish(request)
        proc = subprocess.run(
            ["git", "push", "--set-upstream", self.remote, request.branch],
            cwd=request.worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return PublishOutcome(
                pushed=False,
                pull_request_url=None,
                detail=f"push failed; {queued.detail}",
            )
        return PublishOutcome(
            pushed=True,
            pull_request_url=None,
            detail=f"pushed to {self.remote}/{request.branch}; PR still {queued.detail}",
        )


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], *, cwd: Path, timeout: int) -> CommandResult: ...


def run_command(argv: list[str], *, cwd: Path, timeout: int) -> CommandResult:
    """The real transport: an argv list, no shell, a hard timeout."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(returncode=127, stdout="", stderr=f"{type(exc).__name__}: {exc}")
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class PublishFailed(RuntimeError):
    """The branch or the pull request did not land. The caller records `failed`."""


class GhPullRequestPublisher:
    """Push the branch, verify the remote head, then create or reuse the pull request.

    The expected-head check is the point of this class. `gh pr create` names a branch,
    not a commit, so without re-reading `ls-remote` a push that silently lost a race
    would open a pull request that claims to carry work it does not carry. Here the
    remote head must equal the commit we meant to publish, or nothing is opened and the
    caller writes `failed` into the ledger.

    Re-running is safe: pushing an already-pushed commit is a no-op, and an existing
    pull request for the same head is reused instead of duplicated.
    """

    def __init__(
        self,
        queue_dir: Path,
        *,
        remote: str = "origin",
        base_branch: str = "main",
        gh_bin: str = "gh",
        draft: bool = False,
        repo: str | None = None,
        timeout_seconds: int = 120,
        runner: CommandRunner | None = None,
    ) -> None:
        self.queue_dir = queue_dir
        self.remote = remote
        self.base_branch = base_branch
        self.gh_bin = gh_bin
        self.draft = draft
        self.repo = repo
        self.timeout_seconds = timeout_seconds
        self.runner = runner or run_command

    def publish(self, request: PublishRequest) -> PublishOutcome:
        queued = QueuedPublisher(self.queue_dir).publish(request)
        cwd = request.worktree

        push = self._run(
            ["git", "push", "--set-upstream", self.remote, f"{request.commit_sha}:refs/heads/{request.branch}"],
            cwd,
        )
        if push.returncode != 0:
            raise PublishFailed(f"push failed: {_tail(push.stderr)}; {queued.detail}")

        remote_head = self._remote_head(cwd, request.branch)
        if remote_head != request.commit_sha:
            raise PublishFailed(
                f"remote {self.remote}/{request.branch} is at "
                f"{remote_head or 'nothing'}, expected {request.commit_sha}"
            )

        existing = self._existing_pull_request(cwd, request.branch)
        if existing:
            return PublishOutcome(
                pushed=True,
                pull_request_url=existing,
                detail=f"reused {existing} at {request.commit_sha[:12]}",
            )

        argv = [self.gh_bin, "pr", "create", "--base", self.base_branch, "--head", request.branch,
                "--title", request.title, "--body", request.body]
        if self.draft:
            argv.append("--draft")
        if self.repo:
            argv.extend(["-R", self.repo])
        created = self._run(argv, cwd)
        if created.returncode != 0:
            raise PublishFailed(f"gh pr create failed: {_tail(created.stderr)}")
        url = _first_url(created.stdout)
        if url is None:
            raise PublishFailed("gh pr create printed no pull request URL")
        return PublishOutcome(
            pushed=True,
            pull_request_url=url,
            detail=f"opened {url} at {request.commit_sha[:12]}",
        )

    def _remote_head(self, cwd: Path, branch: str) -> str | None:
        result = self._run(["git", "ls-remote", self.remote, f"refs/heads/{branch}"], cwd)
        if result.returncode != 0:
            raise PublishFailed(f"ls-remote failed: {_tail(result.stderr)}")
        line = result.stdout.strip().splitlines()
        if not line:
            return None
        return line[0].split()[0].strip()

    def _existing_pull_request(self, cwd: Path, branch: str) -> str | None:
        argv = [self.gh_bin, "pr", "list", "--head", branch, "--state", "open",
                "--json", "url", "--limit", "1"]
        if self.repo:
            argv.extend(["-R", self.repo])
        result = self._run(argv, cwd)
        if result.returncode != 0:
            # An unreadable list is not proof that nothing is open, so refuse rather
            # than risk a duplicate pull request for the same head.
            raise PublishFailed(f"gh pr list failed: {_tail(result.stderr)}")
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            raise PublishFailed("gh pr list returned non-JSON") from None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            url = rows[0].get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
        return None

    def _run(self, argv: list[str], cwd: Path) -> CommandResult:
        return self.runner(argv, cwd=cwd, timeout=self.timeout_seconds)


def build_publisher(
    publish_config,
    queue_dir: Path,
    *,
    runner: CommandRunner | None = None,
) -> WikiPublisher | None:
    """Turn the `evening.publish` block into a transport. `None` means publish nothing."""
    from swarm_reports.evening.config import (
        PUBLISH_GH,
        PUBLISH_NONE,
        PUBLISH_PUSH,
        PUBLISH_QUEUE,
    )

    if publish_config.mode == PUBLISH_NONE:
        return None
    if publish_config.mode == PUBLISH_QUEUE:
        return QueuedPublisher(queue_dir)
    if publish_config.mode == PUBLISH_PUSH:
        return GitPushPublisher(queue_dir, remote=publish_config.remote)
    if publish_config.mode == PUBLISH_GH:
        return GhPullRequestPublisher(
            queue_dir,
            remote=publish_config.remote,
            base_branch=publish_config.base_branch,
            gh_bin=publish_config.gh_bin,
            draft=publish_config.draft,
            repo=publish_config.repo,
            timeout_seconds=publish_config.timeout_seconds,
            runner=runner,
        )
    raise ValueError(f"unknown publish mode '{publish_config.mode}'")


def _tail(text: str, limit: int = 300) -> str:
    return (text or "").strip()[-limit:]


def _first_url(text: str) -> str | None:
    for token in (text or "").split():
        if token.startswith("https://"):
            return token.strip()
    return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
