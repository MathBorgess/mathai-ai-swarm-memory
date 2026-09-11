"""Authorization-first query, graph walk, sanitization and resolve.

Visible nodes and edges are selected before scoring, limit or neighbor
expansion. Hidden nodes never contribute score, never occupy a limit slot and
never bridge two visible nodes. Denied and unknown handles share the empty
item list.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping

from app.context.store import MD_LINK, NAMESPACE_PATHS, WIKILINK, ContextStore, Node, _normalize_path

POLICY_VERSION = "v1"
PRINCIPAL_KEYS = frozenset(
    {"principal_id", "workspace_id", "scopes", "classifications", "family_id", "expires_at"}
)


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def validate_principal(mapping: Mapping) -> dict:
    if not isinstance(mapping, Mapping) or set(mapping) < PRINCIPAL_KEYS:
        raise AuthError(401, "Invalid credential")
    principal_id = mapping["principal_id"]
    workspace_id = mapping["workspace_id"]
    family_id = mapping["family_id"]
    scopes = mapping["scopes"]
    classifications = mapping["classifications"]
    expires_at = mapping["expires_at"]
    if not all(isinstance(value, str) and value.strip() == value and value for value in (principal_id, workspace_id, family_id)):
        raise AuthError(401, "Invalid credential")
    if not _string_tuple(scopes) or not _string_tuple(classifications):
        raise AuthError(401, "Invalid credential")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise AuthError(401, "Invalid credential")
    if expires_at <= datetime.now(timezone.utc):
        raise AuthError(401, "Invalid credential")
    return {
        "principal_id": principal_id,
        "workspace_id": workspace_id,
        "scopes": tuple(scopes),
        "classifications": tuple(classifications),
        "family_id": family_id,
        "expires_at": expires_at,
    }


def read_scopes(principal: Mapping) -> tuple[str, ...]:
    used = []
    for scope in principal["scopes"]:
        if not isinstance(scope, str) or not scope.startswith("ctx:read:"):
            continue
        namespace = scope.removeprefix("ctx:read:")
        if namespace in NAMESPACE_PATHS:
            used.append(scope)
    return tuple(dict.fromkeys(used))


def require_read(principal: Mapping) -> tuple[str, ...]:
    used = read_scopes(principal)
    if not used:
        raise AuthError(403, "Operation out of scope")
    return used


def search(store: ContextStore, principal: Mapping, query: str, limit: int) -> dict:
    used = require_read(principal)
    visible, hidden, allowed_edges = _authorized_graph(store, principal)
    forbidden = _forbidden_aliases(hidden, store.withheld_aliases(principal["workspace_id"]))
    texts = {node.handle: sanitize(node.body, visible, forbidden) for node in visible}
    scored = []
    for node in visible:
        score = _score_text(node.title, texts[node.handle], query)
        if score > 0:
            scored.append((score, node))
    matched = {node.handle for _, node in scored}
    by_handle = {node.handle: node for node in visible}
    extras = []
    for source, target in allowed_edges:
        if source in matched and target not in matched and target in by_handle:
            extras.append((0, by_handle[target]))
            matched.add(target)
        elif target in matched and source not in matched and source in by_handle:
            extras.append((0, by_handle[source]))
            matched.add(source)
    ranked = sorted(scored + extras, key=lambda item: (-item[0], item[1].handle))[:limit]
    items = [
        {"handle": node.handle, "text": texts[node.handle], "source_revision": node.source_revision}
        for _, node in ranked
    ]
    return _envelope(principal, used, items)


def resolve_handles(store: ContextStore, principal: Mapping, handles: list[str]) -> dict:
    used = require_read(principal)
    visible, hidden, _ = _authorized_graph(store, principal)
    visible_by_handle = {node.handle: node for node in visible}
    forbidden = _forbidden_aliases(hidden, store.withheld_aliases(principal["workspace_id"]))
    items = []
    seen: set[str] = set()
    for handle in handles:
        if handle in seen:
            continue
        seen.add(handle)
        node = visible_by_handle.get(handle)
        if node is None:
            continue
        items.append(_item(node, visible, forbidden))
    return _envelope(principal, used, items)


def _authorized_graph(
    store: ContextStore, principal: Mapping
) -> tuple[list[Node], list[Node], tuple[tuple[str, str], ...]]:
    workspace_id = principal["workspace_id"]
    nodes = store.nodes(workspace_id)
    visible = [node for node in nodes if _visible(node, principal)]
    allowed = {node.handle for node in visible}
    hidden = [node for node in nodes if node.handle not in allowed]
    edges = tuple(
        (source, target)
        for source, target in store.edges(workspace_id)
        if source in allowed and target in allowed
    )
    return visible, hidden, edges


def _visible(node: Node, principal: Mapping) -> bool:
    if node.workspace_id != principal["workspace_id"]:
        return False
    if f"ctx:read:{node.namespace}" not in principal["scopes"]:
        return False
    if node.classification not in principal["classifications"]:
        return False
    if node.private and "restricted" not in principal["classifications"]:
        return False
    return True


def _score_text(title: str, body: str, query: str) -> int:
    terms = [term for term in re.split(r"\s+", query.casefold()) if term]
    if not terms:
        return 0
    title_l = title.casefold()
    body_l = body.casefold()
    return sum(10 * title_l.count(term) + body_l.count(term) for term in terms)


def _item(node: Node, visible: list[Node], forbidden: frozenset[str]) -> dict:
    return {
        "handle": node.handle,
        "text": sanitize(node.body, visible, forbidden),
        "source_revision": node.source_revision,
    }


def sanitize(text: str, visible: list[Node], forbidden: frozenset[str]) -> str:
    visible_titles = {node.title: node for node in visible}
    visible_paths = {node.path: node for node in visible}
    for node in visible:
        visible_paths.setdefault(node.path[:-3] if node.path.endswith(".md") else node.path, node)
        visible_paths.setdefault(node.path.split("/")[-1], node)
        visible_paths.setdefault(node.path.split("/")[-1].removesuffix(".md"), node)

    def keep_target(target: str) -> bool:
        name = target.split("|", 1)[0].split("#", 1)[0].strip()
        if name in visible_titles:
            return True
        normalized = _normalize_path(name)
        if normalized and (normalized in visible_paths or normalized + ".md" in visible_paths):
            return True
        return name in visible_paths

    def wiki(match: re.Match[str]) -> str:
        return match.group(0) if keep_target(match.group(1)) else ""

    def markdown(match: re.Match[str]) -> str:
        return match.group(0) if keep_target(match.group(2)) else ""

    redacted = WIKILINK.sub(wiki, text)
    redacted = MD_LINK.sub(markdown, redacted)
    for alias in sorted((item for item in forbidden if "/" in item or item.endswith(".md")), key=len, reverse=True):
        redacted = redacted.replace(alias, "")
    return re.sub(r"[ \t]+\n", "\n", redacted).strip()


def _forbidden_aliases(hidden: list[Node], withheld: frozenset[str]) -> frozenset[str]:
    aliases = set(withheld)
    for node in hidden:
        aliases.add(node.title)
        aliases.add(node.path)
        aliases.add(node.path.split("/")[-1])
        aliases.add(node.path.split("/")[-1].removesuffix(".md"))
        if node.path.endswith(".md"):
            aliases.add(node.path[:-3])
    return frozenset(aliases)


def _envelope(principal: Mapping, scopes_used: tuple[str, ...], items: list[dict]) -> dict:
    return {
        "items": items,
        "capability_receipt": {
            "principal_id": principal["principal_id"],
            "workspace_id": principal["workspace_id"],
            "scopes_used": list(scopes_used),
            "pass_as": "handle",
            "policy_version": POLICY_VERSION,
        },
    }


def _string_tuple(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) and item for item in value)
