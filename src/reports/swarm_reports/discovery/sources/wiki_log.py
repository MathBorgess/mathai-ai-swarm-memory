"""Discovery of new `wiki/log.md` rows (append-only markdown table).

Dedup is by content hash, not line number, so it survives log rotation and
re-appended duplicate lines: a previously-seen row is skipped even if the file
was rewritten and the row now sits at a different line. `cursor` (line count)
is only a fast-path skip-ahead for the common append-only case; if the file is
shorter than the stored cursor, the whole file is rescanned and hashes filter
out what was already reported.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.models import DiscoveryItem
from swarm_reports.discovery.sources.base import SourceError, SourceResult

MAX_BYTES = 2_000_000


def _row_hash(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    # separator row: |---|---|...
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if all(set(c) <= {"-", ":"} and c for c in cells):
        return False
    if cells and cells[0].lower() == "date":
        return False
    return True


@dataclass
class WikiLogConfig:
    log_path: Path
    wiki_root: Path
    max_bytes: int = MAX_BYTES


class WikiLogSource:
    name = "wiki_log"

    def __init__(self, config: WikiLogConfig) -> None:
        self.config = config

    def discover(self, checkpoint: SourceCheckpoint) -> SourceResult:
        log_real = self.config.log_path.resolve()
        root_real = self.config.wiki_root.resolve()
        if root_real not in log_real.parents and log_real != root_real:
            raise SourceError(f"wiki_log: '{self.config.log_path}' escapes wiki root '{self.config.wiki_root}'")
        if not log_real.exists():
            raise SourceError(f"wiki_log: '{self.config.log_path}' does not exist")
        size = log_real.stat().st_size
        if size > self.config.max_bytes:
            raise SourceError(f"wiki_log: '{self.config.log_path}' exceeds {self.config.max_bytes} bytes")
        text = log_real.read_text(encoding="utf-8")
        lines = text.splitlines()

        prior_cursor = 0
        if checkpoint.cursor:
            try:
                prior_cursor = int(checkpoint.cursor)
            except ValueError:
                prior_cursor = 0
        # Fast path only if the file only grew (append-only); otherwise rescan
        # from the top and rely on hash dedup against seen_ids.
        start = prior_cursor if len(lines) >= prior_cursor else 0
        candidate_lines = lines[start:] if start else lines

        seen = set(checkpoint.seen_ids)
        items: list[DiscoveryItem] = []
        new_ids: list[str] = []
        for line in candidate_lines:
            if not _is_table_row(line):
                continue
            row_id = f"wiki_log:{_row_hash(line)}"
            if row_id in seen:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            date = cells[0] if len(cells) > 0 else ""
            op = cells[1] if len(cells) > 1 else ""
            slug = cells[2] if len(cells) > 2 else ""
            title = f"{op} {slug}".strip() or line.strip()[:120]
            items.append(
                DiscoveryItem(
                    kind="wiki_log_entry",
                    id=row_id,
                    source="wiki_log",
                    url=None,
                    title=title,
                    observed_at=date or "",
                    meta={"raw": line.strip()},
                )
            )
            new_ids.append(row_id)
            seen.add(row_id)

        return SourceResult(items=items, new_cursor=str(len(lines)), new_ids=new_ids)
