"""Pure text edits for `daily/` and `brand/posts/` notes.

No git, no config, no clock: everything here is `str -> str`, which is the only way a
writeback into someone's private vault can be reviewed honestly.

What the night is allowed to touch, and nothing else:

- the checkbox state and the classification comment of frozen checklist lines;
- one managed block per concern (unplanned work, Evolução, tomorrow's proposal), each
  delimited by its own markers and **replaced** rather than appended, so a second
  revision of the same day rewrites its own block instead of stacking duplicates;
- the `swarm_*` scalars in the daily frontmatter.

Everything else — the owner's prose, `external:` pointers, other frontmatter keys,
sections the night knows nothing about — is copied through byte for byte. Post notes
are the one exception: their frontmatter is round-tripped through YAML because it is
machine-written data, and `guided` is never overwritten once it exists.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import yaml

from swarm_reports.metrics.daily import FROZEN_BEGIN, FROZEN_END, META_COMMENT

HOJE_HEADING = re.compile(r"^##\s+Hoje\s*$", re.MULTILINE | re.IGNORECASE)
EVOLUTION_HEADING = re.compile(r"^##\s+Evolu[çc][ãa]o\s*$", re.MULTILINE | re.IGNORECASE)
TOMORROW_HEADING = re.compile(r"^##\s+Amanh[ãa].*$", re.MULTILINE | re.IGNORECASE)

CHECKLIST_LINE = re.compile(r"^(?P<indent>\s*)- \[(?P<mark>[ xX])\](?P<rest>\s+.*)$")
CLASS_COMMENT = re.compile(r"\s*<!--\s*swarm:class\s+[^>]*-->", re.IGNORECASE)

UNPLANNED_BEGIN = re.compile(r"<!--\s*swarm:unplanned-begin[^>]*-->", re.IGNORECASE)
UNPLANNED_END = re.compile(r"<!--\s*swarm:unplanned-end\s*-->", re.IGNORECASE)
EVOLUTION_BEGIN = re.compile(r"<!--\s*swarm:evolution-begin[^>]*-->", re.IGNORECASE)
EVOLUTION_END = re.compile(r"<!--\s*swarm:evolution-end\s*-->", re.IGNORECASE)
TOMORROW_BEGIN = re.compile(r"<!--\s*swarm:tomorrow-begin[^>]*-->", re.IGNORECASE)
TOMORROW_END = re.compile(r"<!--\s*swarm:tomorrow-end\s*-->", re.IGNORECASE)

#: Post notes are named from the URL, so the same spontaneous post always lands on the
#: same path however many times the owner reports it.
POST_SLUG_LENGTH = 12


@dataclass(frozen=True)
class UnplannedRecord:
    task_id: str
    text: str
    done: bool
    classification: str | None
    first_planned: date


@dataclass(frozen=True)
class TomorrowItem:
    task_id: str
    text: str
    is_p0: bool = False
    source_pointer: str = ""


# --------------------------------------------------------------------------- daily


def update_daily_note(
    text: str,
    *,
    day: date,
    revision: int,
    done_ids: set[str],
    classifications: dict[str, str | None],
    unplanned: list[UnplannedRecord],
    evolution_lines: list[str],
    tomorrow: list[TomorrowItem],
    tomorrow_day: date,
) -> str:
    frontmatter_raw, body = _split_frontmatter_raw(text)

    body = _apply_checklist(body, done_ids=done_ids, classifications=classifications)
    body = _apply_unplanned(body, day=day, revision=revision, unplanned=unplanned)
    body = _apply_evolution(body, day=day, revision=revision, lines=evolution_lines)
    body = _apply_tomorrow(body, revision=revision, items=tomorrow, tomorrow_day=tomorrow_day)

    frontmatter_raw = _set_frontmatter_scalars(
        frontmatter_raw,
        {
            "swarm_evening_validated": True,
            "swarm_evening_revision": revision,
            "swarm_evening_day": day.isoformat(),
        },
    )
    return _join_frontmatter(frontmatter_raw, body)


def _apply_checklist(
    body: str,
    *,
    done_ids: set[str],
    classifications: dict[str, str | None],
) -> str:
    """Tick the frozen lines the owner validated; leave every other line alone."""
    lines = body.splitlines(keepends=True)
    hoje_start, hoje_end = _section_bounds(body, HOJE_HEADING)
    if hoje_start is None:
        return body

    out: list[str] = []
    pending_id: str | None = None
    inside_frozen = False
    offset = 0
    for raw in lines:
        line_start = offset
        offset += len(raw)
        stripped = raw.rstrip("\n")
        in_hoje = hoje_start <= line_start < (hoje_end if hoje_end is not None else len(body))

        if not in_hoje:
            out.append(raw)
            continue

        if FROZEN_BEGIN.search(stripped):
            inside_frozen = True
            out.append(raw)
            continue
        if FROZEN_END.search(stripped):
            inside_frozen = False
            out.append(raw)
            continue

        meta = META_COMMENT.search(stripped)
        match = CHECKLIST_LINE.match(stripped)
        if meta and match is None:
            pending_id = _meta_id(meta.group("kv"))
            out.append(raw)
            continue

        if match is None or not inside_frozen:
            out.append(raw)
            continue

        task_id = pending_id or _meta_id_from_line(stripped)
        pending_id = None
        if task_id is None:
            out.append(raw)
            continue

        newline = "\n" if raw.endswith("\n") else ""
        mark = "x" if task_id in done_ids else " "
        rest = match.group("rest")
        if task_id in classifications:
            rest = CLASS_COMMENT.sub("", rest).rstrip()
            label = classifications[task_id]
            if label:
                rest = f"{rest} <!-- swarm:class {label} -->"
        out.append(f"{match.group('indent')}- [{mark}]{rest}{newline}")

    return "".join(out)


def _apply_unplanned(
    body: str,
    *,
    day: date,
    revision: int,
    unplanned: list[UnplannedRecord],
) -> str:
    """Out-of-plan work, in its own block after the frozen checklist.

    It must stay outside the frozen markers and carry `added-after-freeze`, because the
    denominator is the freeze and nothing the evening reports may enter it.
    """
    if not unplanned:
        return _drop_block(body, UNPLANNED_BEGIN, UNPLANNED_END)

    lines = [f"<!-- swarm:unplanned-begin day={day.isoformat()} revision={revision} -->"]
    lines.append("")
    lines.append("**Fora do plano**")
    lines.append("")
    for item in unplanned:
        lines.append("<!-- swarm:added-after-freeze -->")
        lines.append(
            f"<!-- swarm:task-meta id={item.task_id} "
            f"first_planned={item.first_planned.isoformat()} frozen=false -->"
        )
        mark = "x" if item.done else " "
        suffix = f" <!-- swarm:class {item.classification} -->" if item.classification else ""
        lines.append(f"- [{mark}] {item.text}{suffix}")
    lines.append("<!-- swarm:unplanned-end -->")
    block = "\n".join(lines) + "\n"

    replaced = _replace_block(body, UNPLANNED_BEGIN, UNPLANNED_END, block)
    if replaced is not None:
        return replaced

    anchor = _find_line_end(body, FROZEN_END)
    if anchor is None:
        return _append_to_section(body, HOJE_HEADING, block, "## Hoje")
    return body[:anchor] + "\n" + block + body[anchor:]


def _apply_evolution(body: str, *, day: date, revision: int, lines: list[str]) -> str:
    block_lines = [f"<!-- swarm:evolution-begin day={day.isoformat()} revision={revision} -->"]
    block_lines.extend(lines)
    block_lines.append("<!-- swarm:evolution-end -->")
    block = "\n".join(block_lines) + "\n"
    replaced = _replace_block(body, EVOLUTION_BEGIN, EVOLUTION_END, block)
    if replaced is not None:
        return replaced
    return _append_to_section(body, EVOLUTION_HEADING, block, "## Evolução")


def _apply_tomorrow(
    body: str,
    *,
    revision: int,
    items: list[TomorrowItem],
    tomorrow_day: date,
) -> str:
    """Tomorrow's proposal, in *today's* note.

    It is a proposal, not a freeze: the next morning refreshes it against live Linear and
    Calendar and freezes whatever it finds then. Writing it into `daily/<tomorrow>.md`
    would put unchecked `- [ ]` lines under that note's `## Hoje` before its own freeze
    ran, and the parser would read them as a checklist that nobody committed to.
    """
    if not items:
        return _drop_block(body, TOMORROW_BEGIN, TOMORROW_END)
    block_lines = [
        f"<!-- swarm:tomorrow-begin day={tomorrow_day.isoformat()} revision={revision} -->"
    ]
    for item in items:
        prefix = "**P0** " if item.is_p0 else ""
        pointer = f" <!-- swarm:source {item.source_pointer} -->" if item.source_pointer else ""
        block_lines.append(
            f"<!-- swarm:proposed id={item.task_id} -->\n- [ ] {prefix}{item.text}{pointer}"
        )
    block_lines.append("<!-- swarm:tomorrow-end -->")
    block = "\n".join(block_lines) + "\n"
    replaced = _replace_block(body, TOMORROW_BEGIN, TOMORROW_END, block)
    if replaced is not None:
        return replaced
    return _append_to_section(body, TOMORROW_HEADING, block, "## Amanhã (opcional)")


# ---------------------------------------------------------------------------- posts


def post_note_filename(url: str, posted_on: date) -> str:
    digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:POST_SLUG_LENGTH]
    return f"{posted_on.isoformat()}-{digest}.md"


def render_new_post_note(
    *,
    url: str,
    platform: str,
    posted_on: date,
    guided: bool,
    checkpoint: str,
    metrics: dict[str, Any] | None,
) -> str:
    """A spontaneous post the owner reported in the evening form.

    `guided: false` is the whole point of the note: without it the post is countable but
    not attributable, and the RES split silently loses a data point.
    """
    front: dict[str, Any] = {
        "guided": guided,
        "platform": platform,
        "url": url,
        "posted_on": posted_on.isoformat(),
        "checkpoints": [
            {"at": "48h", "due": _plus_days(posted_on, 2)},
            {"at": "7d", "due": _plus_days(posted_on, 7)},
        ],
    }
    front = _merge_metrics(front, checkpoint=checkpoint, metrics=metrics)
    body = (
        f"# Post {posted_on.isoformat()}\n\n"
        f"Registrado pela sessão da noite a partir do formulário. "
        f"`guided: false` significa que não saiu de um draft do ciclo.\n\n"
        f"external: {url}\n"
    )
    return _join_frontmatter(_dump_frontmatter(front), body)


def update_post_note(
    text: str,
    *,
    checkpoint: str,
    metrics: dict[str, Any] | None,
    url: str | None = None,
    platform: str | None = None,
) -> str:
    """Merge reported numbers into an existing post note.

    Absent is not zero. A key the owner did not fill in is simply not written, so the
    difference between "the post got 0 comments" and "I did not look" survives into the
    RES computation instead of being flattened on the way in.
    """
    front_raw, body = _split_frontmatter_raw(text)
    front = yaml.safe_load(front_raw) if front_raw.strip() else {}
    if not isinstance(front, dict):
        raise ValueError("post note frontmatter must be a mapping")
    if url and not front.get("url"):
        front["url"] = url
    if platform and not front.get("platform"):
        front["platform"] = platform
    # `guided` is deliberately absent from this list: the note's own answer wins forever.
    front = _merge_metrics(front, checkpoint=checkpoint, metrics=metrics)
    return _join_frontmatter(_dump_frontmatter(front), body)


def _merge_metrics(
    front: dict[str, Any],
    *,
    checkpoint: str,
    metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    clean = _clean_metrics(metrics)
    if clean is None:
        return front
    if checkpoint == "launch":
        front["metrics"] = _merge_metric_blob(front.get("metrics"), clean)
        return front

    raw = front.get("checkpoints")
    entries = [dict(e) for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    for entry in entries:
        if str(entry.get("at") or entry.get("label") or "") == checkpoint:
            entry["metrics"] = _merge_metric_blob(entry.get("metrics"), clean)
            break
    else:
        entries.append({"at": checkpoint, "metrics": clean})
    front["checkpoints"] = entries
    return front


def _merge_metric_blob(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in incoming.items():
        if key == "signals":
            signals = dict(merged.get("signals") or {})
            signals.update(value)
            merged["signals"] = signals
        else:
            merged[key] = value
    return merged


def _clean_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop the unknowns; keep the zeros. Returns None when nothing was reported."""
    if not metrics:
        return None
    out: dict[str, Any] = {}
    for key in ("reach", "outside_fraction"):
        value = metrics.get(key)
        if value is not None:
            out[key] = _plain_number(value)
    signals = {
        str(name): _plain_number(value)
        for name, value in (metrics.get("signals") or {}).items()
        if value is not None
    }
    if signals:
        out["signals"] = signals
    return out or None


