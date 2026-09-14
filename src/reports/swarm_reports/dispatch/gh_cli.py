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
                raise RuntimeError(f"gh pr merge failed ({code}); diagnostic omitted")
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
                raise RuntimeError(f"gh pr draft conversion failed ({code}); diagnostic omitted")
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
                raise RuntimeError(f"gh pr comment failed ({code}); diagnostic omitted")
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
            "number,title,body,headRefOid,baseRefOid,baseRefName,files,statusCheckRollup",
        ]
        code, out, err = self._runner.run(argv, timeout=None)
        if code != 0:
            raise RuntimeError(f"gh pr view failed ({code}); diagnostic omitted")
        data = json.loads(out or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("gh pr view returned non-object JSON")
        # GraphQL `files` omits status and rename origin. REST carries both.
        files = []
        for page in range(1, 31):
            code, out, _err = self._runner.run(["gh", "api", "--method", "GET",
                f"repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"], timeout=None)
            if code:
                raise RuntimeError("gh changed-file metadata unavailable")
            rows = json.loads(out)
            if not isinstance(rows, list):
                raise RuntimeError("gh changed-file metadata invalid")
            files.extend(rows)
            if len(rows) < 100:
                break
        else:
            raise RuntimeError("gh changed-file list exceeds bounded completeness limit")
        data["files"] = files
        # `--required` asks GitHub which contexts matter; empty/error is unknown.
        code, out, _err = self._runner.run(["gh", "pr", "checks", str(pr_number), "--repo", repo,
            "--required", "--json", "name,bucket"], timeout=None)
        required = json.loads(out) if code == 0 else []
        # The rollup must name the same head; view is checked again before merge.
        data["requiredChecks"] = [{"head_sha": data["headRefOid"],
            "conclusion": "success" if c.get("bucket") == "pass" else "failure"} for c in required]
        code, latest, _err = self._runner.run(["gh", "pr", "view", str(pr_number), "--repo", repo,
            "--json", "headRefOid"], timeout=None)
        if code or json.loads(latest).get("headRefOid") != data["headRefOid"]:
            data["requiredChecks"] = []
        return data

    def pr_diff(self, repo: str, pr_number: int) -> str:
        argv = ["gh", "pr", "diff", str(pr_number), "--repo", repo]
        code, out, err = self._runner.run(argv, timeout=None)
        if code != 0:
            raise RuntimeError(f"gh pr diff failed ({code}); diagnostic omitted")
        return out


__all__ = ["GhCliTransport", "GhCommandRunner", "SubprocessGhRunner"]
