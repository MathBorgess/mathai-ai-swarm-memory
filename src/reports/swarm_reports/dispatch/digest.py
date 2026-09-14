"""Decision digest: max 3 cards per PR, top 5 global, two independent sources.

Source 1 is the implementer's declared section in the PR body. Source 2 is
an independent reviewer over the diff, called through an injected
``DiffReviewer`` so no test spawns a live provider. The gap between the two
lists is itself signal (see the design note's "Digest de decisões").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

Kind = Literal[
    "hardcode",
    "auth_boundary",
    "new_dependency",
    "contract_change",
    "heuristic_with_ceiling",
    "spec_deviation",
]
Source = Literal["declared", "independent"]

_KIND_PRIORITY: dict[Kind, int] = {
    "auth_boundary": 0,
    "contract_change": 1,
    "spec_deviation": 2,
    "hardcode": 3,
    "heuristic_with_ceiling": 4,
    "new_dependency": 5,
}

DECLARED_SECTION_HEADING = "Decisões que merecem pergunta"

_ITEM_RE = re.compile(
    r"""
    ^\s*[-*]\s*
    (?:tipo\s*:\s*)?(?P<kind>[a-z_]+)\s*
    [,\-–]\s*
    (?P<location>[^\s,]+:\d+)\s*
    [,\-–]\s*
    (?:pergunta\s*:\s*)?(?P<question>.+?)
    (?:\s*[,\-–]\s*(?:por que importa\s*:\s*)?(?P<why>.+))?$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class DecisionCard:
    kind: Kind
    location: str  # "arquivo:linha"
    question: str
    why: str
    source: Source
    pr: str = ""  # repo#number or similar external id, filled by the caller


class DiffReviewer(Protocol):
    def __call__(self, diff_text: str) -> list[DecisionCard]: ...


def parse_declared_cards(pr_body: str) -> list[DecisionCard]:
    """Parse the mandatory "Decisões que merecem pergunta" section of a PR body."""
    if DECLARED_SECTION_HEADING not in pr_body:
        return []
    _, _, tail = pr_body.partition(DECLARED_SECTION_HEADING)
    section = tail.split("\n#", 1)[0]  # stop at the next heading
    cards: list[DecisionCard] = []
    for line in section.splitlines():
        match = _ITEM_RE.match(line)
        if not match:
            continue
        kind = match.group("kind").lower()
        if kind not in _KIND_PRIORITY:
            continue
        cards.append(
            DecisionCard(
                kind=kind,  # type: ignore[arg-type]
                location=match.group("location"),
                question=(match.group("question") or "").strip(),
                why=(match.group("why") or "").strip(),
                source="declared",
            )
        )
    return cards


def independent_cards(diff_text: str, reviewer: DiffReviewer) -> list[DecisionCard]:
    return [
        DecisionCard(kind=c.kind, location=c.location, question=c.question, why=c.why, source="independent")
        for c in reviewer(diff_text)
    ]


def _dedup_key(card: DecisionCard) -> tuple[str, str]:
    return (card.kind, card.location)


def merge_cards(
    declared: list[DecisionCard], independent: list[DecisionCard], *, max_per_pr: int = 3
) -> list[DecisionCard]:
    """Union declared+independent, dedup by (kind, location), declared wins ties, cap at max_per_pr."""
    seen: dict[tuple[str, str], DecisionCard] = {}
    for card in declared + independent:
        key = _dedup_key(card)
        if key not in seen:
            seen[key] = card
    ordered = sorted(seen.values(), key=lambda c: (_KIND_PRIORITY[c.kind], c.location))
    return ordered[:max_per_pr]


def top_n_global(cards_by_pr: dict[str, list[DecisionCard]], *, n: int = 5) -> list[DecisionCard]:
    """Deterministic top-N across all PRs' cards for the day's digest."""
    flat: list[DecisionCard] = []
    for pr, cards in cards_by_pr.items():
        for card in cards:
            flat.append(card if card.pr else DecisionCard(**{**card.__dict__, "pr": pr}))
    ordered = sorted(flat, key=lambda c: (_KIND_PRIORITY[c.kind], c.pr, c.location))
    return ordered[:n]
