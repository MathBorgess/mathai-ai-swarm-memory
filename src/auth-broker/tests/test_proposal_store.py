from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.proposals.store import IdempotencyConflict, ProposalStore

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def claim(store, **overrides):
    values = dict(
        workspace_id="personal",
        principal_id="advisor-01",
        idempotency_key="k1",
        payload_hash="a" * 64,
        proposal_id="prop_aaaaaaaa",
        namespace="pesquisa.tcc",
        title="t",
        note="n",
        now=NOW,
    )
    values.update(overrides)
    return store.claim(**values)


def test_retry_same_key_returns_same_row(tmp_path):
    path = tmp_path / "proposals.sqlite3"
    store = ProposalStore(path)
    first = claim(store)
    second = claim(store, proposal_id="prop_bbbbbbbb")
    assert first.proposal_id == second.proposal_id == "prop_aaaaaaaa"
    assert first.path.startswith("pesquisa/tcc/inbox/")
    assert "wiki/" not in first.path and "fontes/" not in first.path
    store.close()
    reopened = ProposalStore(path)
    third = claim(reopened, proposal_id="prop_cccccccc")
    assert third.proposal_id == first.proposal_id
    assert third.status == "pending"
    reopened.close()


def test_same_key_different_hash_conflicts(tmp_path):
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    claim(store)
    with pytest.raises(IdempotencyConflict):
        claim(store, payload_hash="b" * 64)
    store.close()


def test_workspace_and_principal_are_isolated(tmp_path):
    store = ProposalStore(tmp_path / "proposals.sqlite3")
    a = claim(store)
    b = claim(store, workspace_id="other", proposal_id="prop_bbbbbbbb")
    c = claim(store, principal_id="advisor-02", proposal_id="prop_cccccccc")
    assert len({a.proposal_id, b.proposal_id, c.proposal_id}) == 3
    store.close()


def test_advance_is_monotonic_and_survives_reopen(tmp_path):
    path = tmp_path / "proposals.sqlite3"
    store = ProposalStore(path)
    row = claim(store)
    store.mark_branch(row.proposal_id, "sha-b", NOW)
    store.mark_commit(row.proposal_id, "sha-c", NOW)
    done = store.mark_pr(row.proposal_id, 7, "https://github.com/acme/discard/pull/7", NOW)
    store.mark_branch(row.proposal_id, "ignored", NOW)
    assert done.status == "pr_opened"
    assert done.pr_url.endswith("/pull/7")
    store.close()
    reopened = ProposalStore(path)
    loaded = reopened.get(row.proposal_id)
    assert loaded.pr_number == 7 and loaded.commit_sha == "sha-c"
    reopened.close()


def test_concurrent_claims_share_one_row(tmp_path):
    path = tmp_path / "proposals.sqlite3"

    def once(i):
        store = ProposalStore(path)
        try:
            return claim(store, proposal_id=f"prop_{i:08d}aaaa").proposal_id
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(once, range(8)))
    assert len(set(ids)) == 1
