"""Runs configured discovery sources, merges results, persists checkpoints.

Contract:
- A source failure raises inside the source; the orchestrator turns it into a
  `DiscoveryError` and leaves that source's checkpoint bucket untouched (its
  next run retries from the same cursor).
- Items whose id is in `known_ledger_action_ids` are dropped explicitly (the
  swarm's own already-ledgered actions) — never inferred from author name.
- Output ordering is a stable sort on (observed_at, source, kind, id), so same
  -timestamp ties and cross-source overlap are deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from swarm_reports.discovery.checkpoints import (
    SourceCheckpoint,
    checkpoint_lock,
    default_checkpoint_path,
    load_checkpoints,
    save_checkpoints,
)
from swarm_reports.discovery.models import DiscoveryBatch, DiscoveryError, DiscoveryItem
from swarm_reports.discovery.sources.base import DiscoverySource, SourceError
from swarm_reports.discovery.sources.github import GithubConfig, GithubSource
from swarm_reports.discovery.sources.linear import LinearConfig, LinearSource
from swarm_reports.discovery.sources.wiki_log import WikiLogConfig, WikiLogSource


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DiscoveryConfig:
    state_dir: Path
    github: GithubConfig | None = None
    linear: LinearConfig | None = None
    wiki_log: WikiLogConfig | None = None
    known_ledger_action_ids: frozenset[str] = field(default_factory=frozenset)


def _build_sources(config: DiscoveryConfig) -> dict[str, DiscoverySource]:
    sources: dict[str, DiscoverySource] = {}
    if config.github is not None:
        sources["github"] = GithubSource(config.github)
    if config.linear is not None:
        sources["linear"] = LinearSource(config.linear)
    if config.wiki_log is not None:
        sources["wiki_log"] = WikiLogSource(config.wiki_log)
    return sources


def run_discovery(config: DiscoveryConfig) -> DiscoveryBatch:
    sources = _build_sources(config)
    if not sources:
        raise SourceError("discovery: no sources configured")

    state_path = default_checkpoint_path(config.state_dir)
    items: list[DiscoveryItem] = []
    errors: list[DiscoveryError] = []

    with checkpoint_lock(config.state_dir):
        state = load_checkpoints(state_path)
        for name, source in sources.items():
            checkpoint = state.sources.get(name, SourceCheckpoint())
            try:
                result = source.discover(checkpoint)
            except SourceError as exc:
                errors.append(DiscoveryError(source=name, message=str(exc), at=utc_now_iso()))
                continue
            except Exception as exc:  # defensive: source bug must not corrupt other sources
                errors.append(DiscoveryError(source=name, message=f"unexpected error: {exc}", at=utc_now_iso()))
                continue
            kept = [item for item in result.items if item.id not in config.known_ledger_action_ids]
            items.extend(kept)
            state.sources[name] = checkpoint.with_update(cursor=result.new_cursor, new_ids=result.new_ids)
        save_checkpoints(state_path, state)
        checkpoints_snapshot = {name: cp.to_json() for name, cp in state.sources.items() if name in sources}

    items.sort(key=lambda item: item.sort_key())
    return DiscoveryBatch(items=items, errors=errors, checkpoints=checkpoints_snapshot)
