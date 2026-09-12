"""Runtime context index with authorization-first graph filter.

This package is not wired into api.py. Agent A must inject authorize() and
include the router; tests use an explicit fake. Nothing here talks to Hermes.
"""

from app.context.router import build_router
from app.context.store import ContextStore, ingest_manifest

__all__ = ["ContextStore", "build_router", "ingest_manifest"]
