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