def _plain_number(value: Any) -> Any:
    number = float(value)
    return int(number) if number.is_integer() else number


def _plus_days(day: date, days: int) -> str:
    return (day + timedelta(days=days)).isoformat()


# ------------------------------------------------------------------------ internals


def _split_frontmatter_raw(text: str) -> tuple[str, str]:
    """Frontmatter as raw text, so unknown keys survive verbatim."""
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return "", text


def _join_frontmatter(front: str, body: str) -> str:
    if not front.strip():
        return body
    if not front.endswith("\n"):
        front += "\n"
    return f"---\n{front}---\n{body}"


def _dump_frontmatter(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _set_frontmatter_scalars(front: str, updates: dict[str, Any]) -> str:
    """Set top-level scalars by line, leaving nested blocks (`external:`) untouched."""
    lines = front.splitlines() if front.strip() else []
    for key, value in updates.items():
        rendered = f"{key}: {_yaml_scalar(value)}"
        pattern = re.compile(rf"^{re.escape(key)}\s*:", re.IGNORECASE)
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = rendered
                break
        else:
            lines.append(rendered)
    return "\n".join(lines) + "\n" if lines else ""


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return yaml.safe_dump(value, default_flow_style=True).strip().rstrip("\n...").strip()


def _meta_id(blob: str) -> str | None:
    for token in blob.split():
        if token.startswith("id="):
            return token[3:].strip().strip('"').strip("'")
    return None


def _meta_id_from_line(line: str) -> str | None:
    match = META_COMMENT.search(line)
    if not match:
        return None
    return _meta_id(match.group("kv"))


def _section_bounds(body: str, heading: re.Pattern[str]) -> tuple[int | None, int | None]:
    match = heading.search(body)
    if not match:
        return None, None
    start = match.end()
    nxt = re.compile(r"^##\s+", re.MULTILINE).search(body, start)
    return start, (nxt.start() if nxt else None)


def _find_line_end(body: str, marker: re.Pattern[str]) -> int | None:
    match = marker.search(body)
    if not match:
        return None
    end = body.find("\n", match.end())
    return len(body) if end == -1 else end + 1


def _replace_block(
    body: str,
    begin: re.Pattern[str],
    end: re.Pattern[str],
    block: str,
) -> str | None:
    start_match = begin.search(body)
    if not start_match:
        return None
    end_match = end.search(body, start_match.end())
    if not end_match:
        return None
    tail = body.find("\n", end_match.end())
    stop = len(body) if tail == -1 else tail + 1
    return body[: start_match.start()] + block + body[stop:]


def _drop_block(body: str, begin: re.Pattern[str], end: re.Pattern[str]) -> str:
    replaced = _replace_block(body, begin, end, "")
    return body if replaced is None else replaced


def _append_to_section(body: str, heading: re.Pattern[str], block: str, title: str) -> str:
    match = heading.search(body)
    if not match:
        if body and not body.endswith("\n"):
            body += "\n"
        return f"{body}\n{title}\n\n{block}"
    _start, end = _section_bounds(body, heading)
    if end is None:
        if body and not body.endswith("\n"):
            body += "\n"
        return body + "\n" + block
    prefix = body[:end]
    if not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block + "\n" + body[end:]
