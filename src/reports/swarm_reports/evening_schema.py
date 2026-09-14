"""Canonical EveningPayload — the single body shared by the form POST, the
"copy prompt" clipboard text and the `report evening --input` CLI seam.

Validation rules that the browser, the F3 server and the F4 session all rely on:

- Every type is strict. `"false"`, `0` and `1` are **not** booleans; `"1"` is not
  an integer. Coercion here would silently invert what the owner reported.
- Unknown keys are rejected, at every level, so a typo fails loudly instead of
  being dropped.
- Numbers must be finite; NaN/Infinity are rejected even though `json` accepts them.
- Bounds raise instead of truncating. A silently shortened note is a lost note.
- `notes` may be empty; a blank classification is `None`, which carries no penalty.
- `owner_id` is a consistency field, never an authorization one: the F3 server
  compares it against its own config and never learns identity from the body.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from swarm_reports.metrics.ids import stable_task_id

SCHEMA_VERSION = 1

MAX_NOTE = 4000
MAX_ITEMS = 64
MAX_POSTS = 8
MAX_SIGNALS = 16
MAX_TEXT = 500
MAX_EDIT = 8000
MAX_METRIC = 1e12

#: Owner-facing scope classifications. Only the last two are penalized (F1).
CLASSIFICATIONS = frozenset({"oportunidade", "procrastinacao", "procrastinação", "devaneio"})
POST_PLATFORMS = frozenset({"linkedin", "instagram", "x"})
POST_CHECKPOINTS = frozenset({"launch", "48h", "7d"})
REVIEW_ACTIONS = frozenset({"ready", "reject", "edit"})

#: Fields the transport may add. They never take part in the revision content hash,
#: so a resend that only differs by clock does not create a revision (F3).
TRANSPORT_FIELDS = frozenset({"client_saved_at"})

_ISO_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


@dataclass
class EveningChecklistItem:
    task_id: str
    done: bool
    classification: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "done": self.done,
            "classification": self.classification,
        }


@dataclass
class EveningUnplannedItem:
    """Work done outside the frozen checklist. Never enters the denominator."""

    task_id: str
    text: str
    done: bool
    classification: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "text": self.text,
            "done": self.done,
            "classification": self.classification,
        }


@dataclass
class EveningPostMetrics:
    reach: float | None = None
    outside_fraction: float | None = None
    signals: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "reach": self.reach,
            "outside_fraction": self.outside_fraction,
            "signals": dict(sorted(self.signals.items())),
        }


@dataclass
class EveningPost:
    url: str
    platform: str
    guided: bool
    checkpoint: str
    metrics: EveningPostMetrics = field(default_factory=EveningPostMetrics)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "platform": self.platform,
            "guided": self.guided,
            "checkpoint": self.checkpoint,
            "metrics": self.metrics.to_json(),
        }


@dataclass
class EveningLedgerClassification:
    entry_id: str
    classification: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "classification": self.classification}


@dataclass
class EveningReviewAction:
    draft_id: str
    action: str
    content: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"draft_id": self.draft_id, "action": self.action, "content": self.content}


@dataclass
class EveningPayload:
    schema_version: int
    day: date
    owner_id: str
    checklist: list[EveningChecklistItem] = field(default_factory=list)
    unplanned: list[EveningUnplannedItem] = field(default_factory=list)
    posts: list[EveningPost] = field(default_factory=list)
    ledger_classifications: list[EveningLedgerClassification] = field(default_factory=list)
    review_actions: list[EveningReviewAction] = field(default_factory=list)
    pending_checkpoint_ids: list[str] = field(default_factory=list)
    notes: str = ""
    #: Transport metadata only; excluded from `content_json`.
    client_saved_at: str | None = None

    def content_json(self) -> dict[str, Any]:
        """The part of the payload that decides whether the night must run again."""
        return {
            "schema_version": self.schema_version,
            "day": self.day.isoformat(),
            "owner_id": self.owner_id,
            "checklist": [item.to_json() for item in self.checklist],
            "unplanned": [item.to_json() for item in self.unplanned],
            "posts": [item.to_json() for item in self.posts],
            "ledger_classifications": [i.to_json() for i in self.ledger_classifications],
            "review_actions": [item.to_json() for item in self.review_actions],
            "pending_checkpoint_ids": list(self.pending_checkpoint_ids),
            "notes": self.notes,
        }

    def to_json(self) -> dict[str, Any]:
        payload = self.content_json()
        if self.client_saved_at is not None:
            payload["client_saved_at"] = self.client_saved_at
        return payload

    def to_canonical_json(self) -> str:
        return canonical_json(self.content_json())

    def with_owner(self, owner_id: str) -> EveningPayload:
        """Identity comes from the server config, never from the body."""
        return EveningPayload(
            schema_version=self.schema_version,
            day=self.day,
            owner_id=owner_id,
            checklist=self.checklist,
            unplanned=self.unplanned,
            posts=self.posts,
            ledger_classifications=self.ledger_classifications,
            review_actions=self.review_actions,
            pending_checkpoint_ids=self.pending_checkpoint_ids,
            notes=self.notes,
            client_saved_at=self.client_saved_at,
        )


def canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


_TOP_LEVEL = {
    "schema_version",
    "day",
    "owner_id",
    "checklist",
    "unplanned",
    "posts",
    "ledger_classifications",
    "review_actions",
    "pending_checkpoint_ids",
    "notes",
} | TRANSPORT_FIELDS


def parse_evening_payload(data: Any) -> EveningPayload:
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    _reject_unknown(data, _TOP_LEVEL, "payload")

    version = _strict_int(data.get("schema_version"), "schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version}")

    day_raw = data.get("day")
    if not isinstance(day_raw, str):
        raise ValueError("day must be a YYYY-MM-DD string")
    day = date.fromisoformat(day_raw)

    owner = _required_text(data.get("owner_id"), 64, "owner_id")

    checklist = _parse_checklist(data.get("checklist"))
    unplanned = _parse_unplanned(data.get("unplanned"))
    posts = _parse_posts(data.get("posts"))
    ledger = _parse_ledger(data.get("ledger_classifications"))
    review_actions = _parse_review_actions(data.get("review_actions"))
    pending = _parse_pending(data.get("pending_checkpoint_ids"))

    notes_raw = data.get("notes", "")
    if not isinstance(notes_raw, str):
        raise ValueError("notes must be a string")
    if len(notes_raw) > MAX_NOTE:
        raise ValueError(f"notes exceeds {MAX_NOTE} characters")
    notes = _strip_control(notes_raw, "notes")

    saved_at = data.get("client_saved_at")
    if saved_at is not None:
        if not isinstance(saved_at, str) or not _ISO_INSTANT.match(saved_at):
            raise ValueError("client_saved_at must be an ISO-8601 instant")

    return EveningPayload(
        schema_version=version,
        day=day,
        owner_id=owner,
        checklist=checklist,
        unplanned=unplanned,
        posts=posts,
        ledger_classifications=ledger,
        review_actions=review_actions,
        pending_checkpoint_ids=pending,
        notes=notes,
        client_saved_at=saved_at,
    )


def _parse_checklist(raw: Any) -> list[EveningChecklistItem]:
    entries = _require_list(raw, MAX_ITEMS, "checklist")
    seen: set[str] = set()
    out: list[EveningChecklistItem] = []
    for entry in entries:
        _reject_unknown(entry, {"task_id", "done", "classification"}, "checklist item")
        task_id = _required_text(entry.get("task_id"), 128, "checklist.task_id")
        if task_id in seen:
            raise ValueError(f"duplicate checklist task_id '{task_id}'")
        seen.add(task_id)
        out.append(
            EveningChecklistItem(
                task_id=task_id,
                done=_strict_bool(entry.get("done"), "checklist.done"),
                classification=_parse_classification(entry.get("classification")),
            )
        )
    return out


def _parse_unplanned(raw: Any) -> list[EveningUnplannedItem]:
    entries = _require_list(raw, MAX_ITEMS, "unplanned")
    seen: set[str] = set()
    out: list[EveningUnplannedItem] = []
    for entry in entries:
        _reject_unknown(entry, {"task_id", "text", "done", "classification"}, "unplanned item")
        text = _required_text(entry.get("text"), MAX_TEXT, "unplanned.text")
        task_id_raw = entry.get("task_id")
        if task_id_raw is None:
            task_id = stable_task_id(text, [])
        else:
            task_id = _required_text(task_id_raw, 128, "unplanned.task_id")
        if task_id in seen:
            raise ValueError(f"duplicate unplanned task_id '{task_id}'")
        seen.add(task_id)
        out.append(
            EveningUnplannedItem(
                task_id=task_id,
                text=text,
                done=_strict_bool(entry.get("done"), "unplanned.done"),
                classification=_parse_classification(entry.get("classification")),
            )
        )
    return out


def _parse_posts(raw: Any) -> list[EveningPost]:
    entries = _require_list(raw, MAX_POSTS, "posts")
    out: list[EveningPost] = []
    for entry in entries:
        _reject_unknown(entry, {"url", "platform", "guided", "checkpoint", "metrics"}, "post")
        url = _required_text(entry.get("url"), 2000, "posts.url")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("posts.url must be http(s)")
        platform = _required_text(entry.get("platform"), 32, "posts.platform").lower()
        if platform not in POST_PLATFORMS:
            raise ValueError(f"posts.platform must be one of {sorted(POST_PLATFORMS)}")
        checkpoint = _required_text(entry.get("checkpoint"), 16, "posts.checkpoint").lower()
        if checkpoint not in POST_CHECKPOINTS:
            raise ValueError(f"posts.checkpoint must be one of {sorted(POST_CHECKPOINTS)}")
        out.append(
            EveningPost(
                url=url,
                platform=platform,
                guided=_strict_bool(entry.get("guided"), "posts.guided"),
                checkpoint=checkpoint,
                metrics=_parse_post_metrics(entry.get("metrics")),
            )
        )
    return out


def _parse_post_metrics(raw: Any) -> EveningPostMetrics:
    if raw is None:
        return EveningPostMetrics()
    if not isinstance(raw, dict):
        raise ValueError("posts.metrics must be an object")
    _reject_unknown(raw, {"reach", "outside_fraction", "signals"}, "posts.metrics")
    reach = _optional_metric(raw.get("reach"), "posts.metrics.reach")
    outside = _optional_metric(raw.get("outside_fraction"), "posts.metrics.outside_fraction")
    if outside is not None and not (0.0 <= outside <= 1.0):
        raise ValueError("posts.metrics.outside_fraction must be between 0 and 1")
    signals_raw = raw.get("signals")
    signals: dict[str, float] = {}
    if signals_raw is not None:
        if not isinstance(signals_raw, dict):
            raise ValueError("posts.metrics.signals must be an object")
        if len(signals_raw) > MAX_SIGNALS:
            raise ValueError(f"posts.metrics.signals exceeds {MAX_SIGNALS} keys")
        for key, value in signals_raw.items():
            name = _required_text(key, 32, "posts.metrics.signals key")
            if value is None:
                continue
            signals[name] = _require_metric(value, f"posts.metrics.signals.{name}")
    return EveningPostMetrics(reach=reach, outside_fraction=outside, signals=signals)


def _parse_ledger(raw: Any) -> list[EveningLedgerClassification]:
    entries = _require_list(raw, MAX_ITEMS, "ledger_classifications")
    seen: set[str] = set()
    out: list[EveningLedgerClassification] = []
    for entry in entries:
        _reject_unknown(entry, {"entry_id", "classification"}, "ledger_classifications item")
        entry_id = _required_text(entry.get("entry_id"), 128, "ledger_classifications.entry_id")
        if entry_id in seen:
            raise ValueError(f"duplicate ledger entry_id '{entry_id}'")
        seen.add(entry_id)
        out.append(
            EveningLedgerClassification(
                entry_id=entry_id,
                classification=_parse_classification(entry.get("classification")),
            )
        )
    return out


def _parse_review_actions(raw: Any) -> list[EveningReviewAction]:
    entries = _require_list(raw, MAX_ITEMS, "review_actions")
    seen: set[str] = set()
    out: list[EveningReviewAction] = []
    for entry in entries:
        _reject_unknown(entry, {"draft_id", "action", "content"}, "review_actions item")
        draft_id = _required_text(entry.get("draft_id"), 64, "review_actions.draft_id")
        if draft_id in seen:
            raise ValueError(f"duplicate review action draft_id '{draft_id}'")
        seen.add(draft_id)
        action = _required_text(entry.get("action"), 16, "review_actions.action").lower()
        if action not in REVIEW_ACTIONS:
            raise ValueError(f"review_actions.action must be one of {sorted(REVIEW_ACTIONS)}")
        content_raw = entry.get("content")
        content: str | None = None
        if content_raw is not None:
            if not isinstance(content_raw, str):
                raise ValueError("review_actions.content must be a string")
            if len(content_raw) > MAX_EDIT:
                raise ValueError(f"review_actions.content exceeds {MAX_EDIT} characters")
            content = _strip_control(content_raw, "review_actions.content")
        if action == "edit" and not content:
            raise ValueError("review_actions.content is required when action is 'edit'")
        out.append(EveningReviewAction(draft_id=draft_id, action=action, content=content))
    return out


def _parse_pending(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("pending_checkpoint_ids must be a list")
    if len(raw) > MAX_ITEMS:
        raise ValueError(f"pending_checkpoint_ids exceeds {MAX_ITEMS} entries")
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        text = _required_text(value, 128, "pending_checkpoint_ids entry")
        if text in seen:
            raise ValueError(f"duplicate pending_checkpoint_id '{text}'")
        seen.add(text)
        out.append(text)
    return out


def _parse_classification(value: Any) -> str | None:
    """Blank or absent means "not classified", which never penalizes (design)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("classification must be a string or null")
    text = value.strip().lower()
    if not text:
        return None
    if text not in CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {sorted(CLASSIFICATIONS)} or null")
    return text


