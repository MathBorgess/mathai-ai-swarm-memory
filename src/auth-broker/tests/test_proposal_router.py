import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.proposals.github import ProposalGitHubError, ProposalGitHubTimeout, ProposalRepository
from app.proposals.router import build_router
from app.proposals.store import ProposalStore

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
REPO = ProposalRepository(owner="acme", name="discard-t2-inbox", base_ref="t2")
BODY = {
    "namespace": "pesquisa.tcc",
    "title": "Nota de leitura",
    "body_markdown": "Afirmação não verificada.",
    "sources": [{"url": "https://example.test/paper", "label": "paper"}],
}


class FakeGitHub:
    def __init__(self):
        self.repository = REPO
        self.lock = threading.Lock()
        self.branches = {}
        self.files = {}
        self.prs = {}
        self.calls = []
        self.fail_after = None
        self._n = 0

    def ensure_branch(self, *, branch):
        with self.lock:
            self._check(branch=branch)
            self.calls.append(("branch", branch))
            self.branches.setdefault(branch, "sha-" + branch)
            if self.fail_after == "branch":
                self.fail_after = None
                raise ProposalGitHubTimeout("after branch")
            return self.branches[branch]

    def ensure_file(self, *, branch, path, content, message):
        with self.lock:
            self._check(branch=branch, path=path)
            self.calls.append(("file", branch, path))
            key = (branch, path)
            self.files.setdefault(key, ("sha-file", content))
            if self.files[key][1] != content:
                raise ProposalGitHubError("different content")
            if self.fail_after == "file":
                self.fail_after = None
                raise ProposalGitHubTimeout("after file")
            return self.files[key][0]

    def ensure_draft_pr(self, *, branch, title, body):
        with self.lock:
            self._check(branch=branch)
            self.calls.append(("pr", branch, title, body))
            if branch not in self.prs:
                self._n += 1
                self.prs[branch] = (
                    self._n,
                    f"https://github.com/{REPO.full_name}/pull/{self._n}",
                    True,
                    REPO.base_ref,
                )
            if self.fail_after == "pr":
                self.fail_after = None
                raise ProposalGitHubTimeout("after pr")
            number, url, draft, base = self.prs[branch]
            assert draft is True and base == "t2"
            return number, url

    def _check(self, *, branch, path=None):
        assert self.repository == REPO
        assert branch.startswith("swarm/proposal-")
        assert branch != "main" and branch != REPO.base_ref
        if path is not None:
            assert path.startswith("pesquisa/tcc/inbox/")
            assert not path.startswith("wiki/") and "fontes/" not in path
            assert ".." not in path


def auth_for(*, principal="advisor-01", workspace="personal", scopes=("ctx:propose:pesquisa.tcc",),
            expired=False, unauthorized=False, from_json=False):
    def authorize(request):
        if unauthorized:
            raise HTTPException(401, "Missing session")
        mapping = {
            "principal_id": principal,
            "workspace_id": workspace,
            "scopes": scopes,
            "classifications": ("public", "shared"),
            "family_id": "fam-1",
            "expires_at": NOW + timedelta(minutes=-1 if expired else 5),
        }
        if from_json:
            raise AssertionError("caller JSON must not populate auth")
        return mapping
    return authorize


def app_client(tmp_path, *, github=None, authorize=None, store=None):
    store = store or ProposalStore(tmp_path / "proposals.sqlite3")
    github = FakeGitHub() if github is None else github
    app = FastAPI()
    app.include_router(build_router(
        authorize=authorize or auth_for(),
        store=store,
        github=github,
        repository=REPO,
        clock=lambda: NOW,
    ))
    return TestClient(app), github, store


def post(client, body=None, key="idem-1", **kwargs):
    headers = {"Idempotency-Key": key, **kwargs.pop("headers", {})}
    return client.post("/v1/context/propose", json=body or BODY, headers=headers, **kwargs)


def test_propose_creates_draft_inbox_pr(tmp_path):
    client, github, store = app_client(tmp_path)
    response = post(client)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert data["branch"].startswith("swarm/proposal-")
    assert data["pr_url"] == "https://github.com/acme/discard-t2-inbox/pull/1"
    assert data["branch"] not in {"main", "t2"}
    path = store.get(data["proposal_id"]).path
    assert path == f"pesquisa/tcc/inbox/2026-09-11-{data['proposal_id']}.md"
    assert github.calls[0][0] == "branch"
    assert all(call[0] != "file" or call[2].startswith("pesquisa/tcc/inbox/") for call in github.calls)
    note = store.get(data["proposal_id"]).note
    assert "layer: T2" in note and "promotes_to_wiki: false" in note
    store.close()


def test_retry_same_key_does_not_open_second_pr(tmp_path):
    client, github, _ = app_client(tmp_path)
    first, second = post(client), post(client)
    assert first.json() == second.json()
    assert len(github.prs) == 1


