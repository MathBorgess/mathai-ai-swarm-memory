"""Durable F5 runtime state: deferred handoffs, digest cards, last quota note."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm_reports.dispatch.digest import DecisionCard

RUNTIME_FILE = "dispatch-runtime.json"


@dataclass
class DeferredHandoff:
    task_id: str
    title: str
    reason: str

    def to_json(self) -> dict[str, str]:
        return {"task_id": self.task_id, "title": self.title, "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeferredHandoff:
        return cls(
            task_id=str(data["task_id"]),
            title=str(data.get("title") or ""),
            reason=str(data.get("reason") or "quota"),
        )


@dataclass
class RuntimeState:
    deferred: list[DeferredHandoff] = field(default_factory=list)
    digest_cards: list[dict[str, Any]] = field(default_factory=list)
    dispatch_note: str = ""
    costs_unknown: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "deferred": [d.to_json() for d in self.deferred],
            "digest_cards": list(self.digest_cards),
            "dispatch_note": self.dispatch_note,
            "costs_unknown": self.costs_unknown,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RuntimeState:
        return cls(
            deferred=[DeferredHandoff.from_json(x) for x in data.get("deferred") or []],
            digest_cards=list(data.get("digest_cards") or []),
            dispatch_note=str(data.get("dispatch_note") or ""),
            costs_unknown=bool(data.get("costs_unknown", True)),
        )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".dispatch-runtime-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def load_runtime(state_dir: Path) -> RuntimeState:
    path = state_dir / RUNTIME_FILE
    if not path.exists():
        return RuntimeState()
    return RuntimeState.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_runtime(state_dir: Path, state: RuntimeState) -> None:
    _atomic_write(
        state_dir / RUNTIME_FILE,
        json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n",
    )


def cards_from_runtime(state: RuntimeState) -> list[DecisionCard]:
    out: list[DecisionCard] = []
    for raw in state.digest_cards:
        try:
            out.append(
                DecisionCard(
                    kind=raw["kind"],
                    location=raw["location"],
                    question=raw["question"],
                    why=raw.get("why") or "",
                    source=raw.get("source") or "declared",
                    pr=raw.get("pr") or "",
                    verified_line=bool(raw.get("verified_line")),
                )
            )
        except (KeyError, TypeError):
            continue
    return out


__all__ = [
    "DeferredHandoff",
    "RUNTIME_FILE",
    "RuntimeState",
    "cards_from_runtime",
    "load_runtime",
    "save_runtime",
]
