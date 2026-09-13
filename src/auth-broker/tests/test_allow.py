"""Owner allow-by-login: unkeyed principals, GitHub numeric identity, refusals."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.github import GitHubIdentityError, lookup_github_login, lookup_github_subject
from app.adapters.sqlite import SqlitePairingStore
from app.api import create_app
from app.cli import main
from swarm_helpers import (
    AUDIENCE,
    GitHubOAuth,
    NOW,
    PUBLIC_URL,
    WORKSPACE,
    FakeHermes,
    dpop_headers,
    es256_material,
)

USERS = "https://api.github.com/users/"


class FakeGitHubUsers:
    def __init__(self, accounts: dict[str, int] | None = None):
        self.by_login: dict[str, dict] = {}
        self.by_id: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.redirect_to: str | None = None
        self.status_for: dict[str, int] = {}
        self.payload_for: dict[str, object] = {}
        for login, subject in (accounts or {}).items():
            self.add(login, subject)

    def add(self, login: str, subject: int | str) -> None:
        payload = {"id": int(subject), "login": login}
        self.by_login[login.lower()] = payload
        self.by_id[str(int(subject))] = payload

    def drop_login(self, login: str) -> None:
        self.by_login.pop(login.lower(), None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parsed = urlsplit(str(request.url))
        url = parsed._replace(query="", fragment="").geturl()
        self.calls.append((request.method, url))
        if self.redirect_to:
            return httpx.Response(302, headers={"Location": self.redirect_to})
        if url in self.status_for:
            payload = self.payload_for.get(url, {"message": "error"})
            return httpx.Response(self.status_for[url], json=payload)
        if url in self.payload_for:
            return httpx.Response(200, json=self.payload_for[url])
        path = parsed.path
        if request.method == "GET" and path.startswith("/users/"):
            login = path[len("/users/"):]
            payload = self.by_login.get(login.lower())
            if payload is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=payload)
        if request.method == "GET" and path.startswith("/user/"):
            subject = path[len("/user/"):]
            payload = self.by_id.get(subject)
            if payload is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=payload)
        return httpx.Response(500, json={"message": "unhandled"})


def transport(api: FakeGitHubUsers) -> httpx.MockTransport:
    return httpx.MockTransport(api)


def open_store(path) -> SqlitePairingStore:
    return SqlitePairingStore(path)


def allow_cli(store, login, *, api, role="advisor", scopes=None, ttl=30, now=NOW):
    scopes = scopes or ["ctx:read:pesquisa.tcc"]
    argv = ["--store", str(store), "allow", login, "--role", role, "--ttl", str(ttl)]
    for scope in scopes:
        argv.extend(["--scope", scope])
    return main(argv, github_transport=transport(api), now=now)


def test_lookup_uses_fixed_github_endpoints_and_returns_numeric_subject():
    api = FakeGitHubUsers({"Calegario": 4242})
    account = lookup_github_login("@Calegario", transport=transport(api))
    assert account.subject == "4242"
    assert account.login == "Calegario"
    assert api.calls == [("GET", "https://api.github.com/users/Calegario")]
    by_id = lookup_github_subject("4242", transport=transport(api))
    assert by_id == account
    assert api.calls[1] == ("GET", "https://api.github.com/user/4242")


@pytest.mark.parametrize(
    "login",
    ["", "@", "user/repo", "https://github.com/calegario", "../admin", "alice bob", "a" * 40],
)
def test_lookup_refuses_invalid_login_without_http(login):
    api = FakeGitHubUsers({"calegario": 1})
    with pytest.raises((ValueError, GitHubIdentityError)):
        lookup_github_login(login, transport=transport(api))
    assert api.calls == []


@pytest.mark.parametrize(
    "setup",
    ["missing", "redirect", "non_object", "bad_id", "bad_login"],
)
def test_lookup_refuses_untrusted_github_responses(setup):
    api = FakeGitHubUsers({"calegario": 4242})
    url = USERS + "calegario"
    if setup == "missing":
        api.drop_login("calegario")
    elif setup == "redirect":
        api.redirect_to = "https://evil.example/steal"
    elif setup == "non_object":
        api.payload_for[url] = ["not", "an", "object"]
    elif setup == "bad_id":
        api.payload_for[url] = {"id": "4242", "login": "calegario"}
    else:
        api.payload_for[url] = {"id": 4242, "login": ""}
    with pytest.raises(GitHubIdentityError):
        lookup_github_login("calegario", transport=transport(api))


def test_v2_store_gains_unkeyed_principals_without_rewriting_keyed_rows(tmp_path):
    path = tmp_path / "v2.sqlite3"
    store = open_store(path)
    try:
        keyed = store.add_principal(
            "advisor-01", github_subject="12345", jwk=es256_material()[2], role="advisor", now=NOW,
        )
        assert keyed.jwk["kty"] == "EC"
        unkeyed = store.add_principal(
            "github:4242", github_subject="4242", role="advisor", now=NOW, github_login="calegario",
        )
        assert unkeyed.jwk is None
        assert unkeyed.jwk_thumbprint is None
        assert unkeyed.github_login == "calegario"
    finally:
        store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        keyed_row = db.execute(
            "SELECT jwk, jwk_thumbprint, github_login FROM principals WHERE id = 'advisor-01'"
        ).fetchone()
        bare = db.execute(
            "SELECT jwk, jwk_thumbprint, github_login FROM principals WHERE id = 'github:4242'"
        ).fetchone()
        assert json.loads(keyed_row[0])["kty"] == "EC"
        assert keyed_row[1]
        assert bare[0] in ("", None)
        assert bare[1] in ("", None)
        assert bare[2] == "calegario"
        dump = "\n".join(db.iterdump())
    assert '"kty": "oct"' not in dump
    reopened = open_store(path)
    try:
        loaded = reopened.get_principal("github:4242")
        assert loaded.jwk is None
        assert loaded.jwk_thumbprint is None
        assert reopened.get_principal("advisor-01").jwk["kty"] == "EC"
        assert reopened.get_principal_by_github_subject("4242").id == "github:4242"
        assert reopened.get_principal_by_github_subject("12345").id == "advisor-01"
    finally:
        reopened.close()


def test_username_rename_does_not_transfer_grant_and_cache_is_not_identity(tmp_path):
    path = tmp_path / "broker.sqlite3"
    api = FakeGitHubUsers({"alice": 111})
    assert allow_cli(path, "@alice", api=api) == 0
    with closing(open_store(path)) as store:
        original = store.get_principal_by_github_subject("111")
        assert original.id == "github:111"
        assert original.github_login == "alice"
        assert [g.scope for g in store.list_grants("github:111", NOW)] == ["ctx:read:pesquisa.tcc"]
        store.connection.execute(
            "UPDATE principals SET github_login = 'bob' WHERE id = 'github:111'"
        )
        store.connection.commit()
        assert store.get_principal_by_github_subject("111").id == "github:111"
        assert store.get_principal_by_github_subject("222") is None
    api.drop_login("alice")
    api.add("alice2", 111)
    api.add("alice", 222)
    assert allow_cli(path, "@alice", api=api, scopes=["ctx:propose:pesquisa.tcc"]) == 0
    with closing(open_store(path)) as store:
        kept = store.get_principal_by_github_subject("111")
        new = store.get_principal_by_github_subject("222")
        assert kept.github_subject == "111"
        assert [g.scope for g in store.list_grants("github:111", NOW)] == ["ctx:read:pesquisa.tcc"]
        assert new.id == "github:222"
        assert new.github_login == "alice"
        assert [g.scope for g in store.list_grants("github:222", NOW)] == ["ctx:propose:pesquisa.tcc"]


def test_allow_is_atomic_and_validates_before_mutation(tmp_path):
    path = tmp_path / "broker.sqlite3"
    api = FakeGitHubUsers({"calegario": 4242})
    assert allow_cli(path, "@calegario", api=api, ttl=0) == 2
    assert allow_cli(path, "@calegario", api=api, scopes=["ctx:write:wiki"]) == 2
    assert api.calls == []
    with closing(open_store(path)) as store:
        assert store.list_principals() == []
    api.drop_login("calegario")
    assert allow_cli(path, "@calegario", api=api) == 2
    with closing(open_store(path)) as store:
        assert store.list_principals() == []
        assert store.connection.execute("SELECT COUNT(*) FROM grant_audit_events").fetchone()[0] == 0
        store.connection.execute(
            """CREATE TRIGGER fail_second_grant BEFORE INSERT ON grants
               WHEN (SELECT COUNT(*) FROM grants) >= 1
               BEGIN SELECT RAISE(ABORT, 'simulated grant failure'); END"""
        )
        store.connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            store.allow_github_principal(
                github_subject="4242",
                github_login="calegario",
                role="advisor",
                scopes=("ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"),
                now=NOW,
                expires_at=NOW + timedelta(days=30),
            )
        assert store.list_principals() == []
        assert store.list_grants("github:4242", NOW) == []


def test_unkeyed_principal_cannot_create_family_until_key_enrollment(tmp_path):
    path = tmp_path / "broker.sqlite3"
    store = open_store(path)
    try:
        store.add_principal("github:4242", github_subject="4242", role="advisor", now=NOW)
        store.set_grant("github:4242", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=30))
        with pytest.raises(ValueError, match="enrolled"):
            store.create_family(
                principal_id="github:4242",
                workspace_id=WORKSPACE,
                refresh_token_hash="abc",
                requested_scopes=("ctx:read:pesquisa.tcc",),
                now=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        _, _, jwk, thumb = es256_material()
        enrolled = store.enroll_principal_key("github:4242", jwk, NOW)
        assert enrolled.jwk_thumbprint == thumb
        family = store.create_family(
            principal_id="github:4242",
            workspace_id=WORKSPACE,
            refresh_token_hash="abc",
            requested_scopes=("ctx:read:pesquisa.tcc",),
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        assert family.principal_id == "github:4242"
        with pytest.raises(ValueError, match="already"):
            store.enroll_principal_key("github:4242", es256_material()[2], NOW)
    finally:
        store.close()


def test_cli_allow_creates_unkeyed_principal_and_keyed_dpop_still_works(tmp_path, capsys):
    path = tmp_path / "cli.sqlite3"
    api = FakeGitHubUsers({"calegario": 4242})
    assert allow_cli(
        path, "@calegario", api=api,
        scopes=["ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc"],
        ttl=14,
    ) == 0
    out = capsys.readouterr().out
    assert "github:4242" in out
    assert "4242" in out
    with closing(open_store(path)) as store:
        principal = store.get_principal_by_github_subject("4242")
        assert principal.jwk is None
        assert principal.role == "advisor"
        assert principal.github_login == "calegario"
        assert {g.scope for g in store.list_grants("github:4242", NOW)} == {
            "ctx:read:pesquisa.tcc", "ctx:propose:pesquisa.tcc",
        }
        assert all(g.expires_at == NOW + timedelta(days=14) for g in store.list_grants("github:4242", NOW))
    key, pem, jwk, _ = es256_material()
    with closing(open_store(path)) as store:
        store.add_principal("advisor-01", github_subject="12345", jwk=jwk, role="advisor", now=NOW)
        store.set_grant("advisor-01", "ctx:read:pesquisa.tcc", NOW, NOW + timedelta(days=30))
    app = create_app(
        database_path=path, audience=AUDIENCE, owner_verifier=object(), hermes=FakeHermes(),
        clock=lambda: NOW, github_oauth=GitHubOAuth(), github_allowed_user_id="12345",
        token_signing_key=pem, workspace_id=WORKSPACE, public_url=PUBLIC_URL,
    )
    with TestClient(app) as client:
        denied = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "github:4242", "scopes": ["ctx:read:pesquisa.tcc"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start"),
        )
        assert denied.json() == {"error": "access_denied"}
        start = client.post(
            "/v1/oauth/github/device/start",
            json={"principal_id": "advisor-01", "scopes": ["ctx:read:pesquisa.tcc"]},
            headers=dpop_headers(key, method="POST", path="/v1/oauth/github/device/start"),
        )
        assert "device_code" in start.json()


def test_get_principal_by_github_subject_prefers_canonical_after_key_enrollment(tmp_path):
    store = open_store(tmp_path / "broker.sqlite3")
    try:
        store.add_principal(
            "advisor-01", github_subject="4242", jwk=es256_material()[2], role="advisor", now=NOW,
        )
        store.add_principal(
            "github:4242", github_subject="4242", role="advisor", now=NOW + timedelta(seconds=1),
            github_login="calegario",
        )
        found = store.get_principal_by_github_subject("4242")
        assert found.id == "github:4242"
        assert found.jwk is None
        enrolled = store.enroll_principal_key("github:4242", es256_material()[2], NOW)
        assert enrolled.jwk is not None
        still = store.get_principal_by_github_subject("4242")
        assert still.id == "github:4242"
        assert still.jwk_thumbprint == enrolled.jwk_thumbprint
        with pytest.raises(ValueError):
            store.get_principal_by_github_subject("calegario")
    finally:
        store.close()


def test_get_principal_by_github_subject_fails_closed_on_ambiguous_legacy_rows(tmp_path):
    store = open_store(tmp_path / "broker.sqlite3")
    try:
        store.add_principal(
            "advisor-01", github_subject="4242", jwk=es256_material()[2], role="advisor", now=NOW,
        )
        store.add_principal(
            "advisor-02", github_subject="4242", jwk=es256_material()[2], role="advisor",
            now=NOW + timedelta(seconds=1),
        )
        assert store.get_principal_by_github_subject("4242") is None
        store.add_principal(
            "github:4242", github_subject="4242", role="advisor", now=NOW + timedelta(seconds=2),
        )
        assert store.get_principal_by_github_subject("4242").id == "github:4242"
    finally:
        store.close()


def test_get_principal_by_github_subject_selects_single_legacy_row(tmp_path):
    store = open_store(tmp_path / "broker.sqlite3")
    try:
        store.add_principal(
            "advisor-01", github_subject="4242", jwk=es256_material()[2], role="advisor", now=NOW,
        )
        found = store.get_principal_by_github_subject("4242")
        assert found.id == "advisor-01"
    finally:
        store.close()
