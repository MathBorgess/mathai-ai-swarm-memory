"""Real `gh` transport: argv-only subprocess, JSON where `gh` supports it."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

from swarm_reports.dispatch.gh_adapter import GhRequest, GhTransport, merge_argv


class GhCommandRunner(Protocol):
    def run(self, argv: list[str], *, timeout: float) -> tuple[int, str, str]: ...


@dataclass
class SubprocessGhRunner:
    gh_bin: str = "gh"
    timeout: float = 120.0

    def run(self, argv: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
        if argv and argv[0] == "gh":
            argv = [self.gh_bin, *argv[1:]]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", f"{type(exc).__name__}: {exc}"
        return proc.returncode, proc.stdout, proc.stderr


class GhCliTransport:
    """Send the one authorized GhRequest through `gh`."""

    def __init__(self, runner: GhCommandRunner | None = None, *, gh_bin: str = "gh") -> None:
        self._runner = runner or SubprocessGhRunner(gh_bin=gh_bin)

    def send(self, request: GhRequest) -> None:
        if request.op == "merge":
            argv = merge_argv(request)
            code, _out, err = self._runner.run(argv, timeout=None)
            if code != 0:
                raise RuntimeError(f"gh pr merge failed ({code}): {err[:400]}")
            return
        if request.op == "convert_to_draft":
            argv = [
                "gh",
                "pr",
                "ready",
                str(request.pr_number),
                "--undo",
                "--repo",
                request.repo,
            ]
            code, _out, err = self._runner.run(argv, timeout=None)
            if code != 0:
                raise RuntimeError(f"gh pr draft conversion failed ({code}): {err[:400]}")
            return
        if request.op == "comment":
            body = request.body or ""
            argv = [
                "gh",
                "pr",
                "comment",
                str(request.pr_number),
                "--repo",
                request.repo,
                "--body",
                body,
            ]
            code, _out, err = self._runner.run(argv, timeout=None)
            if code != 0:
                raise RuntimeError(f"gh pr comment failed ({code}): {err[:400]}")
            return
        raise ValueError(f"unsupported gh op {request.op!r}")

    def pr_view_json(self, repo: str, pr_number: int) -> dict:
        argv = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,headRefOid,baseRefOid,files,statusCheckRollup",
        ]
        code, out, err = self._runner.run(argv, timeout=None)
        if code != 0:
            raise RuntimeError(f"gh pr view failed ({code}): {err[:400]}")
        data = json.loads(out or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("gh pr view returned non-object JSON")
        return data

    def pr_diff(self, repo: str, pr_number: int) -> str:
        argv = ["gh", "pr", "diff", str(pr_number), "--repo", repo]
        code, out, err = self._runner.run(argv, timeout=None)
        if code != 0:
            raise RuntimeError(f"gh pr diff failed ({code}): {err[:400]}")
        return out


__all__ = ["GhCliTransport", "GhCommandRunner", "SubprocessGhRunner"]
