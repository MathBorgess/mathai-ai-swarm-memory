"""Strict propose input, closed path prefix, and T2 note rendering.

Caller JSON cannot choose path, branch, repo, or template. User strings are
escaped into YAML/Markdown as data; URLs are not fetched or executed.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

NAMESPACES = {"pesquisa.tcc": "pesquisa/tcc"}
INBOX = "inbox"
MAX_MARKDOWN = 12288
MAX_TITLE = 200
MAX_SOURCES = 32
MAX_LABEL = 200
MAX_URL = 2000
PROPOSAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
BRANCH_PREFIX = "swarm/proposal-"
CLOSED_PREFIXES = tuple(f"{root}/{INBOX}" for root in NAMESPACES.values())
BLOCKED_PARTS = frozenset({"wiki", "fontes", ".github", "scripts", "AGENTS.md"})


class ProposalPayloadError(ValueError):
    """Invalid propose JSON."""


class ProposalTooLarge(ValueError):
    """Markdown exceeds the 12 KiB cap."""


@dataclass(frozen=True)
class Source:
    url: str
    label: str


@dataclass(frozen=True)
class ProposalInput:
    namespace: str
    title: str
    body_markdown: str
    sources: tuple[Source, ...]

    def payload_hash(self) -> str:
        blob = json.dumps(
            {
                "body_markdown": self.body_markdown,
                "namespace": self.namespace,
                "sources": [{"label": s.label, "url": s.url} for s in self.sources],
                "title": self.title,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def propose_scope(self) -> str:
        return f"ctx:propose:{self.namespace}"


def parse_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProposalPayloadError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ProposalPayloadError("Invalid JSON") from exc
    except json.JSONDecodeError as exc:
        raise ProposalPayloadError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProposalPayloadError("Expected JSON object")
    return value


def parse_proposal(data: dict) -> ProposalInput:
    if set(data) != {"namespace", "title", "body_markdown", "sources"}:
        raise ProposalPayloadError("Unexpected proposal fields")
    namespace, title, body, sources = data["namespace"], data["title"], data["body_markdown"], data["sources"]
    if namespace not in NAMESPACES or not isinstance(title, str) or not isinstance(body, str):
        raise ProposalPayloadError("Invalid proposal fields")
    if not title.strip() or len(title) > MAX_TITLE or "\n" in title or "\r" in title or "\x00" in title:
        raise ProposalPayloadError("Invalid title")
    if "\x00" in body:
        raise ProposalPayloadError("Invalid body")
    if len(body.encode()) > MAX_MARKDOWN:
        raise ProposalTooLarge("Proposal markdown too large")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ProposalPayloadError("Invalid sources")
    parsed = []
    for item in sources:
        if not isinstance(item, dict) or set(item) != {"url", "label"}:
            raise ProposalPayloadError("Invalid source")
        url, label = item["url"], item["label"]
        if not isinstance(url, str) or not isinstance(label, str):
            raise ProposalPayloadError("Invalid source")
        if not url.strip() or not label.strip() or len(url) > MAX_URL or len(label) > MAX_LABEL:
            raise ProposalPayloadError("Invalid source")
        if "\n" in url or "\r" in url or "\x00" in url or "\n" in label or "\r" in label:
            raise ProposalPayloadError("Invalid source")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise ProposalPayloadError("Invalid source URL")
        parsed.append(Source(url=url, label=label.strip()))
    return ProposalInput(namespace=namespace, title=title.strip(), body_markdown=body, sources=tuple(parsed))


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def proposal_id_ok(proposal_id: str) -> bool:
    return bool(PROPOSAL_ID.fullmatch(proposal_id))


def derive_path(namespace: str, proposal_id: str, at: datetime) -> str:
    if namespace not in NAMESPACES or not proposal_id_ok(proposal_id):
        raise ProposalPayloadError("Cannot derive proposal path")
    root = NAMESPACES[namespace]
    path = f"{root}/{INBOX}/{at.date().isoformat()}-{proposal_id}.md"
    return assert_write_path(path)


def derive_branch(proposal_id: str) -> str:
    if not proposal_id_ok(proposal_id):
        raise ProposalPayloadError("Cannot derive proposal branch")
    return f"{BRANCH_PREFIX}{proposal_id}"


def assert_write_path(path: str) -> str:
    if not isinstance(path, str) or "\\" in path or "\x00" in path or path != path.strip():
        raise ProposalPayloadError("Forbidden proposal path")
    normalized = posixpath.normpath(path)
    if normalized != path or path.startswith("/") or path.startswith("../") or "/../" in path:
        raise ProposalPayloadError("Forbidden proposal path")
    parts = path.split("/")
    if any(part in {".", "..", ""} or part in BLOCKED_PARTS for part in parts):
        raise ProposalPayloadError("Forbidden proposal path")
    if not any(path.startswith(prefix + "/") for prefix in CLOSED_PREFIXES):
        raise ProposalPayloadError("Forbidden proposal path")
    if not path.endswith(".md"):
        raise ProposalPayloadError("Forbidden proposal path")
    return path


def assert_branch(branch: str) -> str:
    if not branch.startswith(BRANCH_PREFIX) or not proposal_id_ok(branch[len(BRANCH_PREFIX):]):
        raise ProposalPayloadError("Forbidden proposal branch")
    return branch


def render_note(*, proposal_id: str, principal_id: str, workspace_id: str, created_at: datetime,
                payload: ProposalInput) -> str:
    sources = "\n".join(
        f"- {yaml_string(item.label)}: {yaml_string(item.url)}" for item in payload.sources
    ) or "- (none)"
    return "\n".join((
        "---",
        "layer: T2",
        "status: human_confirmation_pending",
        "promotes_to_wiki: false",
        f"proposal_id: {yaml_string(proposal_id)}",
        f"principal_id: {yaml_string(principal_id)}",
        f"workspace_id: {yaml_string(workspace_id)}",
        f"namespace: {yaml_string(payload.namespace)}",
        f"created_at: {yaml_string(created_at.date().isoformat())}",
        f"title: {yaml_string(payload.title)}",
        "claims: unverified_suggestions",
        "---",
        "",
        "# Cartão de revisão T2",
        "",
        "Afirmações abaixo são sugestões não verificadas. Decisão humana pendente.",
        "Esta nota não promove conteúdo para wiki T1/T0, `fontes/` ou `main`.",
        "Sem merge automático, workflow executável ou scheduler.",
        "",
        f"Origem: principal {yaml_string(principal_id)} no workspace {yaml_string(workspace_id)}.",
        "",
        "## Título proposto",
        "",
        yaml_string(payload.title),
        "",
        "## Afirmações propostas (não verificadas)",
        "",
        payload.body_markdown,
        "",
        "## Fontes (dados; não buscadas nem executadas)",
        "",
        sources,
        "",
        "## Decisão humana",
        "",
        "- [ ] Pendente",
        "",
    ))
