"""Mobile morning HTML (375px, inline CSS/JS only)."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from swarm_reports.evening_schema import EveningPayload, evening_payload_json_schema
from swarm_reports.plan import MorningPlan
from swarm_reports.rendering.assets import MOBILE_CSS


@dataclass
class MetricTile:
    label: str
    value: str


@dataclass
class MorningViewModel:
    day: date
    owner_id: str
    metrics: list[MetricTile]
    plan: MorningPlan
    evening_seed: EveningPayload
    fetch_revision_url: str | None = None


def escape_html(text: str) -> str:
    return html.escape(text, quote=True)


def json_for_script(data: Any) -> str:
    """Serialize JSON safe inside <script> (blocks </script> breakout)."""
    raw = json.dumps(data, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_morning_html(model: MorningViewModel) -> str:
    tabs = ["Hoje", "Revisar"]
    if model.plan.lesson and model.plan.lesson.link:
        tabs.append("Lição")
    tabs.append("Noite")

    metrics_html = "".join(
        f'<div class="metric"><span>{escape_html(m.label)}</span>'
        f"<strong>{escape_html(m.value)}</strong></div>"
        for m in model.metrics
    )
    tab_buttons = "".join(
        f'<button type="button" class="tab" data-tab="{escape_html(name)}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{escape_html(name)}</button>'
        for i, name in enumerate(tabs)
    )
    panels = [
        _panel_hoje(model),
        _panel_revisar(model),
    ]
    if "Lição" in tabs:
        panels.append(_panel_lesson(model))
    panels.append(_panel_noite(model))

    panel_html = "".join(
        f'<section class="panel{" active" if i == 0 else ""}" data-panel="{escape_html(name)}">'
        f"{body}</section>"
        for i, (name, body) in enumerate(zip(tabs, panels, strict=True))
    )

    seed_json = json_for_script(model.evening_seed.to_json())
    schema_json = json_for_script(evening_payload_json_schema())
    fetch_url = model.fetch_revision_url or ""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Report {escape_html(model.day.isoformat())}</title>
  <style>{MOBILE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="metrics">{metrics_html}</header>
    <nav class="tabs" role="tablist">{tab_buttons}</nav>
    {panel_html}
  </div>
  <script>
  (function() {{
    const owner = {json_for_script(model.owner_id)};
    const day = {json_for_script(model.day.isoformat())};
    const storageKey = "evening:" + owner + ":" + day;
    const seed = {seed_json};
    const schema = {schema_json};
    const fetchUrl = {json_for_script(fetch_url)};

    function selectTab(name) {{
      document.querySelectorAll(".tab").forEach(btn => {{
        btn.setAttribute("aria-selected", btn.dataset.tab === name ? "true" : "false");
      }});
      document.querySelectorAll(".panel").forEach(panel => {{
        panel.classList.toggle("active", panel.dataset.panel === name);
      }});
    }}
    document.querySelectorAll(".tab").forEach(btn => {{
      btn.addEventListener("click", () => selectTab(btn.dataset.tab));
    }});

    function readForm() {{
      const payload = JSON.parse(JSON.stringify(seed));
      document.querySelectorAll("[data-field]").forEach(el => {{
        const path = el.dataset.field.split(".");
        let cur = payload;
        for (let i = 0; i < path.length - 1; i++) cur = cur[path[i]];
        const key = path[path.length - 1];
        if (el.type === "checkbox") cur[key] = el.checked;
        else if (el.type === "number") cur[key] = el.value === "" ? null : Number(el.value);
        else cur[key] = el.value;
      }});
      document.querySelectorAll("[data-checklist-id]").forEach(el => {{
        const id = el.dataset.checklistId;
        const item = payload.checklist.find(c => c.task_id === id);
        if (!item) return;
        if (el.dataset.checklistField === "done") item.done = el.checked;
        if (el.dataset.checklistField === "classification") item.classification = el.value || null;
      }});
      return payload;
    }}

    function saveLocal(payload) {{
      try {{
        localStorage.setItem(storageKey, JSON.stringify(payload));
      }} catch (e) {{
        console.warn("localStorage unavailable", e);
      }}
    }}

    function loadLocal() {{
      try {{
        const raw = localStorage.getItem(storageKey);
        if (!raw) return null;
        return JSON.parse(raw);
      }} catch (e) {{
        return null;
      }}
    }}

    function mergeNewer(local, remote) {{
      if (!remote) return local;
      if (!local) return remote;
      return local._revision >= (remote._revision || 0) ? local : remote;
    }}

    async function maybeFetchRemote() {{
      if (!fetchUrl) return null;
      try {{
        const res = await fetch(fetchUrl, {{ credentials: "same-origin" }});
        if (!res.ok) return null;
        return await res.json();
      }} catch (e) {{
        return null;
      }}
    }}

    function applyPayload(payload) {{
      document.querySelectorAll("[data-checklist-id]").forEach(el => {{
        const id = el.dataset.checklistId;
        const item = payload.checklist.find(c => c.task_id === id);
        if (!item) return;
        if (el.dataset.checklistField === "done") el.checked = !!item.done;
        if (el.dataset.checklistField === "classification") el.value = item.classification || "";
      }});
      const notes = document.querySelector("[data-field='notes']");
      if (notes) notes.value = payload.notes || "";
    }}

    (async function init() {{
      let payload = loadLocal() || seed;
      const remote = await maybeFetchRemote();
      payload = mergeNewer(payload, remote) || payload;
      applyPayload(payload);
      document.querySelectorAll("input, textarea").forEach(el => {{
        el.addEventListener("change", () => saveLocal(readForm()));
      }});
      const copyBtn = document.getElementById("copy-prompt");
      if (copyBtn) {{
        copyBtn.addEventListener("click", () => {{
          const body = JSON.stringify(readForm());
          navigator.clipboard.writeText(body).catch(() => {{
            window.prompt("Copiar payload", body);
          }});
        }});
      }}
      const sendBtn = document.getElementById("send-evening");
      if (sendBtn) {{
        sendBtn.addEventListener("click", () => {{
          const body = JSON.stringify(readForm());
          saveLocal(JSON.parse(body));
          if (!fetchUrl) {{
            alert("Servidor F3 indisponível; payload salvo localmente.");
            return;
          }}
          fetch(fetchUrl, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body,
            credentials: "same-origin",
          }}).then(res => {{
            if (!res.ok) alert("Envio falhou; rascunho mantido localmente.");
            else alert("Enviado.");
          }}).catch(() => alert("Offline; rascunho mantido."));
        }});
      }}
    }})();
  }})();
  </script>
</body>
</html>
"""


