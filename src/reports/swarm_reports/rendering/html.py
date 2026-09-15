"""Mobile morning HTML (375px, inline CSS/JS only, no network dependency).

One rule governs the script: `readForm()` is the single producer of the evening
payload, and both "Copiar prompt" and "Enviar" call it. Two builders would drift, and
then the copy-prompt path — the fallback for when the VPS is down — would quietly
submit something different from the form.

`client_saved_at` is attached to the POST only. The clipboard text is the canonical
content, so it stays byte-identical to what the server hashes, and the draft in
`localStorage` is versioned by revision number rather than by clock, so restoring a
draft can never lose an edit to a timestamp comparison.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from swarm_reports.evening_schema import (
    POST_CHECKPOINTS,
    POST_PLATFORMS,
    SCHEMA_VERSION,
    EveningPayload,
    evening_payload_json_schema,
)
from swarm_reports.plan import MorningPlan
from swarm_reports.rendering.assets import MOBILE_CSS

CLASSIFICATION_OPTIONS = ("", "oportunidade", "procrastinação", "devaneio")


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
    #: Task ids already validated as done, so a reopened report shows real state.
    done_task_ids: list[str] = field(default_factory=list)
    post_url: str | None = None
    revision_url: str | None = None
    deferred_handoffs: list[tuple[str, str, str]] = field(default_factory=list)
    digest_cards: list[dict[str, str]] = field(default_factory=list)
    dispatch_note: str = ""
    outside_items: list[dict] = field(default_factory=list)


def escape_html(text: str) -> str:
    return html.escape(text, quote=True)


def json_for_script(data: Any) -> str:
    """Serialize JSON safe inside `<script>`: blocks `</script>` and JS line breaks."""
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _options(values, selected: str = "") -> str:
    out = []
    for value in values:
        label = value or "—"
        mark = ' selected="selected"' if value == selected else ""
        out.append(f'<option value="{escape_html(value)}"{mark}>{escape_html(label)}</option>')
    return "".join(out)


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
    panels = [_panel_hoje(model), _panel_revisar(model)]
    if "Lição" in tabs:
        panels.append(_panel_lesson(model))
    panels.append(_panel_noite(model))
    panel_html = "".join(
        f'<section class="panel{" active" if i == 0 else ""}" data-panel="{escape_html(name)}">'
        f"{body}</section>"
        for i, (name, body) in enumerate(zip(tabs, panels, strict=True))
    )

    seed_json = json_for_script(model.evening_seed.content_json())
    schema_json = json_for_script(evening_payload_json_schema())

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
  <script id="evening-schema" type="application/json">{schema_json}</script>
  <script>
  (function() {{
    "use strict";
    const OWNER = {json_for_script(model.owner_id)};
    const DAY = {json_for_script(model.day.isoformat())};
    const SCHEMA_VERSION = {SCHEMA_VERSION};
    const SEED = {seed_json};
    const POST_URL = {json_for_script(model.post_url or "")};
    const REVISION_URL = {json_for_script(model.revision_url or "")};
    const STORAGE_KEY = "evening:" + OWNER + ":" + DAY;

    function q(sel, root) {{ return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }}
    function byId(id) {{ return document.getElementById(id); }}
    function status(msg) {{ const el = byId("evening-status"); if (el) el.textContent = msg; }}

    function selectTab(name) {{
      q(".tab").forEach(function(btn) {{
        btn.setAttribute("aria-selected", btn.dataset.tab === name ? "true" : "false");
      }});
      q(".panel").forEach(function(panel) {{
        panel.classList.toggle("active", panel.dataset.panel === name);
      }});
    }}
    q(".tab").forEach(function(btn) {{
      btn.addEventListener("click", function() {{ selectTab(btn.dataset.tab); }});
    }});

    // Same key order the server uses, so the clipboard text is diffable against it.
    function canonical(value) {{
      if (value === null || typeof value !== "object") return JSON.stringify(value);
      if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
      const keys = Object.keys(value).sort();
      return "{{" + keys.map(function(k) {{
        return JSON.stringify(k) + ":" + canonical(value[k]);
      }}).join(",") + "}}";
    }}

    function numberOrNull(el) {{
      if (!el || el.value.trim() === "") return null;
      const n = Number(el.value);
      return Number.isFinite(n) ? n : null;
    }}
    function classOrNull(el) {{
      if (!el || !el.value) return null;
      return el.value;
    }}

    function unplannedRows() {{
      return q("[data-unplanned-row]").map(function(row) {{
        const text = row.querySelector("[data-unplanned-text]");
        if (!text || text.value.trim() === "") return null;
        const item = {{
          text: text.value.trim(),
          done: !!(row.querySelector("[data-unplanned-done]") || {{}}).checked,
          classification: classOrNull(row.querySelector("[data-unplanned-class]"))
        }};
        const id = row.dataset.unplannedRow;
        if (id) item.task_id = id;
        return item;
      }}).filter(Boolean);
    }}

    function postRows() {{
      return q("[data-post-row]").map(function(row) {{
        const url = row.querySelector("[data-post-url]");
        if (!url || url.value.trim() === "") return null;
        const signals = {{}};
        q("[data-post-signal]", row).forEach(function(el) {{
          const v = numberOrNull(el);
          if (v !== null) signals[el.dataset.postSignal] = v;
        }});
        return {{
          url: url.value.trim(),
          platform: row.querySelector("[data-post-platform]").value,
          guided: !!row.querySelector("[data-post-guided]").checked,
          checkpoint: row.querySelector("[data-post-checkpoint]").value,
          metrics: {{
            reach: numberOrNull(row.querySelector("[data-post-reach]")),
            outside_fraction: numberOrNull(row.querySelector("[data-post-outside]")),
            signals: signals
          }}
        }};
      }}).filter(Boolean);
    }}

    function reviewActions() {{
      return q("[data-review-row]").map(function(row) {{
        const picked = row.querySelector("[data-review-action]:checked");
        if (!picked) return null;
        const content = row.querySelector("[data-review-content]");
        const action = picked.value;
        const item = {{ draft_id: row.dataset.reviewRow, action: action }};
        if (content && content.value.trim() !== "") item.content = content.value;
        return item;
      }}).filter(Boolean);
    }}

    // The one and only payload builder.
    function readForm() {{
      return {{
        schema_version: SCHEMA_VERSION,
        day: DAY,
        owner_id: OWNER,
        checklist: q("[data-check-row]").map(function(row) {{
          return {{
            task_id: row.dataset.checkRow,
            done: !!(row.querySelector("[data-check-done]") || {{}}).checked,
            classification: classOrNull(row.querySelector("[data-check-class]"))
          }};
        }}),
        unplanned: unplannedRows(),
        posts: postRows(),
        ledger_classifications: q("[data-ledger-row]").map(function(row) {{
          return {{
            entry_id: row.dataset.ledgerRow,
            classification: classOrNull(row.querySelector("[data-ledger-class]"))
          }};
        }}),
        review_actions: reviewActions(),
        pending_checkpoint_ids: SEED.pending_checkpoint_ids || [],
        notes: (byId("evening-notes") || {{ value: "" }}).value
      }};
    }}

    function applyPayload(payload) {{
      if (!payload) return;
      (payload.checklist || []).forEach(function(item) {{
        const row = document.querySelector('[data-check-row="' + CSS.escape(item.task_id) + '"]');
        if (!row) return;
        const done = row.querySelector("[data-check-done]");
        if (done) done.checked = !!item.done;
        const cls = row.querySelector("[data-check-class]");
        if (cls) cls.value = item.classification || "";
      }});
      (payload.ledger_classifications || []).forEach(function(item) {{
        const row = document.querySelector('[data-ledger-row="' + CSS.escape(item.entry_id) + '"]');
        if (!row) return;
        const cls = row.querySelector("[data-ledger-class]");
        if (cls) cls.value = item.classification || "";
      }});
      (payload.review_actions || []).forEach(function(item) {{
        const row = document.querySelector('[data-review-row="' + CSS.escape(item.draft_id) + '"]');
        if (!row) return;
        const picked = row.querySelector('[data-review-action][value="' + item.action + '"]');
        if (picked) picked.checked = true;
        const content = row.querySelector("[data-review-content]");
        if (content && item.content) content.value = item.content;
      }});
      const notes = byId("evening-notes");
      if (notes) notes.value = payload.notes || "";
      byId("unplanned-rows").replaceChildren();
      const outsideIds = new Set((payload.unplanned || []).map(function(x) {{ return x.task_id; }}));
      (payload.unplanned || []).concat((SEED.unplanned || []).filter(function(x) {{ return !outsideIds.has(x.task_id); }}))
        .forEach(function(item) {{ addUnplanned(item); }});
      byId("post-rows").replaceChildren();
      (payload.posts || []).forEach(function(item) {{ addPost(item); }});
    }}

    // Draft state is keyed by revision, never by clock.
    function saveLocal(revision, payload) {{
      try {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify({{ revision: revision, payload: payload }}));
      }} catch (err) {{
        status("Rascunho não pôde ser salvo neste navegador.");
      }}
    }}
    function loadLocal() {{
      try {{
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || !parsed.payload) return null;
        return {{ revision: Number(parsed.revision) || 0, payload: parsed.payload }};
      }} catch (err) {{
        return null;
      }}
    }}
    async function loadRemote() {{
      if (!REVISION_URL) return null;
      try {{
        const res = await fetch(REVISION_URL, {{ credentials: "same-origin" }});
        if (!res.ok) return null;
        const body = await res.json();
        if (!body || !body.payload) return null;
        return {{ revision: Number(body.revision) || 0, payload: body.payload }};
      }} catch (err) {{
        return null;
      }}
    }}

    let currentRevision = 0;

    function addUnplanned(item) {{
      const host = byId("unplanned-rows");
      if (!host) return;
      const row = document.createElement("div");
      row.className = "row";
      if (item && item.task_id) row.dataset.unplannedRow = item.task_id;
      else row.dataset.unplannedRow = "";
      row.setAttribute("data-unplanned-row", row.dataset.unplannedRow);
      const text = document.createElement("input");
      text.type = "text";
      text.setAttribute("data-unplanned-text", "1");
      text.placeholder = "o que você fez fora do plano";
      if (item && item.text) text.value = item.text;
      const done = document.createElement("input");
      done.type = "checkbox";
      done.setAttribute("data-unplanned-done", "1");
      if (item && item.done) done.checked = true;
      const cls = document.createElement("select");
      cls.setAttribute("data-unplanned-class", "1");
      cls.innerHTML = {json_for_script(_options(CLASSIFICATION_OPTIONS))};
      if (item && item.classification) cls.value = item.classification;
      const label = document.createElement("label");
      label.appendChild(done);
      label.appendChild(document.createTextNode(" concluído"));
      row.appendChild(text);
      row.appendChild(label);
      row.appendChild(cls);
      host.appendChild(row);
      bindAutosave(row);
    }}

    function addPost(item) {{
      const host = byId("post-rows");
      if (!host) return;
      const row = document.createElement("div");
      row.className = "row";
      row.setAttribute("data-post-row", "1");
      row.innerHTML = {json_for_script(_post_row_template())};
      if (item) {{
        row.querySelector("[data-post-url]").value = item.url || "";
        row.querySelector("[data-post-platform]").value = item.platform || "linkedin";
        row.querySelector("[data-post-guided]").checked = !!item.guided;
        row.querySelector("[data-post-checkpoint]").value = item.checkpoint || "launch";
        const m = item.metrics || {{}};
        if (m.reach !== null && m.reach !== undefined) row.querySelector("[data-post-reach]").value = m.reach;
        if (m.outside_fraction !== null && m.outside_fraction !== undefined) {{
          row.querySelector("[data-post-outside]").value = m.outside_fraction;
        }}
        Object.keys(m.signals || {{}}).forEach(function(name) {{
          const el = row.querySelector('[data-post-signal="' + CSS.escape(name) + '"]');
          if (el) el.value = m.signals[name];
        }});
      }}
      host.appendChild(row);
      bindAutosave(row);
    }}

    function bindAutosave(root) {{
      q("input, textarea, select", root).forEach(function(el) {{
        var persist = function() {{ saveLocal(currentRevision, readForm()); }};
        el.addEventListener("input", persist);
        el.addEventListener("change", persist);
      }});
    }}

    (async function init() {{
      const local = loadLocal();
      currentRevision = local ? local.revision : 0;
      applyPayload(local ? local.payload : SEED);
      bindAutosave(document);
      let editedDuringLoad = false;
      document.addEventListener("input", function() {{ editedDuringLoad = true; }}, {{ once: true }});
      const remote = await loadRemote();
      let chosen = local;
      if (remote && (!local || remote.revision > local.revision)) chosen = remote;
      currentRevision = chosen ? chosen.revision : 0;
      if (!editedDuringLoad) applyPayload(chosen ? chosen.payload : SEED);
      bindAutosave(document);

      const addU = byId("add-unplanned");
      if (addU) addU.addEventListener("click", function() {{ addUnplanned(null); }});
      const addP = byId("add-post");
      if (addP) addP.addEventListener("click", function() {{ addPost(null); }});

      q("[data-question]").forEach(function(button) {{
        button.addEventListener("click", function() {{
          const prompt = button.dataset.question;
          const notes = byId("evening-notes");
          if (!notes.value.includes(prompt)) notes.value += (notes.value ? "\\n" : "") + prompt;
          saveLocal(currentRevision, readForm());
          if (navigator.clipboard) navigator.clipboard.writeText(prompt).catch(function() {{}});
          status("Pergunta registrada nas notas. Envie a noite para sincronizar.");
        }});
      }});
      const copyBtn = byId("copy-prompt");
      if (copyBtn) {{
        copyBtn.addEventListener("click", function() {{
          const text = canonical(readForm());
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(text).then(
              function() {{ status("Payload copiado. Cole na sessão local da noite."); }},
              function() {{ window.prompt("Copiar payload", text); }}
            );
          }} else {{
            window.prompt("Copiar payload", text);
          }}
        }});
      }}

      const sendBtn = byId("send-evening");
      if (sendBtn) {{
        sendBtn.addEventListener("click", async function() {{
          const content = readForm();
          saveLocal(currentRevision, content);
          if (!POST_URL) {{
            status("Sem servidor configurado: use Copiar prompt.");
            return;
          }}
          const body = JSON.stringify(Object.assign({{}}, content, {{
            client_saved_at: new Date().toISOString().replace(/\\.\\d{{3}}Z$/, "Z")
          }}));
          try {{
            const res = await fetch(POST_URL, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: body,
              credentials: "same-origin"
            }});
            if (!res.ok) {{
              status("Envio recusado (" + res.status + "). Rascunho mantido.");
              return;
            }}
            const out = await res.json();
            currentRevision = Number(out.revision) || currentRevision;
            saveLocal(currentRevision, content);
            status(out.changed === false
              ? "Nada mudou: revisão " + currentRevision + " mantida."
              : "Salvo como revisão " + currentRevision + ".");
          }} catch (err) {{
            status("Offline. Rascunho mantido neste aparelho.");
          }}
        }});
      }}
    }})();
  }})();
  </script>
</body>
</html>
"""


