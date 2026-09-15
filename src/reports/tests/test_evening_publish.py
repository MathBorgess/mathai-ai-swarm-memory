"""The `gh` transport. The expected-head gate is the reason this class exists."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from swarm_reports.evening.config import (
    PUBLISH_GH,
    PUBLISH_NONE,
    PUBLISH_PUSH,
    PUBLISH_QUEUE,
    PublishConfig,
)
from swarm_reports.wiki.publish import (
    GhPullRequestPublisher,
    GitPushPublisher,
    PublishFailed,
    PublishRequest,
    QueuedPublisher,
    build_publisher,
)

DAY = date(2026, 9, 14)
SHA = "a" * 40


def _request(tmp_path: Path) -> PublishRequest:
    return PublishRequest(
        wiki_dir=tmp_path / "wiki",
        worktree=tmp_path / "wt",
        branch="codex/reports-freeze-2026-09-14",
        day=DAY,
        commit_sha=SHA,
        title="reports: evening writeback",
        body="corpo",
    )


def test_pushes_the_commit_then_opens_the_pull_request(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh()
    publisher = GhPullRequestPublisher(tmp_path / "queue", runner=gh, draft=True)
    outcome = publisher.publish(_request(tmp_path))

    assert outcome.pushed is True
    assert outcome.pull_request_url == "https://github.com/owner/wiki/pull/7"
    push = next(c for c in gh.calls if c[:2] == ["git", "push"])
    assert push[-1] == f"{SHA}:refs/heads/codex/reports-freeze-2026-09-14"
    create = next(c for c in gh.calls if c[1:3] == ["pr", "create"])
    assert "--draft" in create
    assert create[create.index("--base") + 1] == "main"
    assert (tmp_path / "queue").is_dir(), "the intent is queued before the attempt"


def test_a_remote_head_that_is_not_ours_blocks_the_pull_request(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh(head="b" * 40)
    publisher = GhPullRequestPublisher(tmp_path / "queue", runner=gh)
    with pytest.raises(PublishFailed) as exc:
        publisher.publish(_request(tmp_path))
    assert "expected" in str(exc.value)
    assert not any(c[1:3] == ["pr", "create"] for c in gh.calls)


def test_a_failed_push_never_reaches_gh(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh(head=SHA)
    gh.push_returncode = 1
    with pytest.raises(PublishFailed) as exc:
        GhPullRequestPublisher(tmp_path / "queue", runner=gh).publish(_request(tmp_path))
    assert "push failed" in str(exc.value)
    assert [c[1:3] for c in gh.calls] == [["push", "--set-upstream"]]


def test_an_existing_pull_request_is_reused_not_duplicated(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh(existing_url="https://github.com/owner/wiki/pull/3")
    outcome = GhPullRequestPublisher(tmp_path / "queue", runner=gh).publish(_request(tmp_path))
    assert outcome.pull_request_url == "https://github.com/owner/wiki/pull/3"
    assert not any(c[1:3] == ["pr", "create"] for c in gh.calls)


def test_gh_failing_is_a_failure_not_a_silent_success(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh()
    gh.create_returncode = 1
    with pytest.raises(PublishFailed) as exc:
        GhPullRequestPublisher(tmp_path / "queue", runner=gh).publish(_request(tmp_path))
    assert "gh pr create failed" in str(exc.value)


def test_repo_override_is_passed_to_every_gh_call(tmp_path):
    from tests.conftest import FakeGh

    gh = FakeGh()
    GhPullRequestPublisher(tmp_path / "q", runner=gh, repo="MathBorgess/mathai-wiki").publish(
        _request(tmp_path)
    )
    for call in gh.calls:
        if call[0] == "gh":
            assert call[-2:] == ["-R", "MathBorgess/mathai-wiki"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        (PUBLISH_NONE, type(None)),
        (PUBLISH_QUEUE, QueuedPublisher),
        (PUBLISH_PUSH, GitPushPublisher),
        (PUBLISH_GH, GhPullRequestPublisher),
    ],
)
def test_the_config_block_alone_selects_the_transport(tmp_path, mode, expected):
    """No custom Python: `{"publish": {"mode": "gh"}}` is the whole production wiring."""
    publisher = build_publisher(PublishConfig(mode=mode), tmp_path / "queue")
    assert isinstance(publisher, expected)
