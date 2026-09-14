"""Decision digest: max 3 cards per PR, top 5 global, two independent sources.

Source 1 is the implementer's declared section in the PR body. Source 2 is an
independent reviewer over the diff, called through an injected
``DiffReviewer`` so no test spawns a live provider. The gap between the two
lists is itself signal (see the design note's "Digest de decisões").

Both sources are **untrusted text**. The declared section is written by the
agent that wants its PR merged; the independent reviewer is a model reading a
diff. So every card is validated against a schema before it becomes a
``DecisionCard``, and a card's ``file:line`` is only turned into a clickable
URL after the line has been verified to exist in the diff — a card pointing at
a line that was never touched sends the owner to read the wrong code, which is
worse than no card at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal, Mapping, Protocol

Kind = Literal[
    "hardcode",
    "auth_boundary",
    "new_dependency",
    "contract_change",
    "heuristic_with_ceiling",
    "spec_deviation",
]
Source = Literal["declared", "independent"]

_KIND_PRIORITY: dict[str, int] = {
    "auth_boundary": 0,
    "contract_change": 1,
    "spec_deviation": 2,
    "hardcode": 3,
    "heuristic_with_ceiling": 4,
    "new_dependency": 5,
}

DECLARED_SECTION_HEADING = "Decisões que merecem pergunta"
MAX_CARDS_PER_PR = 3
MAX_TOP_N = 5
MAX_TEXT_LEN = 300

_LOCATION_RE = re.compile(r"^(?P<file>[A-Za-z0-9._][A-Za-z0-9._\-/]*):(?P<line>\d{1,7})$")

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


class InvalidCard(ValueError):
    """A proposed card failed schema validation and was dropped."""


@dataclass(frozen=True)
class DecisionCard:
    kind: Kind
    location: str  # "arquivo:linha"
    question: str
    why: str
    source: Source
    pr: str = ""  # repo#number or similar external id, filled by the caller
    verified_line: bool = False  # True only when the line was found in the diff


class DiffReviewer(Protocol):
    def __call__(self, diff_text: str) -> Iterable[Mapping[str, Any]]:
        """Return raw JSON-ish card dicts. Untrusted; validated by this module."""


def _clean_text(value: Any, *, field: str, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidCard(f"{field} must be a string, got {type(value).__name__}")
    text = " ".join(value.split())  # collapses newlines/control whitespace
    if any(ord(ch) < 0x20 for ch in text):
        raise InvalidCard(f"{field} contains control characters")
    if required and not text:
        raise InvalidCard(f"{field} is required")
    return text[:MAX_TEXT_LEN]


def validate_card(raw: Mapping[str, Any], *, source: Source, pr: str = "") -> DecisionCard:
    """Turn one untrusted dict into a DecisionCard, or raise InvalidCard."""
    if not isinstance(raw, Mapping):
        raise InvalidCard("card must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind.lower() not in _KIND_PRIORITY:
        raise InvalidCard(f"unknown card kind {kind!r}")
    location = raw.get("location")
    if not isinstance(location, str) or not _LOCATION_RE.match(location):
        raise InvalidCard(f"location must look like file.py:123, got {location!r}")
    if ".." in location:
        raise InvalidCard(f"location escapes the repo: {location!r}")
    return DecisionCard(
        kind=kind.lower(),  # type: ignore[arg-type]
        location=location,
        question=_clean_text(raw.get("question"), field="question", required=True),
        why=_clean_text(raw.get("why"), field="why", required=False),
        source=source,
        pr=pr,
    )


def parse_declared_cards(pr_body: str, *, pr: str = "") -> list[DecisionCard]:
    """Parse the mandatory "Decisões que merecem pergunta" section of a PR body."""
    if not isinstance(pr_body, str) or DECLARED_SECTION_HEADING not in pr_body:
        return []
    _, _, tail = pr_body.partition(DECLARED_SECTION_HEADING)
    section = tail.split("\n#", 1)[0]  # stop at the next heading
    cards: list[DecisionCard] = []
    for line in section.splitlines():
        match = _ITEM_RE.match(line)
        if not match:
            continue
        try:
            cards.append(
                validate_card(
                    {
                        "kind": match.group("kind"),
                        "location": match.group("location"),
                        "question": match.group("question"),
                        "why": match.group("why"),
                    },
                    source="declared",
                    pr=pr,
                )
            )
        except InvalidCard:
            continue  # a malformed declared line is dropped, never guessed at
    return cards


def independent_cards(diff_text: str, reviewer: DiffReviewer, *, pr: str = "") -> list[DecisionCard]:
    """Call the injected reviewer and keep only the cards that validate."""
    raw_cards = reviewer(diff_text)
    if raw_cards is None:
        return []
    if isinstance(raw_cards, (str, bytes, Mapping)):
        raise InvalidCard("reviewer must return a list of card objects")
    cards: list[DecisionCard] = []
    for raw in list(raw_cards)[: MAX_CARDS_PER_PR * 4]:  # a runaway reviewer cannot flood the digest
        try:
            cards.append(validate_card(raw, source="independent", pr=pr))
        except InvalidCard:
            continue
    return cards


def diff_line_index(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Map each file in a unified diff to the post-image line ranges it touches."""
    index: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith(("a/", "b/")) else target
                index.setdefault(current, [])
        elif line.startswith("@@") and current is not None:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                if count > 0:
                    index[current].append((start, start + count - 1))
    return index