def _panel_hoje(model: MorningViewModel) -> str:
    parts = [
        f'<p class="muted">Ledger (F6): {escape_html(model.plan.ledger_placeholder or "—")}</p>',
        f'<p class="muted">Descoberta: {escape_html(model.plan.discovery_placeholder or "—")}</p>',
    ]
    for item in model.plan.p0_items:
        tid = escape_html(str(item["task_id"]))
        text = escape_html(str(item.get("text") or ""))
        parts.append(
            f'<div class="card"><h3>{text}</h3>'
            f'<p class="muted">id {tid}</p>'
            f'<label><input type="checkbox" data-checklist-id="{tid}" '
            f'data-checklist-field="done" /> concluído</label></div>'
        )
    for card in model.plan.handoffs:
        links = "".join(
            f'<li><a href="{escape_html(u)}" rel="noopener noreferrer">{escape_html(u)}</a></li>'
            for u in card.context_links
        )
        parts.append(
            f'<div class="card"><h3>{escape_html(card.title)}</h3>'
            f"<p>{escape_html(card.objective)}</p>"
            f"<ul>{links}</ul>"
            f'<pre class="muted">{escape_html(card.copy_prompt)}</pre>'
            f'<p class="muted">{escape_html(card.criteria)}</p></div>'
        )
    for src in model.plan.sources:
        status = escape_html(src.status)
        cls = "source-unavailable" if src.status == "unavailable" else "muted"
        parts.append(
            f'<p class="{cls}">{escape_html(src.kind)}: {status} '
            f'{escape_html(src.pointer)}</p>'
        )
    return "\n".join(parts)


def _panel_revisar(model: MorningViewModel) -> str:
    parts = ['<p class="muted">Digest top 5 (F5): placeholder</p>']
    for draft in model.plan.review_drafts:
        parts.append(
            f'<div class="card"><h3>{escape_html(draft.kind)}</h3>'
            f"<p>{escape_html(draft.reason)}</p>"
            f'<textarea rows="4" readonly>{escape_html(draft.content)}</textarea>'
            f'<p class="muted">status: {escape_html(draft.status)} (ready ≠ publicado)</p></div>'
        )
    if model.plan.optional_post_draft:
        d = model.plan.optional_post_draft
        parts.append(
            f'<div class="card"><h3>Post opcional</h3>'
            f"<p>{escape_html(d.reason)}</p>"
            f'<textarea rows="4" readonly>{escape_html(d.content)}</textarea></div>'
        )
    parts.append('<p class="muted">T0/T1 pendentes: placeholder</p>')
    return "\n".join(parts)


def _panel_lesson(model: MorningViewModel) -> str:
    lesson = model.plan.lesson
    if not lesson or not lesson.link:
        return "<p class='muted'>Sem lição.</p>"
    link = escape_html(lesson.link)
    topic = escape_html(lesson.topic or "")
    return (
        f'<div class="card"><h3>Lição</h3><p>{topic}</p>'
        f'<p><a href="{link}" rel="noopener noreferrer">{link}</a></p></div>'
    )


def _panel_noite(model: MorningViewModel) -> str:
    checklist_fields = []
    for item in model.evening_seed.checklist:
        tid = escape_html(item.task_id)
        checklist_fields.append(
            f'<label><input type="checkbox" data-checklist-id="{tid}" '
            f'data-checklist-field="done" /> {tid}</label>'
        )
    return (
        "<div class='card'>"
        + "".join(checklist_fields)
        + '<label>Notas<textarea data-field="notes" rows="4"></textarea></label>'
        + '<div class="actions">'
        + '<button type="button" id="copy-prompt">Copiar prompt</button>'
        + '<button type="button" id="send-evening">Enviar</button>'
        + "</div></div>"
    )
