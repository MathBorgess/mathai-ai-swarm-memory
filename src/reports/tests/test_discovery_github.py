from __future__ import annotations

import json

import pytest

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.sources.base import SourceError
from swarm_reports.discovery.sources.github import GithubConfig, GithubSource, ProcResult


def _gh_i(status: int, body, next_page: bool = False) -> str:
    headers = f"HTTP/2.0 {status}\r\n"
    if next_page:
        headers += 'Link: <https://api.github.com/x?page=2>; rel="next"\r\n'
    return headers + "\r\n" + json.dumps(body)


def _pr(number: int, updated_at: str, merged_at: str | None = None, title: str = "t") -> dict:
    return {
        "number": number,
        "updated_at": updated_at,
        "merged_at": merged_at,
        "title": title,
        "html_url": f"https://example.invalid/pr/{number}",
        "user": {"login": "someone"},
    }


def _commit(sha: str, date: str, message: str = "msg") -> dict:
    return {
        "sha": sha,
        "html_url": f"https://example.invalid/commit/{sha}",
        "commit": {"committer": {"date": date}, "message": message},
        "author": {"login": "someone"},
    }


def test_malformed_repo_is_rejected_before_any_subprocess_call():
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(argv)
        return ProcResult(0, _gh_i(200, []), "")

    config = GithubConfig(repos=("owner/repo; rm -rf /",), runner=runner)
    source = GithubSource(config)
    with pytest.raises(SourceError):
        source.discover(SourceCheckpoint())
    assert calls == []


def test_path_traversal_repo_is_rejected():
    config = GithubConfig(repos=("../../etc/passwd",), runner=lambda *a: ProcResult(0, _gh_i(200, []), ""))
    source = GithubSource(config)
    with pytest.raises(SourceError):
        source.discover(SourceCheckpoint())


def test_merged_pr_emits_only_merge_event_not_also_update():
    responses = iter(
        [
            _gh_i(200, [_pr(1, "2026-09-14T09:00:00Z", merged_at="2026-09-14T09:00:00Z")]),
            _gh_i(200, []),  # commits page
        ]
    )

    def runner(argv, timeout):
        return ProcResult(0, next(responses), "")

    config = GithubConfig(repos=("o/r",), runner=runner, max_pages=1)
    source = GithubSource(config)
    result = source.discover(SourceCheckpoint())
    kinds = [item.kind for item in result.items]
    assert kinds.count("pr_merged") == 1
    assert "pr_updated" not in kinds


def test_updated_pr_without_merge_emits_pr_updated():
    responses = iter([_gh_i(200, [_pr(2, "2026-09-14T09:00:00Z")]), _gh_i(200, [])])

    def runner(argv, timeout):
        return ProcResult(0, next(responses), "")

    config = GithubConfig(repos=("o/r",), runner=runner, max_pages=1)
    result = GithubSource(config).discover(SourceCheckpoint())
    assert [item.kind for item in result.items] == ["pr_updated"]


def test_truncated_pagination_does_not_advance_cursor():
    # max_pages=1, page 1 is full (per_page items) and has rel="next" -> truncated.
    full_page = [_pr(i, f"2026-09-14T0{i}:00:00Z") for i in range(1, 3)]
    responses = iter(
        [
            _gh_i(200, full_page, next_page=True),  # PR page 1/1 (truncated)
            _gh_i(200, []),  # commits page
        ]
    )

    def runner(argv, timeout):
        return ProcResult(0, next(responses), "")

    config = GithubConfig(repos=("o/r",), runner=runner, max_pages=1, per_page=2)
    checkpoint = SourceCheckpoint(cursor="2026-09-13T00:00:00Z")
    result = GithubSource(config).discover(checkpoint)
    assert result.truncated is True
    assert result.new_cursor == checkpoint.cursor  # unchanged: unvisited pages remain
    assert len(result.items) == 2  # still returns what it saw


def test_overlapping_duplicate_pages_are_deduped_within_one_run():
    same_pr = _pr(9, "2026-09-14T09:00:00Z")
    responses = iter(
        [
            _gh_i(200, [same_pr], next_page=True),  # PR page 1 (claims more pages)
            _gh_i(200, [same_pr]),  # PR page "2" (buggy overlap, same content)
            _gh_i(200, []),  # commits page
        ]
    )

    def runner(argv, timeout):
        return ProcResult(0, next(responses), "")

    config = GithubConfig(repos=("o/r",), runner=runner, max_pages=2, per_page=1)
    result = GithubSource(config).discover(SourceCheckpoint())
    # per_page=1 with a full first page keeps paginating; both pages carry the
    # same PR id, so the in-run dedup must collapse them to a single item.
    assert len(result.items) == 1


def test_gh_failure_raises_source_error():
    def runner(argv, timeout):
        return ProcResult(1, "", "gh: authentication required")

    config = GithubConfig(repos=("o/r",), runner=runner)
    with pytest.raises(SourceError):
        GithubSource(config).discover(SourceCheckpoint())


def test_commit_discovery_respects_cursor():
    responses = iter(
        [
            _gh_i(200, []),  # PR page
            _gh_i(200, [_commit("abc", "2026-09-14T10:00:00Z")]),
        ]
    )

    def runner(argv, timeout):
        return ProcResult(0, next(responses), "")

    config = GithubConfig(repos=("o/r",), runner=runner, max_pages=1)
    result = GithubSource(config).discover(SourceCheckpoint(cursor="2026-09-13T00:00:00Z"))
    assert result.items[0].kind == "commit"
    assert result.items[0].id == "github:o/r:commit:abc"