def _post_row_template() -> str:
    # Union of the signal names in the wiki weights file; F1/F4 validate them per
    # platform, since the weights are the only place that knows which apply.
    signals = (
        "reactions",
        "likes",
        "comments",
        "reposts",
        "shares",
        "saves",
        "profile_views",
        "profile_visits",
        "link_clicks",
        "followers",
    )
    signal_inputs = "".join(
        f'<label class="sig">{escape_html(name)}'
        f'<input type="number" min="0" step="1" data-post-signal="{escape_html(name)}" /></label>'
        for name in signals
    )
    return (
        '<input type="text" data-post-url placeholder="https://..." />'
        f'<select data-post-platform>{_options(sorted(POST_PLATFORMS), "linkedin")}</select>'
        f'<select data-post-checkpoint>{_options(sorted(POST_CHECKPOINTS), "launch")}</select>'
        '<label><input type="checkbox" data-post-guided /> guiado</label>'
        '<label>alcançados<input type="number" min="0" step="1" data-post-reach /></label>'
        '<label>fora da rede (0–1)'
        '<input type="number" min="0" max="1" step="0.01" data-post-outside /></label>'
        f'<div class="signals">{signal_inputs}</div>'
    )


def _panel_hoje(model: MorningViewModel) -> str:
    plan = model.plan
    parts: list[str] = []

    parts.append('<div class="card"><h3>Desde a última rodada</h3>')
    if plan.ledger:
        for entry in plan.ledger:
            link = ""
            if entry.link:
                safe = escape_html(entry.link)
                link = f' <a href="{safe}" rel="noopener noreferrer">abrir</a>'
            parts.append(
                f'<p class="muted">{escape_html(entry.kind)}: '
                f"{escape_html(entry.summary)}{link}</p>"
            )
    else:
        parts.append('<p class="muted">Nada registrado no ledger.</p>')
    if model.outside_items:
        parts.append('<div class="card"><h3>Desde a última rodada — fora do plano</h3>')
        for item in model.outside_items:
            parts.append(f'<p>{escape_html(item["kind"])}: {escape_html(item["title"])}</p>')
        parts.append('</div>')
    if plan.discovery_placeholder:
        parts.append(f'<p class="muted">{escape_html(plan.discovery_placeholder)}</p>')
    if model.deferred_handoffs:
        parts.append('<p class="muted"><strong>Adiados (quota):</strong></p><ul>')
        for task_id, title, reason in model.deferred_handoffs:
            parts.append(
                f"<li>{escape_html(title)} "
                f'<span class="muted">({escape_html(task_id)}: {escape_html(reason)})</span></li>'
            )
        parts.append("</ul>")
    if model.dispatch_note:
        parts.append(f'<p class="muted">{escape_html(model.dispatch_note)}</p>')
    parts.append("</div>")

    p0 = plan.p0_items
    parts.append('<div class="card"><h3>P0</h3>')
    if p0:
        for item in p0:
            parts.append(
                f'<p><strong>{escape_html(str(item.get("text") or ""))}</strong>'
                f'<br /><span class="muted">id {escape_html(str(item["task_id"]))}</span></p>'
            )
    else:
        parts.append('<p class="muted">Sem P0 hoje.</p>')
    parts.append("</div>")

    parts.append('<div class="card"><h3>Agenda</h3>')
    if plan.agenda:
        for entry in plan.agenda:
            when = f'{escape_html(entry.when)} · ' if entry.when else ""
            parts.append(f'<p class="muted">{when}{escape_html(entry.title)}</p>')
    else:
        parts.append('<p class="muted">Sem eventos.</p>')
    parts.append("</div>")

    parts.append('<div class="card"><h3>Checklist congelado</h3>')
    done = set(model.done_task_ids)
    if plan.checklist:
        for item in plan.checklist:
            tid = str(item["task_id"])
            mark = "[x]" if tid in done else "[ ]"
            tag = " <em>P0</em>" if item.get("is_p0") else ""
            first = escape_html(str(item.get("first_planned") or ""))
            parts.append(
                f'<p>{mark} {escape_html(str(item.get("text") or ""))}{tag}'
                f'<br /><span class="muted">id {escape_html(tid)} · desde {first}</span></p>'
            )
    else:
        parts.append('<p class="muted">Dia congelado vazio (confirmado).</p>')
    parts.append("</div>")

    for card in plan.handoffs:
        links = "".join(
            f'<li><a href="{escape_html(u)}" rel="noopener noreferrer">{escape_html(u)}</a></li>'
            for u in card.context_links
        )
        budget = (
            f'<p class="muted">tempo: {card.time_budget_minutes} min</p>'
            if card.time_budget_minutes is not None
            else ""
        )
        parts.append(
            f'<div class="card"><h3>{escape_html(card.title)}</h3>'
            f"<p>{escape_html(card.objective)}</p>"
            f"<ul>{links}</ul>"
            f'<pre class="muted">{escape_html(card.copy_prompt)}</pre>'
            f'<p class="muted">pronto quando: {escape_html(card.criteria)}</p>'
            f"{budget}</div>"
        )

    for src in plan.sources:
        cls = "muted" if src.confirms_empty else "source-unavailable"
        pointer = f" {escape_html(src.pointer)}" if src.pointer else ""
        parts.append(
            f'<p class="{cls}">{escape_html(src.kind)}: {escape_html(src.status)}{pointer}</p>'
        )
    return "\n".join(parts)


