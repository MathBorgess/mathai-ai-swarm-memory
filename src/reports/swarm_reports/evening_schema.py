"""Public EveningPayload JSON schema for F3/F4 (validated, bounded)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

SCHEMA_VERSION = 1
MAX_NOTE = 4000
MAX_ITEMS = 64


@dataclass
class EveningChecklistItem:
    task_id: str
    done: bool
    classification: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"task_id": self.task_id, "done": self.done}
        if self.classification:
            payload["classification"] = self.classification
        return payload


@dataclass
class EveningReviewAction:
    draft_id: str
    action: str

    def to_json(self) -> dict[str, str]:
        return {"draft_id": self.draft_id, "action": self.action}


@dataclass
class EveningPayload:
    """Canonical evening form + copy-prompt payload (F3 POST body)."""

    schema_version: int
    day: date
    owner_id: str
    checklist: list[EveningChecklistItem]
    review_actions: list[EveningReviewAction] = field(default_factory=list)
    unplanned_classifications: dict[str, str] = field(default_factory=dict)
    post_metrics: dict[str, float | int | None] = field(default_factory=dict)
    notes: str = ""
    lesson_completed: bool | None = None
    pending_checkpoint_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "day": self.day.isoformat(),
            "owner_id": self.owner_id,
            "checklist": [item.to_json() for item in self.checklist],
            "review_actions": [item.to_json() for item in self.review_actions],
            "unplanned_classifications": self.unplanned_classifications,
            "post_metrics": self.post_metrics,
            "notes": self.notes,
            "lesson_completed": self.lesson_completed,
            "pending_checkpoint_ids": self.pending_checkpoint_ids,
        }

    def to_canonical_json(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_evening_payload(data: dict[str, Any]) -> EveningPayload:
    version = int(data.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version}")
    day = date.fromisoformat(str(data["day"]))
    owner = _bounded(str(data.get("owner_id") or ""), 64)
    checklist_raw = data.get("checklist") or []
    if not isinstance(checklist_raw, list) or len(checklist_raw) > MAX_ITEMS:
        raise ValueError("checklist invalid")
    checklist = []
    for entry in checklist_raw:
        if not isinstance(entry, dict):
            raise ValueError("checklist item must be object")
        task_id = _bounded(str(entry.get("task_id") or ""), 128)
        done = bool(entry.get("done"))
        classification = entry.get("classification")
        if classification is not None:
            classification = _bounded(str(classification), 32)
        checklist.append(
            EveningChecklistItem(task_id=task_id, done=done, classification=classification)
        )
    actions_raw = data.get("review_actions") or []
    review_actions = []
    for entry in actions_raw:
        if not isinstance(entry, dict):
            raise ValueError("review_actions item must be object")
        draft_id = _bounded(str(entry.get("draft_id") or ""), 64)
        action = _bounded(str(entry.get("action") or ""), 16).lower()
        if action not in {"ready", "reject", "edit"}:
            raise ValueError("invalid review action")
        review_actions.append(EveningReviewAction(draft_id=draft_id, action=action))
    unplanned = data.get("unplanned_classifications") or {}
    if not isinstance(unplanned, dict):
        raise ValueError("unplanned_classifications must be object")
    unplanned_out: dict[str, str] = {}
    for key, value in unplanned.items():
        unplanned_out[_bounded(str(key), 128)] = _bounded(str(value), 32)
    post_metrics = data.get("post_metrics") or {}
    if not isinstance(post_metrics, dict):
        raise ValueError("post_metrics must be object")
    metrics_out: dict[str, float | int | None] = {}
    for key, value in post_metrics.items():
        k = _bounded(str(key), 32)
        if value is None:
            metrics_out[k] = None
        elif isinstance(value, bool):
            raise ValueError("bool metric not allowed")
        elif isinstance(value, int):
            metrics_out[k] = value
        elif isinstance(value, float):
            metrics_out[k] = value
        else:
            raise ValueError("metric must be number or null")
    notes = _bounded(str(data.get("notes") or ""), MAX_NOTE)
    lesson_raw = data.get("lesson_completed")
    lesson_completed = None if lesson_raw is None else bool(lesson_raw)
    pending = data.get("pending_checkpoint_ids") or []
    if not isinstance(pending, list):
        raise ValueError("pending_checkpoint_ids must be list")
    pending_ids = [_bounded(str(x), 128) for x in pending][:20]
    return EveningPayload(
        schema_version=version,
        day=day,
        owner_id=owner,
        checklist=checklist,
        review_actions=review_actions,
        unplanned_classifications=unplanned_out,
        post_metrics=metrics_out,
        notes=notes,
        lesson_completed=lesson_completed,
        pending_checkpoint_ids=pending_ids,
    )


def evening_payload_json_schema() -> dict[str, Any]:
    """Documented JSON Schema fragment for operators (F3/F4)."""
    return {
        "$id": "https://mathai.com.br/schemas/evening-payload.v1.json",
        "type": "object",
        "required": ["schema_version", "day", "owner_id", "checklist"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "day": {"type": "string", "format": "date"},
            "owner_id": {"type": "string", "maxLength": 64},
            "checklist": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "required": ["task_id", "done"],
                    "properties": {
                        "task_id": {"type": "string", "maxLength": 128},
                        "done": {"type": "boolean"},
                        "classification": {"type": "string", "maxLength": 32},
                    },
                },
            },
            "review_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["draft_id", "action"],
                    "properties": {
                        "draft_id": {"type": "string"},
                        "action": {"enum": ["ready", "reject", "edit"]},
                    },
                },
            },
            "notes": {"type": "string", "maxLength": MAX_NOTE},
        },
    }


def _bounded(text: str, limit: int) -> str:
    text = text.strip()
    if not text or len(text) > limit:
        raise ValueError("field empty or too long")
    return text
