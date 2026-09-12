"""Operator-curated manifest ingest and SQLite runtime index.

Entries without a complete known mapping are withheld. Paths cannot escape the
content root through `..` or symlinks. Handles are random and reused only for
the same workspace+path across reindex of the same revision; a new revision
replaces the row and issues a new handle.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

NAMESPACE_PATHS = {"pesquisa.tcc": "pesquisa/tcc"}
CLASSIFICATIONS = frozenset({"public", "shared", "internal", "restricted"})
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HEADING = re.compile(r"(?m)^#\s+(.+)$")
ENTRY_KEYS = frozenset({"path", "namespace", "classification", "layer", "private"})
MANIFEST_KEYS = frozenset({"workspace_id", "source_revision", "entries"})


@dataclass(frozen=True)
class Node:
    handle: str
    workspace_id: str
    namespace: str
    classification: str
    layer: str
    source_revision: str
    private: bool
    path: str
    title: str
    body: str


class ContextStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                handle TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                classification TEXT NOT NULL,
                layer TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                private INTEGER NOT NULL CHECK(private IN (0, 1)),
                path TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                UNIQUE (workspace_id, path)
            );
            CREATE TABLE IF NOT EXISTS edges (
                source_handle TEXT NOT NULL REFERENCES nodes(handle) ON DELETE CASCADE,
                target_handle TEXT NOT NULL REFERENCES nodes(handle) ON DELETE CASCADE,
                PRIMARY KEY (source_handle, target_handle)
            );
            CREATE TABLE IF NOT EXISTS withheld (
                workspace_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (workspace_id, alias)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def nodes(self, workspace_id: str) -> tuple[Node, ...]:
        rows = self.connection.execute(
            "SELECT * FROM nodes WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        return tuple(_node(row) for row in rows)

    def node_by_handle(self, handle: str) -> Node | None:
        row = self.connection.execute(
            "SELECT * FROM nodes WHERE handle = ?", (handle,)
        ).fetchone()
        return None if row is None else _node(row)

    def edges(self, workspace_id: str) -> tuple[tuple[str, str], ...]:
        rows = self.connection.execute(
            """
            SELECT e.source_handle, e.target_handle FROM edges e
            JOIN nodes s ON s.handle = e.source_handle
            WHERE s.workspace_id = ?
            """,
            (workspace_id,),
        ).fetchall()
        return tuple((row["source_handle"], row["target_handle"]) for row in rows)

    def withheld_aliases(self, workspace_id: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT alias FROM withheld WHERE workspace_id = ?", (workspace_id,)
        ).fetchall()
        return frozenset(row["alias"] for row in rows)

    def replace_workspace(
        self,
        workspace_id: str,
        nodes: list[Node],
        edges: list[tuple[str, str]],
        withheld: set[str],
    ) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            previous = {
                (row["path"], row["source_revision"]): row["handle"]
                for row in self.connection.execute(
                    "SELECT path, source_revision, handle FROM nodes WHERE workspace_id = ?",
                    (workspace_id,),
                )
            }
            handles = {
                node.path: previous.get((node.path, node.source_revision), node.handle)
                for node in nodes
            }
            remap = {node.handle: handles[node.path] for node in nodes}
            self.connection.execute("DELETE FROM withheld WHERE workspace_id = ?", (workspace_id,))
            self.connection.execute(
                """
                DELETE FROM edges WHERE source_handle IN (
                    SELECT handle FROM nodes WHERE workspace_id = ?
                )
                """,
                (workspace_id,),
            )
            self.connection.execute("DELETE FROM nodes WHERE workspace_id = ?", (workspace_id,))
            for node in nodes:
                self.connection.execute(
                    "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        handles[node.path],
                        node.workspace_id,
                        node.namespace,
                        node.classification,
                        node.layer,
                        node.source_revision,
                        int(node.private),
                        node.path,
                        node.title,
                        node.body,
                    ),
                )
            seen: set[tuple[str, str]] = set()
            for source, target in edges:
                mapped = (remap.get(source), remap.get(target))
                if None in mapped or mapped[0] == mapped[1] or mapped in seen:
                    continue
                seen.add(mapped)
                self.connection.execute("INSERT INTO edges VALUES (?, ?)", mapped)
            for alias in sorted(alias for alias in withheld if alias):
                self.connection.execute(
                    "INSERT OR IGNORE INTO withheld VALUES (?, ?)", (workspace_id, alias)
                )


def ingest_manifest(store: ContextStore, manifest: dict | Path, content_root: Path) -> None:
    if isinstance(manifest, Path):
        manifest = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ValueError("Invalid context manifest")
    workspace_id = manifest["workspace_id"]
    revision = manifest["source_revision"]
    entries = manifest["entries"]
    if not _token(workspace_id) or not _token(revision) or not isinstance(entries, list):
        raise ValueError("Invalid context manifest")
    root = content_root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Content root must be a real directory")

    parsed: list[tuple[dict, str]] = []
    for item in entries:
        rel = _normalize_path(item.get("path") if isinstance(item, dict) else None)
        parsed.append((item if isinstance(item, dict) else {}, rel or ""))

    nodes: list[Node] = []
    by_path: dict[str, Node] = {}
    by_title: dict[str, list[Node]] = {}
    for entry, rel in parsed:
        file_path = _readable_file(root, rel) if rel else None
        title, body = ("", "")
        if file_path is not None:
            title, body = _title_and_body(file_path.read_text(encoding="utf-8"), rel)
        node = _try_node(workspace_id, revision, entry, rel, title, body, readable=file_path is not None)
        if node is None:
            continue
        nodes.append(node)
        by_path[node.path] = node
        by_title.setdefault(node.title, []).append(node)

    withheld: set[str] = set()
    indexed = {node.path for node in nodes}
    for entry, rel in parsed:
        if rel and rel not in indexed:
            withheld.update(_path_aliases(rel))
            file_path = _readable_file(root, rel)
            if file_path is not None:
                title, _ = _title_and_body(file_path.read_text(encoding="utf-8"), rel)
                if title:
                    withheld.add(title)

    unique_title = {title: group[0] for title, group in by_title.items() if len(group) == 1}
    edges: list[tuple[str, str]] = []
    for node in nodes:
        for target in _link_targets(node.body):
            matched = _resolve_link(target, node.path, by_path, unique_title)
            if matched is not None and matched.handle != node.handle:
                edges.append((node.handle, matched.handle))

    store.replace_workspace(workspace_id, nodes, edges, withheld)


def _try_node(
    workspace_id: str,
    revision: str,
    entry: dict,
    rel: str,
    title: str,
    body: str,
    *,
    readable: bool,
) -> Node | None:
    if not readable or set(entry) - ENTRY_KEYS or not rel or not title:
        return None
    namespace = entry.get("namespace")
    classification = entry.get("classification")
    layer = entry.get("layer")
    private = entry.get("private", False)
    if "private" in entry and not isinstance(entry["private"], bool):
        return None
    if not isinstance(private, bool):
        return None
    if namespace not in NAMESPACE_PATHS or classification not in CLASSIFICATIONS:
        return None
    if not _token(layer):
        return None
    prefix = NAMESPACE_PATHS[namespace]
    if rel != prefix and not rel.startswith(prefix + "/"):
        return None
    if not rel.endswith(".md"):
        return None
    if private:
        classification = "restricted"
    return Node(
        handle=secrets.token_urlsafe(32),
        workspace_id=workspace_id,
        namespace=namespace,
        classification=classification,
        layer=layer,
        source_revision=revision,
        private=private,
        path=rel,
        title=title,
        body=body,
    )


def _readable_file(root: Path, rel: str) -> Path | None:
    current = root
    for part in Path(rel).parts:
        current = current / part
        try:
            if current.is_symlink() or not current.exists():
                return None
        except OSError:
            return None
    if not current.is_file():
        return None
    try:
        resolved = current.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return resolved


def _normalize_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or "\0" in value:
        return None
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else None


def _path_aliases(rel: str) -> set[str]:
    path = Path(rel)
    aliases = {rel, path.name, path.stem}
    if rel.endswith(".md"):
        aliases.add(rel[:-3])
    return {item for item in aliases if item}


def _title_and_body(raw: str, rel: str) -> tuple[str, str]:
    match = HEADING.search(raw)
    title = match.group(1).strip() if match else Path(rel).stem.replace("-", " ")
    return title, raw


def _link_targets(body: str) -> list[str]:
    targets = [match.group(1).split("|", 1)[0].split("#", 1)[0].strip() for match in WIKILINK.finditer(body)]
    targets.extend(match.group(2).split("#", 1)[0].strip() for match in MD_LINK.finditer(body))
    return [target for target in targets if target]


def _resolve_link(
    target: str, source_path: str, by_path: dict[str, Node], by_title: dict[str, Node]
) -> Node | None:
    if target in by_title:
        return by_title[target]
    candidates = [target]
    normalized = _normalize_path(target)
    if normalized:
        candidates.append(normalized)
        if not normalized.endswith(".md"):
            candidates.append(normalized + ".md")
    relative = _normalize_path(str(Path(source_path).parent / target))
    if relative:
        candidates.append(relative)
        if not relative.endswith(".md"):
            candidates.append(relative + ".md")
    for candidate in candidates:
        if candidate in by_path:
            return by_path[candidate]
    name = Path(target).name
    stem = Path(target).stem
    matches = [
        node
        for path, node in by_path.items()
        if Path(path).name == name or Path(path).stem == stem
    ]
    return matches[0] if len(matches) == 1 else None


def _token(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value


def _node(row: sqlite3.Row) -> Node:
    return Node(
        handle=row["handle"],
        workspace_id=row["workspace_id"],
        namespace=row["namespace"],
        classification=row["classification"],
        layer=row["layer"],
        source_revision=row["source_revision"],
        private=bool(row["private"]),
        path=row["path"],
        title=row["title"],
        body=row["body"],
    )
