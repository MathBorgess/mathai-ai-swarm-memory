import base64
import json
from urllib.parse import urlsplit

import httpx
import pytest

from app.proposals.github import (
    HttpProposalGitHub,
    ProposalGitHubError,
    ProposalGitHubTimeout,
    ProposalRepository,
)
from app.proposals.schema import ProposalPayloadError

REPO = ProposalRepository(owner="acme", name="discard-t2-inbox", base_ref="t2")
TOKEN = "ghs_synthetic-write-only"
PATH = "pesquisa/tcc/inbox/2026-09-11-prop_aaaaaaaa.md"
BRANCH = "swarm/proposal-prop_aaaaaaaa"
NOTE = "T2 note"


class FakeAPI:
    def __init__(self):
        self.refs = {"refs/heads/t2": "sha-base"}
        self.files = {}
        self.prs = []
        self.calls = []
        self.redirect = False
        self.create_then_timeout = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parsed = urlsplit(str(request.url))
        self.calls.append((request.method, parsed.path, request.read()))
        if self.redirect:
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        path = parsed.path
        if request.method == "GET" and path.endswith("/git/matching-refs/heads/t2"):
            return httpx.Response(200, json=[{"ref": "refs/heads/t2", "object": {"sha": "sha-base"}}])
        if request.method == "GET" and "/git/matching-refs/heads/" in path:
            ref = "refs/heads/" + path.split("/git/matching-refs/heads/", 1)[1]
            sha = self.refs.get(ref)
            if sha is None:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"ref": ref, "object": {"sha": sha}}])
        if request.method == "POST" and path.endswith("/git/refs"):
            payload = json.loads(request.content)
            self.refs[payload["ref"]] = payload["sha"]
            if self.create_then_timeout == "branch":
                self.create_then_timeout = None
                raise httpx.TimeoutException("after create")
            return httpx.Response(201, json={"object": {"sha": payload["sha"]}})
        if request.method == "GET" and "/contents/" in path:
            key = (request.url.params.get("ref"), path.split("/contents/", 1)[1])
            if key not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            content, sha = self.files[key]
            return httpx.Response(200, json={
                "type": "file",
                "sha": sha,
                "content": base64.b64encode(content.encode()).decode(),
            })
        if request.method == "PUT" and "/contents/" in path:
            payload = json.loads(request.content)
            content = base64.b64decode(payload["content"]).decode()
            sha = "sha-commit-1"
            self.files[(payload["branch"], path.split("/contents/", 1)[1])] = (content, sha)
            if self.create_then_timeout == "file":
                self.create_then_timeout = None
                raise httpx.TimeoutException("after file")
            return httpx.Response(201, json={"commit": {"sha": sha}})
        if request.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=self.prs)
        if request.method == "POST" and path.endswith("/pulls"):
            payload = json.loads(request.content)
            number = len(self.prs) + 1
            pr = {
                "number": number,
                "html_url": f"https://github.com/acme/discard-t2-inbox/pull/{number}",
                "draft": True,
                "base": {"ref": payload["base"]},
                "head": {"ref": payload["head"]},
            }
            self.prs.append(pr)
            if self.create_then_timeout == "pr":
                self.create_then_timeout = None
                raise httpx.TimeoutException("after pr")
            return httpx.Response(201, json=pr)
        return httpx.Response(500, json={"message": "unhandled"})


def client(api=None):
    api = api or FakeAPI()
    return HttpProposalGitHub(REPO, TOKEN, transport=httpx.MockTransport(api)), api


def test_happy_path_uses_fixed_repo_prefix_base_and_draft():
    github, api = client()
    assert github.ensure_branch(branch=BRANCH) == "sha-base"
    assert github.ensure_file(branch=BRANCH, path=PATH, content=NOTE, message="T2") == "sha-commit-1"
    number, url = github.ensure_draft_pr(branch=BRANCH, title="t", body="card")
    assert (number, url) == (1, "https://github.com/acme/discard-t2-inbox/pull/1")
    assert all("/repos/acme/discard-t2-inbox/" in path for _, path, _ in api.calls)
    assert not any("/merge" in path for _, path, _ in api.calls)
    assert not any("wiki" in path or "fontes" in path for _, path, _ in api.calls)
    put = json.loads([body for method, path, body in api.calls if method == "PUT"][0])
    assert put["branch"] == BRANCH
    pr = json.loads([body for method, _, body in api.calls if method == "POST" and True][-1])
    assert pr["draft"] is True and pr["base"] == "t2" and pr["head"] == BRANCH


def test_retries_do_not_duplicate_after_timeout_at_each_boundary():
    github, api = client()
    api.create_then_timeout = "branch"
    with pytest.raises(ProposalGitHubTimeout):
        github.ensure_branch(branch=BRANCH)
    assert github.ensure_branch(branch=BRANCH) == "sha-base"
    api.create_then_timeout = "file"
    with pytest.raises(ProposalGitHubTimeout):
        github.ensure_file(branch=BRANCH, path=PATH, content=NOTE, message="T2")
    assert github.ensure_file(branch=BRANCH, path=PATH, content=NOTE, message="T2") == "sha-commit-1"
    api.create_then_timeout = "pr"
    with pytest.raises(ProposalGitHubTimeout):
        github.ensure_draft_pr(branch=BRANCH, title="t", body="card")
    assert github.ensure_draft_pr(branch=BRANCH, title="t", body="card")[0] == 1
    assert len(api.prs) == 1


def test_redirects_and_foreign_paths_are_denied():
    api = FakeAPI()
    api.redirect = True
    github, _ = client(api)
    with pytest.raises(ProposalGitHubError, match="redirect"):
        github.ensure_branch(branch=BRANCH)
    github, _ = client()
    with pytest.raises(ProposalPayloadError):
        github.ensure_file(branch=BRANCH, path="wiki/index.md", content=NOTE, message="no")
    with pytest.raises(ProposalPayloadError):
        github.ensure_file(branch="main", path=PATH, content=NOTE, message="no")


def test_write_token_is_required():
    with pytest.raises(ValueError):
        HttpProposalGitHub(REPO, "")
    captured = []
    api = FakeAPI()

    def capture(request):
        captured.append(request.headers.get("authorization"))
        return api(request)

    github = HttpProposalGitHub(REPO, TOKEN, transport=httpx.MockTransport(capture))
    github.ensure_branch(branch=BRANCH)
    assert captured and all(item == "Bearer " + TOKEN for item in captured)


def test_closed_prefix_and_repo_config():
    with pytest.raises(ValueError):
        ProposalRepository(owner="acme", name="discard", base_ref="t2", allowed_prefix="wiki")
    with pytest.raises(ValueError):
        ProposalRepository(owner="acme/evil", name="discard", base_ref="t2")