def _panel_revisar(model: MorningViewModel) -> str:
    parts: list[str] = []
    if model.digest_cards:
        parts.append('<div class="card"><h3>Decisões (top 5)</h3>')
        for card in model.digest_cards[:5]:
            question_prompt = f'Questionar {card.get("pr", "")} {card.get("location", "")}: {card.get("question", "")} Por que importa: {card.get("why", "")}'
            parts.append(
                f'<p><strong>{escape_html(card.get("kind", ""))}</strong> '
                f'{escape_html(card.get("location", ""))}<br />'
                f'{escape_html(card.get("question", ""))}<br />'
                f'<span class="muted">{escape_html(card.get("why", ""))}</span> '
                f'<button type="button" data-question="{escape_html(question_prompt)}">Questionar</button></p>'
            )
        parts.append("</div>")
    else:
        parts.append('<p class="muted">Nenhum cartão de digest pendente.</p>')
    drafts = list(model.plan.review_drafts)
    if model.plan.optional_post_draft:
        drafts.append(model.plan.optional_post_draft)
    for draft in drafts:
        did = escape_html(draft.draft_id)
        name = f"review-{did}"
        radios = "".join(
            f'<label><input type="radio" name="{name}" data-review-action '
            f'value="{action}" /> {label}</label>'
            for action, label in (
                ("ready", "pronto"),
                ("edit", "editar"),
                ("reject", "rejeitar"),
            )
        )
        parts.append(
            f'<div class="card" data-review-row="{did}"><h3>{escape_html(draft.kind)}</h3>'
            f'<p class="muted">motivo: {escape_html(draft.reason)}</p>'
            f'<textarea rows="5" data-review-content>{escape_html(draft.content)}</textarea>'
            f'<div class="actions">{radios}</div>'
            '<p class="muted">pronto ≠ publicado: a publicação é sempre do dono.</p>'
            "</div>"
        )
    if not drafts:
        parts.append('<p class="muted">Nenhum draft pendente.</p>')
    if model.evening_seed.pending_checkpoint_ids:
        items = "".join(
            f"<li>{escape_html(pid)}</li>" for pid in model.evening_seed.pending_checkpoint_ids
        )
        parts.append(f'<div class="card"><h3>Checkpoints 48h em aberto</h3><ul>{items}</ul></div>')
    return "\n".join(parts)


