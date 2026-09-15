"""JSON-serializable discovery result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DiscoveryItem:
    kind: str
    id: str
    source: str
    url: str | None
    title: str
    observed_at: str
    # None means unclassified: info only, no scope penalty (F4 sets this later).
    classification: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.observed_at, self.source, self.kind, self.id)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "observed_at": self.observed_at,
            "classification": self.classification,
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DiscoveryItem:
        return cls(
            kind=str(data["kind"]),
            id=str(data["id"]),
            source=str(data["source"]),
            url=data.get("url"),
            title=str(data.get("title") or ""),
            observed_at=str(data["observed_at"]),
            classification=data.get("classification"),
            meta=dict(data.get("meta") or {}),
        )


@dataclass(frozen=True)
class DiscoveryError:
    source: str
    message: str
    at: str

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "message": self.message, "at": self.at}


@dataclass(frozen=True)
class DiscoveryBatch:
    items: list[DiscoveryItem]
    errors: list[DiscoveryError]
    checkpoints: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "items": [item.to_json() for item in self.items],
            "errors": [error.to_json() for error in self.errors],
            "checkpoints": self.checkpoints,
        }
