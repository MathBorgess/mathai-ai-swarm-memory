"""Negative ACL tests: filter nodes/edges before ranking, limit or traversal."""

import pytest

from app.context.query import AuthError, resolve_handles, search
from test_context_manifest import (
    abc_entries,
    advisor,
    entry,
    ingest_abc,
    ingest_manifest,
    manifest,
    owner,
    titles,
    write_pages,
)

RECEIPT_KEYS = {"principal_id", "workspace_id", "scopes_used", "pass_as", "policy_version"}
ITEM_KEYS = {"handle", "text", "source_revision"}


def _receipt(payload, principal):
    receipt = payload["capability_receipt"]
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["principal_id"] == principal["principal_id"]
    assert receipt["workspace_id"] == principal["workspace_id"]
    assert receipt["scopes_used"] == ["ctx:read:pesquisa.tcc"]
    assert receipt["pass_as"] == "handle"
    assert receipt["policy_version"] == "v1"
    assert "namespaces_omitted" not in payload and "nodes_redacted" not in payload
    assert "may_disclose_to" not in receipt
    assert "count" not in receipt and "total" not in payload
    return receipt


def test_private_node_is_invisible_to_advisor_and_matches_unknown_query(tmp_path):
    store, _ = ingest_abc(tmp_path)
    principal = advisor()
    denied = search(store, principal, "token-b", 10)
    missing = search(store, principal, "zzzz-not-indexed", 10)
    assert denied["items"] == missing["items"] == []
    assert denied["capability_receipt"] == missing["capability_receipt"]
    _receipt(denied, principal)
    visible = search(store, principal, "token-a", 10)
    assert [item["text"] for item in visible["items"]]
    assert all("token-b" not in item["text"] for item in visible["items"])
    assert all("Hidden B" not in item["text"] for item in visible["items"])
    owned = search(store, owner(), "token-b", 10)
    assert owned["items"] and "token-b" in owned["items"][0]["text"]
    store.close()


def test_query_does_not_traverse_hidden_bridge(tmp_path):
    store, _ = ingest_abc(tmp_path)
    principal = advisor()
    from_a = search(store, principal, "token-a", 10)
    from_c = search(store, principal, "token-c", 10)
    from_bridge = search(store, principal, "secret-bridge", 10)
    assert [item["handle"] for item in from_a["items"]] == [titles(store)["Visible A"].handle]
    assert [item["handle"] for item in from_c["items"]] == [titles(store)["Visible C"].handle]
    assert from_bridge["items"] == []
    owner_bridge = search(store, owner(), "secret-bridge", 10)
    assert titles(store)["Hidden B"].handle in {item["handle"] for item in owner_bridge["items"]}
    store.close()


def test_adding_hidden_node_does_not_change_advisor_observation(tmp_path):
    store, root = ingest_abc(tmp_path)
    principal = advisor()
    before = search(store, principal, "token-a", 10)
    write_pages(
        root,
        {
            "pesquisa/tcc/hidden-d.md": (
                "# Hidden Hub\n\ntoken-a token-a token-a token-c secret-bridge\n"
                "[[Visible A]] [[Visible C]] [[Visible Notes]]\n"
            )
        },
    )
    ingest_manifest(
        store,
        manifest(abc_entries() + [entry("pesquisa/tcc/hidden-d.md", private=True)]),
        root,
    )
    after = search(store, principal, "token-a", 10)
    assert after == before
    _receipt(after, principal)
    store.close()


def test_filter_before_limit_hidden_matches_do_not_occupy_slots(tmp_path):
    extra_pages = {}
    extra_entries = []
    for index in range(8):
        rel = f"pesquisa/tcc/hidden-{index}.md"
        extra_pages[rel] = f"# Hidden {index}\n\nneedle needle needle needle\n"
        extra_entries.append(entry(rel, private=True))
    for index in range(3):
        rel = f"pesquisa/tcc/visible-{index}.md"
        extra_pages[rel] = f"# Visible {index}\n\nneedle\n"
        extra_entries.append(entry(rel))
    store, _ = ingest_abc(tmp_path, extra_entries=extra_entries, extra_pages=extra_pages)
    result = search(store, advisor(), "needle", 2)
    handles = {item["handle"] for item in result["items"]}
    hidden_handles = {node.handle for node in store.nodes("personal") if node.private}
    visible_needles = {node.handle for node in store.nodes("personal") if node.title.startswith("Visible ")}
    assert len(result["items"]) == 2
    assert handles.isdisjoint(hidden_handles)
    assert handles <= visible_needles
    store.close()


