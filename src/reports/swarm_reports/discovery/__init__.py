"""Read-only discovery of activity since the last successful per-source checkpoint.

Sources: GitHub (gh CLI, allowlisted repos), Linear (read-only GraphQL query),
wiki/log.md (append-only table). See `orchestrator.run_discovery`.
"""

from swarm_reports.discovery.models import DiscoveryBatch, DiscoveryError, DiscoveryItem
from swarm_reports.discovery.orchestrator import DiscoveryConfig, run_discovery

__all__ = [
    "DiscoveryBatch",
    "DiscoveryError",
    "DiscoveryItem",
    "DiscoveryConfig",
    "run_discovery",
]
