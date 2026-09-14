from __future__ import annotations

from pathlib import Path

import pytest

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.sources.base import SourceError
from swarm_reports.discovery.sources.wiki_log import WikiLogConfig, WikiLogSource

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def test_parses_added_rows_since_checkpoint(tmp_path: Path):
    log = tmp_path / "wiki" / "log.md"
    log.parent.mkdir()
    log.write_text((FIXTURES / "wiki-log-sample.md").read_text(encoding="utf-8"), encoding="utf-8")
    config = WikiLogConfig(log_path=log, wiki_root=tmp_path / "wiki")
    result = WikiLogSource(config).discover(SourceCheckpoint())
    assert len(result.items) == 2
    assert result.items[0].kind == "wiki_log_entry"
    assert result.new_cursor == str(len(log.read_text(encoding="utf-8").splitlines()))


def test_second_run_with_unchanged_log_reports_nothing_new(tmp_path: Path):
    log = tmp_path / "wiki" / "log.md"
    log.parent.mkdir()
    log.write_text((FIXTURES / "wiki-log-sample.md").read_text(encoding="utf-8"), encoding="utf-8")
    config = WikiLogConfig(log_path=log, wiki_root=tmp_path / "wiki")
    source = WikiLogSource(config)
    first = source.discover(SourceCheckpoint())
    checkpoint = SourceCheckpoint(cursor=first.new_cursor, seen_ids=first.new_ids)
    second = source.discover(checkpoint)
    assert second.items == []


def test_appended_line_is_reported_once(tmp_path: Path):
    log = tmp_path / "wiki" / "log.md"
    log.parent.mkdir()
    log.write_text((FIXTURES / "wiki-log-sample.md").read_text(encoding="utf-8"), encoding="utf-8")
    config = WikiLogConfig(log_path=log, wiki_root=tmp_path / "wiki")
    source = WikiLogSource(config)
    first = source.discover(SourceCheckpoint())
    checkpoint = SourceCheckpoint(cursor=first.new_cursor, seen_ids=first.new_ids)

    with log.open("a", encoding="utf-8") as handle:
        handle.write("| 2026-09-14 | ops | new-thing | `x` | fresh row |\n")

    second = source.discover(checkpoint)
    assert len(second.items) == 1
    assert "new-thing" in second.items[0].title


def test_rotation_reset_does_not_duplicate_already_seen_rows(tmp_path: Path):
    log = tmp_path / "wiki" / "log.md"
    log.parent.mkdir()
    original = (FIXTURES / "wiki-log-sample.md").read_text(encoding="utf-8")
    log.write_text(original, encoding="utf-8")
    config = WikiLogConfig(log_path=log, wiki_root=tmp_path / "wiki")
    source = WikiLogSource(config)
    first = source.discover(SourceCheckpoint())
    checkpoint = SourceCheckpoint(cursor=first.new_cursor, seen_ids=first.new_ids)

    # Simulate rotation: file rewritten shorter (header only), then the same
    # rows plus one new row re-appended (append-only log rotated + replayed).
    rows = [line for line in original.splitlines() if line.strip().startswith("|")]
    log.write_text("# Wiki log\n\n" + "\n".join(rows) + "\n| 2026-09-15 | ops | after-rotation | `y` | z |\n", encoding="utf-8")

    second = source.discover(checkpoint)
    assert len(second.items) == 1
    assert "after-rotation" in second.items[0].title


def test_symlink_escape_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("| 2026-09-14 | ops | x | y | z |\n", encoding="utf-8")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    link = wiki_root / "log.md"
    link.symlink_to(outside)
    config = WikiLogConfig(log_path=link, wiki_root=wiki_root)
    with pytest.raises(SourceError):
        WikiLogSource(config).discover(SourceCheckpoint())


def test_missing_file_raises_not_silent_empty(tmp_path: Path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    config = WikiLogConfig(log_path=wiki_root / "missing.md", wiki_root=wiki_root)
    with pytest.raises(SourceError):
        WikiLogSource(config).discover(SourceCheckpoint())