def test_hidden_corpus_does_not_change_visible_ranking(tmp_path):
    extra_pages = {
        "pesquisa/tcc/rank-a.md": "# Rank A\n\nrare-term\n",
        "pesquisa/tcc/rank-c.md": "# Rank C\n\nrare-term rare-term\n",
        "pesquisa/tcc/rank-hidden.md": "# Rank Hidden\n\n" + ("rare-term " * 50),
    }
    extra_entries = [
        entry("pesquisa/tcc/rank-a.md"),
        entry("pesquisa/tcc/rank-c.md"),
        entry("pesquisa/tcc/rank-hidden.md", private=True),
    ]
    store, _ = ingest_abc(tmp_path, extra_entries=extra_entries, extra_pages=extra_pages)
    result = search(store, advisor(), "rare-term", 10)
    by_handle = {node.handle: node.title for node in store.nodes("personal")}
    assert [by_handle[item["handle"]] for item in result["items"]] == ["Rank C", "Rank A"]
    store.close()


def test_snippets_drop_hidden_wikilinks_citations_and_backlinks(tmp_path):
    store, _ = ingest_abc(tmp_path)
    result = search(store, advisor(), "token-notes", 10)
    assert [item["handle"] for item in result["items"]] == [titles(store)["Visible Notes"].handle]
    text = result["items"][0]["text"]
    assert "Visible Notes" in text
    assert "Hidden B" not in text
    assert "pesquisa/tcc/b.md" not in text
    assert "token-b" not in text
    assert "[[Hidden B]]" not in text
    for item in search(store, advisor(), "token-a", 10)["items"]:
        assert "Hidden B" not in item["text"]
        assert "/" not in item["handle"]
        assert set(item) == ITEM_KEYS
    store.close()


def test_resolve_reauthorizes_and_treats_denied_like_unknown(tmp_path):
    store, _ = ingest_abc(tmp_path)
    hidden = titles(store)["Hidden B"].handle
    visible = titles(store)["Visible A"].handle
    other = titles(store)["Visible C"].handle
    denied = resolve_handles(store, advisor(), [hidden, "no-such-handle", visible])
    missing = resolve_handles(store, advisor(), ["no-such-handle"])
    assert [item["handle"] for item in denied["items"]] == [visible]
    assert missing["items"] == []
    assert denied["capability_receipt"]["principal_id"] == "advisor-01"
    swapped = resolve_handles(store, advisor(workspace_id="other"), [visible, other])
    assert swapped["items"] == []
    owned = resolve_handles(store, owner(), [hidden])
    assert owned["items"][0]["handle"] == hidden
    store.close()


def test_workspace_isolation(tmp_path):
    store, root = ingest_abc(tmp_path)
    write_pages(root, {"pesquisa/tcc/a.md": "# Visible A\n\ntoken-foreign\n"})
    ingest_manifest(store, manifest(abc_entries(), workspace="other", revision="rev-1"), root)
    personal = search(store, advisor(), "token-foreign", 10)
    foreign = search(store, advisor(workspace_id="other", principal_id="advisor-other"), "token-foreign", 10)
    assert personal["items"] == []
    assert foreign["items"] and "token-foreign" in foreign["items"][0]["text"]
    personal_handle = titles(store, "personal")["Visible A"].handle
    assert resolve_handles(store, advisor(workspace_id="other"), [personal_handle])["items"] == []
    store.close()


def test_owner_role_is_not_a_content_bypass(tmp_path):
    store, _ = ingest_abc(tmp_path)
    restricted_without_scope = owner(scopes=("a2a:message",))
    with pytest.raises(AuthError) as error:
        search(store, restricted_without_scope, "token-b", 10)
    assert error.value.status == 403
    shared_only = owner(classifications=("public", "shared"))
    assert search(store, shared_only, "token-b", 10)["items"] == []
    store.close()
