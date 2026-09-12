"""GitHub write adapter for T2 proposals. Device Flow tokens never enter here."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.proposals.schema import assert_branch, assert_write_path

API = "https://api.github.com"


class ProposalGitHubError(RuntimeError):
    pass


class ProposalGitHubTimeout(ProposalGitHubError):
    pass


@dataclass(frozen=True)
class ProposalRepository:
    owner: str
    name: str
    base_ref: str
    allowed_prefix: str = "pesquisa/tcc/inbox"

    def __post_init__(self) -> None:
        for part in (self.owner, self.name, self.base_ref):
            if not part or "/" in part or "\\" in part or ".." in part or "\n" in part:
                raise ValueError("Invalid proposal repository")
        if self.allowed_prefix != "pesquisa/tcc/inbox":
            raise ValueError("Proposal prefix is closed")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class ProposalGitHub(Protocol):
    def ensure_branch(self, *, branch: str) -> str: ...
    def ensure_file(self, *, branch: str, path: str, content: str, message: str) -> str: ...
    def ensure_draft_pr(self, *, branch: str, title: str, body: str) -> tuple[int, str]: ...


class HttpProposalGitHub:
    def __init__(self, repository: ProposalRepository, token: str, *, transport: httpx.BaseTransport | None = None):
        if not token or "\n" in token or "\r" in token:
            raise ValueError("A dedicated GitHub write token is required")
        self.repository = repository
        self._token = token
        self._transport = transport

    def ensure_branch(self, *, branch: str) -> str:
        branch = assert_branch(branch)
        existing = self._ref_sha(branch)
        if existing:
            return existing
        base = self._ref_sha(self.repository.base_ref)
        if not base:
            raise ProposalGitHubError("Configured base ref is missing")
        response = self._request(
            "POST",
            f"/repos/{self.repository.full_name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base},
        )
        if response.status_code in {200, 201}:
            sha = ((response.json() or {}).get("object") or {}).get("sha")
            if isinstance(sha, str) and sha:
                return sha
            raise ProposalGitHubError("GitHub branch create failed")
        if response.status_code == 422:
            sha = self._ref_sha(branch)
            if sha:
                return sha
        raise ProposalGitHubError("GitHub branch create failed")

    def ensure_file(self, *, branch: str, path: str, content: str, message: str) -> str:
        branch, path = assert_branch(branch), assert_write_path(path)
        if not path.startswith(self.repository.allowed_prefix + "/"):
            raise ProposalGitHubError("Refusing path outside the closed prefix")
        if branch == "main" or branch == self.repository.base_ref:
            raise ProposalGitHubError("Refusing to write the base branch")
        found = self._file(branch, path)
        encoded = base64.b64encode(content.encode()).decode()
        if found is not None:
            if found["content"] == content:
                return found["sha"]
            raise ProposalGitHubError("Proposal file already exists with different content")
        response = self._request(
            "PUT",
            f"/repos/{self.repository.full_name}/contents/{quote(path)}",
            json={"message": message, "content": encoded, "branch": branch},
        )
        if response.status_code in {200, 201}:
            sha = ((response.json() or {}).get("commit") or {}).get("sha")
            if isinstance(sha, str) and sha:
                return sha
        if response.status_code == 422:
            found = self._file(branch, path)
            if found is not None and found["content"] == content:
                return found["sha"]
        raise ProposalGitHubError("GitHub file create failed")

    def ensure_draft_pr(self, *, branch: str, title: str, body: str) -> tuple[int, str]:
        branch = assert_branch(branch)
        existing = self._pr(branch)
        if existing:
            return existing
        response = self._request(
            "POST",
            f"/repos/{self.repository.full_name}/pulls",
            json={
                "title": title,
                "head": branch,
                "base": self.repository.base_ref,
                "body": body,
                "draft": True,
            },
        )
        if response.status_code in {200, 201}:
            parsed = self._pr_tuple(response.json())
            if parsed:
                return parsed
        if response.status_code == 422:
            existing = self._pr(branch)
            if existing:
                return existing
        raise ProposalGitHubError("GitHub draft pull request failed")

    def _ref_sha(self, name: str) -> str | None:
        response = self._request("GET", f"/repos/{self.repository.full_name}/git/matching-refs/heads/{name}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProposalGitHubError("GitHub ref lookup failed")
        payload = response.json()
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ProposalGitHubError("GitHub ref lookup failed")
        expected = f"refs/heads/{name}"
        for item in payload:
            if isinstance(item, dict) and item.get("ref") == expected:
                sha = ((item.get("object") or {}).get("sha"))
                if isinstance(sha, str) and sha:
                    return sha
        return None

    def _file(self, branch: str, path: str) -> dict | None:
        response = self._request(
            "GET",
            f"/repos/{self.repository.full_name}/contents/{quote(path)}",
            params={"ref": branch},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProposalGitHubError("GitHub file lookup failed")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("type") == "symlink":
            raise ProposalGitHubError("Refusing symlink or unexpected content")
        encoded = payload.get("content")
        sha = payload.get("sha")
        if not isinstance(encoded, str) or not isinstance(sha, str):
            raise ProposalGitHubError("GitHub file lookup failed")
        try:
            content = base64.b64decode(encoded.replace("\n", ""), validate=False).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProposalGitHubError("GitHub file lookup failed") from exc
        return {"sha": sha, "content": content}

    def _pr(self, branch: str) -> tuple[int, str] | None:
        response = self._request(
            "GET",
            f"/repos/{self.repository.full_name}/pulls",
            params={"head": f"{self.repository.owner}:{branch}", "state": "all"},
        )
        if response.status_code != 200:
            raise ProposalGitHubError("GitHub pull request lookup failed")
        payload = response.json()
        if not isinstance(payload, list):
            raise ProposalGitHubError("GitHub pull request lookup failed")
        for item in payload:
            parsed = self._pr_tuple(item)
            if parsed:
                return parsed
        return None

    def _pr_tuple(self, payload: object) -> tuple[int, str] | None:
        if not isinstance(payload, dict) or payload.get("draft") is not True:
            return None
        number, url = payload.get("number"), payload.get("html_url")
        base = ((payload.get("base") or {}).get("ref") if isinstance(payload.get("base"), dict) else None)
        if not isinstance(number, int) or number <= 0 or not isinstance(url, str):
            return None
        if base != self.repository.base_ref:
            return None
        expected = f"https://github.com/{self.repository.full_name}/pull/{number}"
        if url != expected:
            return None
        return number, url

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = API + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise ProposalGitHubError("Refusing non-GitHub API host")
        expected = f"/repos/{self.repository.full_name}/"
        if not parsed.path.startswith(expected):
            raise ProposalGitHubError("Refusing unexpected repository")
        try:
            with httpx.Client(
                transport=self._transport, timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                response = client.request(
                    method,
                    url,
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    **kwargs,
                )
        except httpx.TimeoutException as exc:
            raise ProposalGitHubTimeout("GitHub request timed out") from None
        except httpx.HTTPError as exc:
            raise ProposalGitHubError("GitHub request failed") from None
        if 300 <= response.status_code < 400:
            raise ProposalGitHubError("Refusing GitHub redirect")
        return response