def _panel_lesson(model: MorningViewModel) -> str:
    lesson = model.plan.lesson
    if not lesson or not lesson.link:
        return '<p class="muted">Sem lição.</p>'
    link = escape_html(lesson.link)
    topic = escape_html(lesson.topic or "")
    return (
        f'<div class="card"><h3>Lição</h3><p>{topic}</p>'
        f'<p><a href="{link}" rel="noopener noreferrer">{link}</a></p>'
        '<p class="muted">O fluxo da lição é do teach-me; o formulário não interage com ela.</p>'
        "</div>"
    )


def _panel_noite(model: MorningViewModel) -> str:
    parts: list[str] = []
    checklist_rows = []
    for item in model.evening_seed.checklist:
        tid = escape_html(item.task_id)
        text = next(
            (
                str(entry.get("text") or "")
                for entry in model.plan.checklist
                if str(entry["task_id"]) == item.task_id
            ),
            item.task_id,
        )
        checklist_rows.append(
            f'<div class="row" data-check-row="{tid}">'
            f'<label><input type="checkbox" data-check-done /> {escape_html(text)}</label>'
            f"<select data-check-class>{_options(CLASSIFICATION_OPTIONS)}</select>"
            "</div>"
        )
    parts.append(
        '<div class="card"><h3>Checklist congelado</h3>'
        + ("".join(checklist_rows) or '<p class="muted">Nada congelado hoje.</p>')
        + "</div>"
    )

    parts.append(
        '<div class="card"><h3>Fora do plano — classificar?</h3>'
        '<p class="muted">Em branco não penaliza.</p>'
        '<div id="unplanned-rows"></div>'
        '<div class="actions"><button type="button" id="add-unplanned">+ atividade</button></div>'
        "</div>"
    )

    if model.evening_seed.ledger_classifications:
        rows = "".join(
            f'<div class="row" data-ledger-row="{escape_html(entry.entry_id)}">'
            f'<span class="muted">{escape_html(entry.entry_id)}</span>'
            f"<select data-ledger-class>{_options(CLASSIFICATION_OPTIONS)}</select></div>"
            for entry in model.evening_seed.ledger_classifications
        )
        parts.append(f'<div class="card"><h3>Ações autônomas</h3>{rows}</div>')

    parts.append(
        '<div class="card"><h3>Números de post</h3>'
        '<div id="post-rows"></div>'
        '<div class="actions"><button type="button" id="add-post">+ post</button></div>'
        "</div>"
    )

    parts.append(
        '<div class="card"><label>Notas'
        '<textarea id="evening-notes" rows="4"></textarea></label>'
        '<div class="actions">'
        '<button type="button" id="copy-prompt">Copiar prompt</button>'
        '<button type="button" id="send-evening">Enviar</button>'
        "</div>"
        '<p class="muted" id="evening-status" role="status"></p>'
        "</div>"
    )
    return "\n".join(parts)
