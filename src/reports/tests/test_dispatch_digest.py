from __future__ import annotations

from pathlib import Path

from swarm_reports.dispatch.digest import (
    DecisionCard,
    independent_cards,
    merge_cards,
    parse_declared_cards,
    top_n_global,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dispatch"
PR_BODY = (FIXTURES / "sample_pr_body.md").read_text()


def _card(kind, location, source="independent") -> DecisionCard:
    return DecisionCard(kind=kind, location=location, question="q", why="w", source=source)


def test_parse_declared_cards_from_pr_body():
    cards = parse_declared_cards(PR_BODY)
    assert len(cards) == 2
    assert cards[0].kind == "hardcode"
    assert cards[0].location == "src/reports/swarm_reports/dispatch/quota.py:24"
    assert cards[0].source == "declared"


def test_parse_declared_cards_missing_section_returns_empty():
    assert parse_declared_cards("# Summary\n\nno decisions section here\n") == []


def test_independent_cards_uses_injected_reviewer_not_a_live_call():
    calls = []

    def fake_reviewer(diff_text: str) -> list[DecisionCard]:
        calls.append(diff_text)
        return [_card("new_dependency", "src/reports/pyproject.toml:5")]

    cards = independent_cards("diff --git a/x b/x", fake_reviewer)
    assert calls == ["diff --git a/x b/x"]
    assert cards[0].source == "independent"


def test_merge_cards_dedups_by_kind_and_location_declared_wins():
    declared = [_card("hardcode", "a.py:1", source="declared")]
    independent = [_card("hardcode", "a.py:1", source="independent"), _card("spec_deviation", "b.py:2")]
    merged = merge_cards(declared, independent)
    assert len(merged) == 2
    hardcode_card = next(c for c in merged if c.kind == "hardcode")
    assert hardcode_card.source == "declared"


def test_merge_cards_caps_at_max_per_pr():
    declared = [_card("hardcode", f"a.py:{i}", source="declared") for i in range(5)]
    merged = merge_cards(declared, [], max_per_pr=3)
    assert len(merged) == 3


def test_top_n_global_is_deterministic_across_prs():
    by_pr = {
        "pr-2": [_card("hardcode", "b.py:1")],
        "pr-1": [_card("auth_boundary", "a.py:1"), _card("contract_change", "a.py:2")],
    }
    top = top_n_global(by_pr, n=5)
    assert [c.kind for c in top] == ["auth_boundary", "contract_change", "hardcode"]
    assert top[0].pr == "pr-1"

    # calling again with the same input produces the same order
    top_again = top_n_global(by_pr, n=5)
    assert [(c.kind, c.pr, c.location) for c in top] == [(c.kind, c.pr, c.location) for c in top_again]


def test_top_n_global_truncates_to_n():
    by_pr = {"pr-1": [_card(k, f"a.py:{i}") for i, k in enumerate(
        ["hardcode", "auth_boundary", "contract_change", "spec_deviation", "heuristic_with_ceiling", "new_dependency"]
    )]}
    assert len(top_n_global(by_pr, n=5)) == 5
