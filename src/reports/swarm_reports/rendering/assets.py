"""Inline CSS for morning report (no network dependencies)."""

MOBILE_CSS = """
:root {
  color-scheme: light dark;
  font-family: system-ui, -apple-system, sans-serif;
  line-height: 1.4;
}
body {
  margin: 0;
  background: #f6f6f8;
  color: #111;
}
.wrap {
  max-width: 375px;
  margin: 0 auto;
  padding-bottom: 2rem;
}
.metrics {
  position: sticky;
  top: 0;
  z-index: 2;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.35rem;
  padding: 0.5rem;
  background: #fff;
  border-bottom: 1px solid #ddd;
  font-size: 0.8rem;
}
.metric span { display: block; color: #555; }
.metric strong { font-size: 0.95rem; }
.tabs {
  display: flex;
  gap: 0.25rem;
  padding: 0.5rem;
  overflow-x: auto;
}
.tab {
  flex: 1;
  text-align: center;
  padding: 0.4rem;
  border: 1px solid #ccc;
  background: #fff;
  border-radius: 0.35rem;
  font-size: 0.85rem;
}
.tab[aria-selected="true"] {
  border-color: #333;
  font-weight: 600;
}
.panel {
  display: none;
  padding: 0.75rem;
}
.panel.active { display: block; }
.card {
  background: #fff;
  border: 1px solid #e0e0e0;
  border-radius: 0.5rem;
  padding: 0.65rem;
  margin-bottom: 0.5rem;
}
.card h3 { margin: 0 0 0.35rem; font-size: 0.95rem; }
.muted { color: #666; font-size: 0.8rem; }
textarea, input[type="text"], input[type="number"] {
  width: 100%;
  box-sizing: border-box;
  font: inherit;
}
.actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
button {
  font: inherit;
  padding: 0.45rem 0.65rem;
  border-radius: 0.35rem;
  border: 1px solid #444;
  background: #fff;
}
.source-unavailable {
  color: #8a2b2b;
  font-weight: 600;
}
"""