def _require_list(raw: Any, limit: int, label: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds {limit} entries")
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{label} entries must be objects")
    return raw


def _reject_unknown(data: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise ValueError(f"{label} has unknown keys: {', '.join(extra)}")


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a JSON boolean")
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a JSON integer")
    return value


def _required_text(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be blank")
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return _strip_control(text, label)


def _strip_control(text: str, label: str) -> str:
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError(f"{label} contains control characters")
    return text


def _require_metric(value: Any, label: str) -> float:
    number = _optional_metric(value, label)
    if number is None:
        raise ValueError(f"{label} must be a number")
    return number


def _optional_metric(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number < 0:
        raise ValueError(f"{label} must not be negative")
    if number > MAX_METRIC:
        raise ValueError(f"{label} exceeds the plausible maximum")
    return number


def evening_payload_json_schema() -> dict[str, Any]:
    """Complete JSON Schema for the payload, embedded in the HTML for the owner."""
    classification = {
        "anyOf": [{"type": "null"}, {"enum": sorted(CLASSIFICATIONS)}],
        "description": "null (or omitted) means unclassified, which carries no penalty",
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mathai.com.br/schemas/evening-payload.v1.json",
        "title": "EveningPayload",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "day", "owner_id", "checklist"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "day": {"type": "string", "format": "date"},
            "owner_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "consistency check only; the server takes identity from its config",
            },
            "checklist": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "done"],
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "done": {"type": "boolean"},
                        "classification": classification,
                    },
                },
            },
            "unplanned": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "done"],
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "maxLength": 128,
                            "description": "optional; derived from a stable hash of text when absent",
                        },
                        "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
                        "done": {"type": "boolean"},
                        "classification": classification,
                    },
                },
            },
            "posts": {
                "type": "array",
                "maxItems": MAX_POSTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "platform", "guided", "checkpoint"],
                    "properties": {
                        "url": {"type": "string", "pattern": "^https?://", "maxLength": 2000},
                        "platform": {"enum": sorted(POST_PLATFORMS)},
                        "guided": {"type": "boolean"},
                        "checkpoint": {"enum": sorted(POST_CHECKPOINTS)},
                        "metrics": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "reach": {"type": ["number", "null"], "minimum": 0},
                                "outside_fraction": {
                                    "type": ["number", "null"],
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "signals": {
                                    "type": "object",
                                    "maxProperties": MAX_SIGNALS,
                                    "additionalProperties": {
                                        "type": ["number", "null"],
                                        "minimum": 0,
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "ledger_classifications": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["entry_id"],
                    "properties": {
                        "entry_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "classification": classification,
                    },
                },
            },
            "review_actions": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["draft_id", "action"],
                    "properties": {
                        "draft_id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "action": {"enum": sorted(REVIEW_ACTIONS)},
                        "content": {
                            "type": ["string", "null"],
                            "maxLength": MAX_EDIT,
                            "description": "required when action is 'edit'; approving never publishes",
                        },
                    },
                },
            },
            "pending_checkpoint_ids": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "notes": {
                "type": "string",
                "maxLength": MAX_NOTE,
                "description": "empty string is valid",
            },
            "client_saved_at": {
                "type": ["string", "null"],
                "description": "transport only; excluded from the revision content hash",
            },
        },
    }