def verify_locations(cards: list[DecisionCard], diff_text: str) -> list[DecisionCard]:
    """Mark each card whose ``file:line`` really falls inside the diff's hunks."""
    index = diff_line_index(diff_text)
    verified: list[DecisionCard] = []
    for card in cards:
        match = _LOCATION_RE.match(card.location)
        ok = False
        if match:
            ranges = index.get(match.group("file"), [])
            line = int(match.group("line"))
            ok = any(start <= line <= end for start, end in ranges)
        verified.append(replace(card, verified_line=ok))
    return verified


def card_url(card: DecisionCard, *, repo: str, pr_number: int) -> str | None:
    """Deep link for a card, only when its line was verified against the diff."""
    if not card.verified_line:
        return None
    match = _LOCATION_RE.match(card.location)
    if not match:
        return None
    return (
        f"https://github.com/{repo}/pull/{pr_number}/files"
        f"#diff-{match.group('file')}R{match.group('line')}"
    )


def _dedup_key(card: DecisionCard) -> tuple[str, str]:
    return (card.kind, card.location)


def merge_cards(
    declared: list[DecisionCard], independent: list[DecisionCard], *, max_per_pr: int = MAX_CARDS_PER_PR
) -> list[DecisionCard]:
    """Union declared+independent, dedup by (kind, location), declared wins ties, cap at max_per_pr."""
    if max_per_pr < 1:
        raise ValueError("max_per_pr must be >= 1")
    max_per_pr = min(max_per_pr, MAX_CARDS_PER_PR)
    seen: dict[tuple[str, str], DecisionCard] = {}
    for card in list(declared) + list(independent):
        key = _dedup_key(card)
        if key not in seen:
            seen[key] = card
    ordered = sorted(seen.values(), key=lambda c: (_KIND_PRIORITY[c.kind], c.location))
    return ordered[:max_per_pr]


def top_n_global(cards_by_pr: dict[str, list[DecisionCard]], *, n: int = MAX_TOP_N) -> list[DecisionCard]:
    """Deterministic top-N across all PRs' cards for the day's digest."""
    n = min(n, MAX_TOP_N)
    flat: list[DecisionCard] = []
    for pr, cards in cards_by_pr.items():
        for card in cards[:MAX_CARDS_PER_PR]:
            flat.append(card if card.pr else replace(card, pr=pr))
    ordered = sorted(flat, key=lambda c: (_KIND_PRIORITY[c.kind], c.pr, c.location))
    return ordered[:n]


__all__ = [
    "DECLARED_SECTION_HEADING",
    "DecisionCard",
    "DiffReviewer",
    "InvalidCard",
    "card_url",
    "diff_line_index",
    "independent_cards",
    "merge_cards",
    "parse_declared_cards",
    "top_n_global",
    "validate_card",
    "verify_locations",
]
