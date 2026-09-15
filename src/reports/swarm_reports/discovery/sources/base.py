"""Shared contract for discovery source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.models import DiscoveryItem


class SourceError(Exception):
    """Raised when a source cannot run: misconfigured, unreachable, or rejected input.

    A source failure must surface as a `DiscoveryError`, never a silent empty success.
    """


@dataclass
class SourceResult:
    items: list[DiscoveryItem] = field(default_factory=list)
    new_cursor: str | None = None
    new_ids: list[str] = field(default_factory=list)
    truncated: bool = False
    paging: dict = field(default_factory=dict)


class DiscoverySource(Protocol):
    name: str

    def discover(self, checkpoint: SourceCheckpoint) -> SourceResult: ...
