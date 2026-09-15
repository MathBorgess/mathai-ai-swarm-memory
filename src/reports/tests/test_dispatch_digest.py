from __future__ import annotations

from pathlib import Path

import pytest
from swarm_reports.dispatch.digest import (
    DecisionCard,
    InvalidCard,
    card_url,
    diff_line_index,
    independent_cards,
    merge_cards,
    parse_declared_cards,
    top_n_global,
    validate_card,
    verify_locations,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dispatch"
PR_BODY = (FIXTURES / "sample_pr_body.md").read_text()

DIFF = """diff --git a/src/reports/swarm_reports/dispatch/quota.py b/src/reports/swarm_reports/dispatch/quota.py
--- a/src/reports/swarm_reports/dispatch/quota.py
+++ b/src/reports/swarm_reports/dispatch/quota.py
@@ -20,6 +20,9 @@ class QuotaWindow:
+new line 20
+new line 21
+new line 22
diff --git a/README.md b/README.md
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+one
+two
"""


def _card(kind, location, source="independent", **kw) -> DecisionCard:
    return DecisionCard(kind=kind, location=location, question="q", why="w", source=source, **kw)


# --- declared source ---------------------------------------------------------


def test_parse_declared_cards_from_pr_body():
    cards = parse_declared_cards(PR_BODY)
    assert len(cards) == 2
    assert cards[0].kind == "hardcode"
    assert cards[0].location == "src/reports/swarm_reports/dispatch/quota.py:24"
    assert cards[0].source == "declared"


def test_parse_declared_cards_missing_section_returns_empty():
    assert parse_declared_cards("# Summary\n\nno decisions section here\n") == []


def test_declared_line_with_an_invented_kind_is_dropped():
    body = "Decisões que merecem pergunta\n- totally_new_kind, a.py:1, pergunta, porque\n"
    assert parse_declared_cards(body) == []


def test_declared_line_with_a_traversal_location_is_dropped():
    body = "Decisões que merecem pergunta\n- hardcode, ../../etc/passwd:1, pergunta, porque\n"
    assert parse_declared_cards(body) == []


# --- finding 5c: the independent reviewer's output is untrusted -------------


def test_independent_reviewer_output_is_schema_validated():
    def rogue_reviewer(diff_text: str):
        return [
            {"kind": "hardcode", "location": "a.py:10", "question": "why hardcoded?", "why": "breaks on rename"},
            {"kind": "not_a_kind", "location": "a.py:11", "question": "q"},
            {"kind": "hardcode", "location": "not a location", "question": "q"},
            {"kind": "hardcode", "location": "a.py:12"},  # no question
            {"kind": "hardcode", "location": "/etc/passwd:1", "question": "q"},
            "a bare string, not a card",
        ]

    cards = independent_cards("diff", rogue_reviewer)
    assert [c.location for c in cards] == ["a.py:10"]
    assert cards[0].source == "independent"


def test_reviewer_cannot_flood_the_digest():
    def flooding_reviewer(diff_text: str):
        return [{"kind": "hardcode", "location": f"a.py:{i}", "question": "q"} for i in range(500)]

    cards = independent_cards("diff", flooding_reviewer)
    assert len(cards) <= 12
    assert len(merge_cards([], cards)) == 3


def test_reviewer_text_is_bounded_and_stripped_of_control_characters():
    card = validate_card(
        {"kind": "hardcode", "location": "a.py:1", "question": "x" * 5000, "why": "line\nbreak\tinjected"},
        source="independent",
    )
    assert len(card.question) == 300
    assert card.why == "line break injected"


def test_reviewer_returning_a_non_list_is_an_error_not_a_silent_empty():
    with pytest.raises(InvalidCard):
        independent_cards("diff", lambda diff_text: {"kind": "hardcode"})


def test_reviewer_returning_none_yields_no_cards():
    assert independent_cards("diff", lambda diff_text: None) == []


def test_a_card_that_is_not_an_object_is_rejected():
    with pytest.raises(InvalidCard):
        validate_card(["kind", "hardcode"], source="independent")


# --- finding 5d: a location is only linkable once verified against the diff --


def test_diff_line_index_maps_files_to_post_image_ranges():
    index = diff_line_index(DIFF)
    assert index["src/reports/swarm_reports/dispatch/quota.py"] == [(20, 28)]
    assert index["README.md"] == [(1, 2)]


def test_only_lines_inside_the_diff_are_marked_verified():
    cards = [
        _card("hardcode", "src/reports/swarm_reports/dispatch/quota.py:24"),
        _card("hardcode", "src/reports/swarm_reports/dispatch/quota.py:900"),
        _card("hardcode", "never/touched.py:1"),
    ]
    verified = verify_locations(cards, DIFF)
    assert [c.verified_line for c in verified] == [True, False, False]


def test_an_unverified_card_gets_no_url():
    card = _card("hardcode", "a.py:1")
    assert card_url(card, repo="o/r", pr_number=7) is None


def test_a_verified_card_links_to_the_pr_file_and_line():
    card = verify_locations([_card("hardcode", "README.md:2")], DIFF)[0]
    url = card_url(card, repo="o/r", pr_number=7)
    assert url.startswith("https://github.com/o/r/pull/7/files")
    assert url.endswith("R2")


def test_verification_does_not_invent_cards_when_the_diff_is_empty():
    assert verify_locations([_card("hardcode", "a.py:1")], "")[0].verified_line is False


# --- merge and top-N ---------------------------------------------------------


def test_merge_cards_dedups_by_kind_and_location_declared_wins():
    declared = [_card("hardcode", "a.py:1", source="declared")]
    independent = [_card("hardcode", "a.py:1"), _card("spec_deviation", "b.py:2")]
    merged = merge_cards(declared, independent)
    assert len(merged) == 2
    assert next(c for c in merged if c.kind == "hardcode").source == "declared"


def test_merge_cards_caps_at_three_even_if_asked_for_more():
    declared = [_card("hardcode", f"a.py:{i}", source="declared") for i in range(9)]
    assert len(merge_cards(declared, [], max_per_pr=99)) == 3


def test_top_n_global_is_deterministic_across_prs():
    by_pr = {
        "pr-2": [_card("hardcode", "b.py:1")],
        "pr-1": [_card("auth_boundary", "a.py:1"), _card("contract_change", "a.py:2")],
    }
    top = top_n_global(by_pr, n=5)
    assert [c.kind for c in top] == ["auth_boundary", "contract_change", "hardcode"]
    assert top[0].pr == "pr-1"
    assert [(c.kind, c.pr, c.location) for c in top] == [
        (c.kind, c.pr, c.location) for c in top_n_global(by_pr, n=5)
    ]


def test_top_n_global_caps_at_five_even_if_asked_for_more():
    by_pr = {
        f"pr-{i}": [_card(k, f"a.py:{i}") for k in ("hardcode", "auth_boundary", "contract_change")]
        for i in range(9)
    }
    assert len(top_n_global(by_pr, n=99)) == 5


def test_top_n_global_never_takes_more_than_three_cards_from_one_pr():
    by_pr = {"pr-1": [_card("auth_boundary", f"a.py:{i}") for i in range(10)]}
    assert len(top_n_global(by_pr, n=5)) == 3