def test_same_key_different_body_is_409(tmp_path):
    client, github, _ = app_client(tmp_path)
    assert post(client).status_code == 200
    other = dict(BODY, title="outro")
    assert post(client, body=other).status_code == 409
    assert len(github.prs) == 1


@pytest.mark.parametrize("scopes,status", [
    (("ctx:propose:pesquisa.tcc",), 200),
    (("ctx:read:pesquisa.tcc",), 403),
    (("ctx:propose:wiki",), 403),
    ((), 403),
])
def test_scope_gate(tmp_path, scopes, status):
    client, github, _ = app_client(tmp_path, authorize=auth_for(scopes=scopes))
    assert post(client).status_code == status
    assert (len(github.prs) == 1) is (status == 200)


def test_missing_auth_and_expired_are_401(tmp_path):
    client, github, _ = app_client(tmp_path, authorize=auth_for(unauthorized=True))
    assert post(client).status_code == 401
    client, github, _ = app_client(tmp_path, authorize=auth_for(expired=True))
    assert post(client).status_code == 401
    assert not github.prs


def test_workspace_and_principal_isolation(tmp_path):
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    github = FakeGitHub()
    a, _, _ = app_client(tmp_path, github=github, store=store, authorize=auth_for())
    b, _, _ = app_client(tmp_path, github=github, store=store,
                         authorize=auth_for(principal="advisor-02"))
    c, _, _ = app_client(tmp_path, github=github, store=store,
                         authorize=auth_for(workspace="other"))
    urls = {post(a).json()["pr_url"], post(b).json()["pr_url"], post(c).json()["pr_url"]}
    assert len(urls) == 3
    store.close()


def test_missing_idempotency_key_is_400(tmp_path):
    client, github, _ = app_client(tmp_path)
    assert client.post("/v1/context/propose", json=BODY).status_code == 400
    assert not github.prs
    client, github, _ = app_client(tmp_path)
    for extra in ({"path": "wiki/x.md"}, {"branch": "main"}, {"principal_id": "root"}):
        assert post(client, body={**BODY, **extra}).status_code == 400
    assert not github.prs


def test_large_body_and_markdown_are_413(tmp_path):
    client, _, _ = app_client(tmp_path)
    huge = dict(BODY, body_markdown="x" * 12289)
    assert post(client, body=huge).status_code == 413
    assert client.post(
        "/v1/context/propose",
        content=b'{"namespace":"pesquisa.tcc"}' + b" " * 16385,
        headers={"Content-Type": "application/json", "Idempotency-Key": "k"},
    ).status_code == 413


def test_missing_github_is_503_without_invented_url(tmp_path):
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    app = FastAPI()
    app.include_router(build_router(
        authorize=auth_for(), store=store, github=None, repository=REPO, clock=lambda: NOW,
    ))
    with TestClient(app) as client:
        response = post(client)
    assert response.status_code == 503
    assert "pr_url" not in response.json() or "github.com" not in str(response.json())
    store.close()


@pytest.mark.parametrize("fail_after", ["branch", "file", "pr"])
def test_timeout_then_retry_completes_single_pr(tmp_path, fail_after):
    github = FakeGitHub()
    github.fail_after = fail_after
    client, _, store = app_client(tmp_path, github=github)
    first = post(client, key="retry-me")
    assert first.status_code == 503
    second = post(client, key="retry-me")
    assert second.status_code == 200
    assert second.json()["pr_url"].endswith("/pull/1")
    assert len(github.prs) == 1
    store.close()


def test_concurrent_retries_open_one_pr(tmp_path):
    github = FakeGitHub()
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    client, _, _ = app_client(tmp_path, github=github, store=store)
    from app.proposals.router import publish
    from app.proposals.schema import parse_proposal, render_note

    payload = parse_proposal(BODY)

    def once(i):
        claimed = store.claim(
            workspace_id="personal",
            principal_id="advisor-01",
            idempotency_key="same",
            payload_hash=payload.payload_hash(),
            proposal_id=f"prop_{i:08d}xxxx",
            namespace=payload.namespace,
            title=payload.title,
            note=render_note(
                proposal_id=f"prop_{i:08d}xxxx",
                principal_id="advisor-01",
                workspace_id="personal",
                created_at=NOW,
                payload=payload,
            ),
            now=NOW,
        )
        return publish(store, github, claimed, NOW).pr_url

    with ThreadPoolExecutor(max_workers=8) as pool:
        urls = list(pool.map(once, range(8)))
    assert len(set(urls)) == 1
    assert len(github.prs) == 1
    assert post(client, key="same").json()["pr_url"] == urls[0]
    store.close()


def test_reopen_db_returns_same_pr(tmp_path):
    github = FakeGitHub()
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    client, _, _ = app_client(tmp_path, github=github, store=store)
    first = post(client).json()
    store.close()
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    client, _, _ = app_client(tmp_path, github=github, store=store)
    second = post(client).json()
    assert first == second
    store.close()
