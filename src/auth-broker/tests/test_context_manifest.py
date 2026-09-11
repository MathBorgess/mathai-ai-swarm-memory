"""Manifest ingest: invalid metadata is withheld; paths cannot escape the root."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.context.query import AuthError, resolve_handles, search
from app.context.store import ContextStore, ingest_manifest

READ = "ctx:read:pesquisa.tcc"


def advisor(**overrides):
    values = {
        "principal_id": "advisor-01",
        "workspace_id": "personal",
        "scopes": (READ,),
        "classifications": ("public", "shared"),
        "family_id": "fam-advisor",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    values.update(overrides)
    return values


def owner(**overrides):
    values = advisor(principal_id="owner-01", family_id="fam-owner")
    values["classifications"] = ("public", "shared", "internal", "restricted")
    values.update(overrides)
    return values


def write_pages(root: Path, pages: dict[str, str]) -> None:
    for rel, content in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def entry(path, **overrides):
    item = {
        "path": path,
        "namespace": "pesquisa.tcc",
        "classification": "shared",
        "layer": "notes",
    }
    item.update(overrides)
    return item


def manifest(entries, workspace="personal", revision="rev-1"):
    return {"workspace_id": workspace, "source_revision": revision, "entries": entries}


def abc_pages():
    return {
        "pesquisa/tcc/a.md": "# Visible A\n\ntoken-a\nNext: [[Hidden B]]\n",
        "pesquisa/tcc/b.md": "# Hidden B\n\ntoken-b secret-bridge\nFrom [[Visible A]] to [[Visible C]]\n",
        "pesquisa/tcc/c.md": "# Visible C\n\ntoken-c\nPrevious: [[Hidden B]]\n",
        "pesquisa/tcc/notes.md": (
            "# Visible Notes\n\ntoken-notes\n"
            "See [[Visible Notes]] and [[Hidden B]] and [hidden](pesquisa/tcc/b.md).\n"
            "Backlinks: [[Hidden B]]\n"
        ),
        "pesquisa/tcc/unlisted.md": "# Unlisted\n\ntoken-unlisted\n",
        "wiki/secret.md": "# Wiki Secret\n\ntoken-wiki\n",
    }


def abc_entries(**b_overrides):
    return [
        entry("pesquisa/tcc/a.md"),
        entry("pesquisa/tcc/b.md", private=True, **b_overrides),
        entry("pesquisa/tcc/c.md"),
        entry("pesquisa/tcc/notes.md"),
    ]


def ingest_abc(tmp_path, extra_entries=(), extra_pages=None, revision="rev-1"):
    root = tmp_path / "content"
    pages = abc_pages()
    if extra_pages:
        pages.update(extra_pages)
    write_pages(root, pages)
    store = ContextStore(tmp_path / "context.sqlite3")
    ingest_manifest(store, manifest(abc_entries() + list(extra_entries), revision=revision), root)
    return store, root


def titles(store, workspace="personal"):
    return {node.title: node for node in store.nodes(workspace)}


def test_missing_or_unknown_metadata_is_not_queryable(tmp_path):
    store, root = ingest_abc(
        tmp_path,
        extra_entries=[
            {"path": "pesquisa/tcc/a.md", "namespace": "pesquisa.tcc"},
            {"path": "pesquisa/tcc/missing.md", "namespace": "pesquisa.tcc", "layer": "notes"},
            entry("wiki/secret.md", namespace="wiki"),
            entry("pesquisa/tcc/bad-private.md", private="yes"),
            {"path": "pesquisa/tcc/c.md", "namespace": "pesquisa.tcc", "classification": "shared", "layer": "notes", "role": "owner"},
        ],
        extra_pages={
            "pesquisa/tcc/missing.md": "# Missing File Pointer\n\ntoken-missing-meta\n",
            "pesquisa/tcc/bad-private.md": "# Bad Private\n\ntoken-bad-private\n",
        },
    )
    found = {node.title for node in store.nodes("personal")}
    assert found == {"Visible A", "Hidden B", "Visible C", "Visible Notes"}
    empty = search(store, advisor(), "token-unlisted", 10)
    assert empty["items"] == []
    assert search(store, advisor(), "token-wiki", 10)["items"] == []
    assert search(store, advisor(), "token-missing-meta", 10)["items"] == []
    assert search(store, advisor(), "token-bad-private", 10)["items"] == []
    store.close()


def test_unlisted_disk_file_is_not_ingested(tmp_path):
    store, _ = ingest_abc(tmp_path)
    assert "Unlisted" not in titles(store)
    assert search(store, owner(), "token-unlisted", 10)["items"] == []
    store.close()


def test_traversal_and_symlink_escape_are_withheld(tmp_path):
    root = tmp_path / "content"
    outside = tmp_path / "outside.md"
    outside.write_text("# Escaped\n\ntoken-escape\n", encoding="utf-8")
    write_pages(root, {"pesquisa/tcc/ok.md": "# Ok\n\ntoken-ok\n"})
    link = root / "pesquisa/tcc/link.md"
    link.symlink_to(outside)
    store = ContextStore(tmp_path / "context.sqlite3")
    ingest_manifest(
        store,
        manifest(
            [
                entry("pesquisa/tcc/ok.md"),
                entry("pesquisa/tcc/link.md"),
                entry("pesquisa/tcc/../../outside.md"),
                entry("/etc/passwd"),
            ]
        ),
        root,
    )
    assert {node.title for node in store.nodes("personal")} == {"Ok"}
    assert search(store, advisor(), "token-escape", 10)["items"] == []
    dump = (tmp_path / "context.sqlite3").read_bytes()
    assert b"/etc/passwd" not in dump
    store.close()


def test_namespace_prefix_is_required(tmp_path):
    root = tmp_path / "content"
    write_pages(root, {"other/x.md": "# Other\n\ntoken-other-path\n"})
    store = ContextStore(tmp_path / "context.sqlite3")
    ingest_manifest(store, manifest([entry("other/x.md")]), root)
    assert store.nodes("personal") == ()
    store.close()


def test_private_true_forces_restricted_even_with_shared_label(tmp_path):
    store, _ = ingest_abc(tmp_path)
    hidden = titles(store)["Hidden B"]
    assert hidden.private is True
    assert hidden.classification == "restricted"
    store.close()


def test_reindex_keeps_handle_for_same_revision_only(tmp_path):
    store, root = ingest_abc(tmp_path, revision="rev-1")
    first = titles(store)["Visible A"].handle
    ingest_manifest(store, manifest(abc_entries(), revision="rev-1"), root)
    assert titles(store)["Visible A"].handle == first
    ingest_manifest(store, manifest(abc_entries(), revision="rev-2"), root)
    second = titles(store)["Visible A"].handle
    assert second != first
    assert "/" not in first and "." not in first
    missing = resolve_handles(store, advisor(), [first])
    current = resolve_handles(store, advisor(), [second])
    assert missing["items"] == []
    assert current["items"][0]["handle"] == second
    assert current["items"][0]["source_revision"] == "rev-2"
    store.close()


def test_operator_json_file_is_accepted(tmp_path):
    store, root = ingest_abc(tmp_path)
    store.close()
    store = ContextStore(tmp_path / "context.sqlite3")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(abc_entries(), revision="rev-file")), encoding="utf-8")
    ingest_manifest(store, path, root)
    assert titles(store)["Visible A"].source_revision == "rev-file"
    store.close()


def test_invalid_manifest_shape_fails_closed(tmp_path):
    store = ContextStore(tmp_path / "context.sqlite3")
    with pytest.raises(ValueError):
        ingest_manifest(store, {"workspace_id": "personal"}, tmp_path)
    store.close()


def test_owner_without_read_scope_cannot_search(tmp_path):
    store, _ = ingest_abc(tmp_path)
    with pytest.raises(AuthError) as error:
        search(store, owner(scopes=("ctx:propose:pesquisa.tcc",)), "token-a", 10)
    assert error.value.status == 403
    store.close()
