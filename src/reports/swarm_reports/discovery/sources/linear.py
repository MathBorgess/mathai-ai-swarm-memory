"""Linear discovery: changed issues since the checkpoint.

Two adapters behind one interface, chosen by config:
- token adapter: hardcoded read-only GraphQL `query` (no mutation string),
  POSTed to `endpoint` with `Authorization: <token>` from `os.environ[token_env]`.
  The token is never included in any returned/logged value.
- command adapter: a configured read-only argv (no shell) that must print a
  JSON array of `{identifier, title, url, updatedAt, state}` objects.

Never fabricates a Linear identifier: an empty result is "found nothing".
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.models import DiscoveryItem
from swarm_reports.discovery.sources.base import SourceError, SourceResult

# Read-only: `query`, never `mutation`. Server-side filter on updatedAt keeps
# pages bounded to genuinely-changed issues.
DISCOVER_QUERY = """
query DiscoverChangedIssues($after: String, $since: DateTimeOrDuration) {
  issues(first: 50, after: $after, orderBy: updatedAt, filter: { updatedAt: { gt: $since } }) {
    nodes { id identifier title url updatedAt state { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

assert "mutation" not in DISCOVER_QUERY.lower()


class HttpResponse(NamedTuple):
    status: int
    body: str


Opener = Callable[[str, dict[str, str], bytes, int], HttpResponse]
CommandRunner = Callable[[list[str], int], str]


def _default_opener(url: str, headers: dict[str, str], body: bytes, timeout: int) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (read-only GraphQL query)
        return HttpResponse(response.status, response.read().decode("utf-8"))


def _default_command_runner(argv: list[str], timeout: int) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise SourceError("linear: command adapter failed")
    return completed.stdout


@dataclass
class LinearConfig:
    token_env: str | None = None
    command: tuple[str, ...] | None = None
    endpoint: str = "https://api.linear.app/graphql"
    max_pages: int = 3
    timeout_seconds: int = 20
    opener: Opener = _default_opener
    command_runner: CommandRunner = _default_command_runner


class LinearSource:
    name = "linear"

    def __init__(self, config: LinearConfig) -> None:
        self.config = config
        if not 1 <= config.max_pages <= 20 or not 1 <= config.timeout_seconds <= 120:
            raise SourceError("linear: invalid pagination/timeout bounds")

    def discover(self, checkpoint: SourceCheckpoint) -> SourceResult:
        if self.config.command:
            return self._discover_via_command(checkpoint)
        if self.config.token_env:
            return self._discover_via_token(checkpoint)
        raise SourceError("linear: not configured (no token_env or command)")

    def _discover_via_command(self, checkpoint: SourceCheckpoint) -> SourceResult:
        raw = self.config.command_runner(list(self.config.command or ()), self.config.timeout_seconds)
        try:
            rows = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise SourceError(f"linear: command adapter returned invalid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise SourceError("linear: command adapter must return a JSON array")
        items, new_ids, max_observed = self._to_items(rows, checkpoint)
        return SourceResult(items=items, new_cursor=max_observed or checkpoint.cursor, new_ids=new_ids)

    def _discover_via_token(self, checkpoint: SourceCheckpoint) -> SourceResult:
        token = os.environ.get(self.config.token_env or "")
        if not token:
            raise SourceError(f"linear: env var '{self.config.token_env}' is not set")
        items: list[DiscoveryItem] = []
        new_ids: list[str] = []
        max_observed = checkpoint.paging.get("high") or checkpoint.cursor
        after: str | None = checkpoint.paging.get("after")
        truncated = False
        for page in range(1, self.config.max_pages + 1):
            variables = {"after": after, "since": checkpoint.cursor}
            payload = json.dumps({"query": DISCOVER_QUERY, "variables": variables}).encode("utf-8")
            headers = {"Content-Type": "application/json", "Authorization": token}
            response = self.config.opener(self.config.endpoint, headers, payload, self.config.timeout_seconds)
            if response.status != 200:
                raise SourceError(f"linear: HTTP {response.status}")
            data = json.loads(response.body)
            if data.get("errors"):
                raise SourceError("linear: API error")
            issues = ((data.get("data") or {}).get("issues")) or {}
            nodes = issues.get("nodes") or []
            page_items, page_ids, page_max = self._to_items(nodes, checkpoint)
            items.extend(page_items)
            new_ids.extend(page_ids)
            if page_max and (max_observed is None or page_max > max_observed):
                max_observed = page_max
            page_info = issues.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            next_after = page_info.get("endCursor")
            if not next_after or next_after == after:
                raise SourceError("linear: invalid pagination cursor")
            after = next_after
            if page == self.config.max_pages:
                truncated = True
        new_cursor = checkpoint.cursor if truncated else max_observed
        return SourceResult(items=items, new_cursor=new_cursor, new_ids=new_ids, truncated=truncated,
            paging={"after": after, "high": max_observed} if truncated else {})

    def _to_items(
        self, rows: list[dict[str, Any]], checkpoint: SourceCheckpoint
    ) -> tuple[list[DiscoveryItem], list[str], str | None]:
        items: list[DiscoveryItem] = []
        new_ids: list[str] = []
        max_observed: str | None = None
        for row in rows:
            identifier = row.get("identifier")
            updated_at = row.get("updatedAt")
            if not identifier or not updated_at:
                continue
            if checkpoint.cursor and str(updated_at) < checkpoint.cursor:
                continue
            if max_observed is None or str(updated_at) > max_observed:
                max_observed = str(updated_at)
            item_id = f"linear:{identifier}:{updated_at}"
            if item_id in checkpoint.seen_ids or item_id in new_ids:
                continue
            state = (row.get("state") or {}).get("name") if isinstance(row.get("state"), dict) else row.get("state")
            items.append(
                DiscoveryItem(
                    kind="linear_issue",
                    id=item_id,
                    source="linear",
                    url=row.get("url"),
                    title=str(row.get("title") or identifier),
                    observed_at=str(updated_at),
                    meta={"identifier": identifier, "state": state},
                )
            )
            new_ids.append(item_id)
        return items, new_ids, max_observed
