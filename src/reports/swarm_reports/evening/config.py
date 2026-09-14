"""The `evening` config block (F4).

Two rules shape this file:

- **The production operator must not need custom Python.** `"publish": {"mode": "gh"}`
  is enough to get push + pull request from the packaged CLI, so `no-publish` stays an
  explicit local-demo choice instead of the only path that works out of the box.
- **Nothing here may be supplied by a model.** Every argv is read from the operator's
  config file and run without a shell; the reflection adapter returns text and
  suggestions, never a path to open or a command to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: No push, no PR: the freeze/evening commits stay on the local branch only. The demo
#: and the tests use this; production does not.
PUBLISH_NONE = "none"
#: Record the intent durably and stop. What F2 shipped.
PUBLISH_QUEUE = "queue"
#: Push the branch; leave the pull request to the operator.
PUBLISH_PUSH = "push"
#: Push, then create (or reuse) the pull request through the `gh` CLI.
PUBLISH_GH = "gh"

PUBLISH_MODES = (PUBLISH_NONE, PUBLISH_QUEUE, PUBLISH_PUSH, PUBLISH_GH)

MAX_P0_TOMORROW = 3


@dataclass(frozen=True)
class PublishConfig:
    mode: str = PUBLISH_QUEUE
    remote: str = "origin"
    base_branch: str = "main"
    gh_bin: str = "gh"
    #: Draft pull requests are the default for anything the owner has to judge.
    draft: bool = False
    #: `owner/name`, passed to `gh -R`. Without it `gh` infers the repo from the remote.
    repo: str | None = None
    timeout_seconds: int = 120

    @property
    def pushes(self) -> bool:
        return self.mode in (PUBLISH_PUSH, PUBLISH_GH)

    @property
    def opens_pull_request(self) -> bool:
        return self.mode == PUBLISH_GH

    @classmethod
    def from_mapping(cls, raw: Any) -> PublishConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("evening.publish must be an object")
        _reject_unknown(
            raw,
            {"mode", "remote", "base_branch", "gh_bin", "draft", "repo", "timeout_seconds"},
            "evening.publish",
        )
        mode = str(raw.get("mode") or PUBLISH_QUEUE).strip().lower()
        if mode not in PUBLISH_MODES:
            raise ValueError(f"evening.publish.mode must be one of {list(PUBLISH_MODES)}")
        draft = raw.get("draft", False)
        if not isinstance(draft, bool):
            raise ValueError("evening.publish.draft must be a boolean")
        repo = raw.get("repo")
        if repo is not None:
            repo = _bounded(repo, 200, "evening.publish.repo")
            if repo.count("/") != 1 or repo.startswith("-"):
                raise ValueError("evening.publish.repo must be 'owner/name'")
        return cls(
            mode=mode,
            remote=_bounded(raw.get("remote") or "origin", 64, "evening.publish.remote"),
            base_branch=_bounded(
                raw.get("base_branch") or "main", 200, "evening.publish.base_branch"
            ),
            gh_bin=_bounded(raw.get("gh_bin") or "gh", 400, "evening.publish.gh_bin"),
            draft=draft,
            repo=repo,
            timeout_seconds=_bounded_int(
                raw.get("timeout_seconds", 120), 1, 3600, "evening.publish.timeout_seconds"
            ),
        )


@dataclass(frozen=True)
class ReflectionConfig:
    """Optional qualitative pass. Metrics never come from here (design, section 2)."""

    command: tuple[str, ...]
    timeout_seconds: int = 120

    @classmethod
    def from_mapping(cls, raw: Any) -> ReflectionConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("evening.reflection must be an object")
        _reject_unknown(raw, {"command", "timeout_seconds"}, "evening.reflection")
        command = raw.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) and x.strip() for x in command)
        ):
            raise ValueError("evening.reflection.command must be a non-empty argv list")
        return cls(
            command=tuple(command),
            timeout_seconds=_bounded_int(
                raw.get("timeout_seconds", 120), 1, 3600, "evening.reflection.timeout_seconds"
            ),
        )


@dataclass(frozen=True)
class EveningConfig:
    publish: PublishConfig = PublishConfig()
    reflection: ReflectionConfig | None = None
    #: Freeze/evening worktrees start from local HEAD instead of a fetched `origin/main`.
    #: Only a wiki checkout with no remote (the tests, the demo) may set this.
    wiki_local_only: bool = False
    #: Run inside the evening worktree before publishing; a non-zero exit is recorded
    #: and blocks nothing but the "lint green" claim. `python3 scripts/lint.py` in the vault.
    lint_command: tuple[str, ...] | None = None
    max_p0_tomorrow: int = MAX_P0_TOMORROW

    @classmethod
    def from_mapping(cls, raw: Any) -> EveningConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("evening must be an object")
        _reject_unknown(
            raw,
            {"publish", "reflection", "wiki_local_only", "lint_command", "max_p0_tomorrow"},
            "evening",
        )
        local_only = raw.get("wiki_local_only", False)
        if not isinstance(local_only, bool):
            raise ValueError("evening.wiki_local_only must be a boolean")
        lint_raw = raw.get("lint_command")
        lint: tuple[str, ...] | None = None
        if lint_raw is not None:
            if (
                not isinstance(lint_raw, list)
                or not lint_raw
                or not all(isinstance(x, str) and x.strip() for x in lint_raw)
            ):
                raise ValueError("evening.lint_command must be a non-empty argv list")
            lint = tuple(lint_raw)
        return cls(
            publish=PublishConfig.from_mapping(raw.get("publish")),
            reflection=ReflectionConfig.from_mapping(raw.get("reflection")),
            wiki_local_only=local_only,
            lint_command=lint,
            max_p0_tomorrow=_bounded_int(
                raw.get("max_p0_tomorrow", MAX_P0_TOMORROW),
                0,
                MAX_P0_TOMORROW,
                "evening.max_p0_tomorrow",
            ),
        )


def _reject_unknown(data: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise ValueError(f"{label} has unknown keys: {', '.join(extra)}")


def _bounded(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return text


def _bounded_int(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < low or value > high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return value
