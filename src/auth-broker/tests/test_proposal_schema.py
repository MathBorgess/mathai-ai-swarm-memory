from datetime import datetime, timezone

import pytest

from app.proposals.schema import (
    ProposalPayloadError,
    ProposalTooLarge,
    assert_write_path,
    derive_branch,
    derive_path,
    parse_object,
    parse_proposal,
    render_note,
    yaml_string,
)

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
VALID = {
    "namespace": "pesquisa.tcc",
    "title": "Nota de leitura",
    "body_markdown": "Afirmação não verificada.",
    "sources": [{"url": "https://example.test/paper", "label": "paper"}],
}


def test_parse_accepts_contract_object():
    payload = parse_proposal(VALID)
    assert payload.namespace == "pesquisa.tcc"
    assert payload.propose_scope() == "ctx:propose:pesquisa.tcc"
    assert payload.payload_hash() == parse_proposal(dict(VALID)).payload_hash()


@pytest.mark.parametrize("change", [
    {"path": "wiki/secret.md"},
    {"branch": "main"},
    {"repo": "MathBorgess/mathai-wiki"},
    {"principal_id": "attacker"},
    {"shell": "rm -rf /"},
])
def test_extra_fields_are_rejected(change):
    with pytest.raises(ProposalPayloadError):
        parse_proposal({**VALID, **change})


@pytest.mark.parametrize("namespace", ["wiki", "fontes", "pesquisa.tcc/../../wiki", "pesquisa"])
def test_unknown_namespace_rejected(namespace):
    with pytest.raises(ProposalPayloadError):
        parse_proposal({**VALID, "namespace": namespace})


def test_duplicate_json_keys_rejected():
    with pytest.raises(ProposalPayloadError):
        parse_object(b'{"namespace":"pesquisa.tcc","namespace":"wiki","title":"a","body_markdown":"b","sources":[]}')


def test_markdown_over_12kib_is_too_large():
    with pytest.raises(ProposalTooLarge):
        parse_proposal({**VALID, "body_markdown": "x" * 12289})


def test_invalid_source_url_and_javascript_scheme():
    with pytest.raises(ProposalPayloadError):
        parse_proposal({**VALID, "sources": [{"url": "javascript:alert(1)", "label": "x"}]})
    with pytest.raises(ProposalPayloadError):
        parse_proposal({**VALID, "sources": [{"url": "https://example.test/a", "label": "x", "path": "../wiki"}]})


def test_derived_path_stays_in_inbox():
    path = derive_path("pesquisa.tcc", "abc_DEF-123456", NOW)
    assert path == "pesquisa/tcc/inbox/2026-09-11-abc_DEF-123456.md"
    assert derive_branch("abc_DEF-123456") == "swarm/proposal-abc_DEF-123456"


@pytest.mark.parametrize("path", [
    "wiki/index.md",
    "fontes/raw.md",
    "pesquisa/tcc/inbox/../wiki/x.md",
    "/pesquisa/tcc/inbox/x.md",
    "pesquisa/tcc/inbox/x.md/../../AGENTS.md",
    "pesquisa/tcc/inbox/x",
    "AGENTS.md",
    ".github/workflows/x.yml",
])
def test_blocked_paths(path):
    with pytest.raises(ProposalPayloadError):
        assert_write_path(path)


def test_frontmatter_injection_stays_quoted():
    payload = parse_proposal({
        **VALID,
        "title": 'x", layer: T0, promotes_to_wiki: true',
        "body_markdown": "---\nlayer: T0\n---\n# hijack",
    })
    note = render_note(
        proposal_id="id_safe_01",
        principal_id='adv", layer: T0',
        workspace_id="personal",
        created_at=NOW,
        payload=payload,
    )
    yaml_block = note.split("---", 2)[1]
    assert "layer: T2" in yaml_block
    assert "promotes_to_wiki: false" in yaml_block
    assert "status: human_confirmation_pending" in yaml_block
    assert "\nlayer: T0\n" not in yaml_block
    assert yaml_string(payload.title) in yaml_block
    assert "não promove conteúdo para wiki T1/T0" in note
    assert "javascript:" not in note
    assert "https://example.test/paper" in note
