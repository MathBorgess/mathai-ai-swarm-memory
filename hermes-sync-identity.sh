#!/usr/bin/env bash
set -euo pipefail
REPO_SLUG="${HERMES_IDENTITY_REPO:-MathBorgess/hermes-identity}"
CLONE_DIR="${HERMES_IDENTITY_DIR:-$HOME/src/hermes-identity}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CMD="${1:-pull}"
IDENTITY_DIR="$CLONE_DIR/src/hermes-identity"
need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need git
ensure_clone() {
  if [[ ! -e "$CLONE_DIR/.git" ]]; then
    mkdir -p "$(dirname "$CLONE_DIR")"
    if command -v gh >/dev/null; then
      gh repo clone "$REPO_SLUG" "$CLONE_DIR"
    else
      git clone "https://github.com/${REPO_SLUG}.git" "$CLONE_DIR"
    fi
  fi
}
link_one() {
  local target="$1" link="$2"
  mkdir -p "$(dirname "$link")"
  if [[ -L "$link" ]]; then
    [[ "$(readlink "$link")" == "$target" ]] && return 0
    rm -f "$link"
  elif [[ -f "$link" ]]; then
    if [[ ! -s "$target" ]] || grep -q 'Hermes updates this file' "$target" 2>/dev/null; then
      [[ -s "$link" ]] && cp "$link" "$target"
    fi
    rm -f "$link"
  fi
  ln -s "$target" "$link"
  echo "linked $link → $target"
}
do_link() {
  mkdir -p "$HERMES_HOME/memories" "$IDENTITY_DIR/memories"
  link_one "$IDENTITY_DIR/SOUL.md" "$HERMES_HOME/SOUL.md"
  link_one "$IDENTITY_DIR/memories/MEMORY.md" "$HERMES_HOME/memories/MEMORY.md"
  link_one "$IDENTITY_DIR/memories/USER.md" "$HERMES_HOME/memories/USER.md"
}
case "$CMD" in
  pull)
    ensure_clone
    git -C "$CLONE_DIR" pull --ff-only || git -C "$CLONE_DIR" pull --rebase
    do_link
    echo "pull ok: $CLONE_DIR"
    ;;
  push)
    ensure_clone
    do_link
    git -C "$CLONE_DIR" add src/hermes-identity/SOUL.md src/hermes-identity/memories/MEMORY.md src/hermes-identity/memories/USER.md
    if git -C "$CLONE_DIR" diff --cached --quiet; then
      echo "nothing to push"; exit 0
    fi
    git -C "$CLONE_DIR" commit -m "identity: sync $(date -u +%Y-%m-%dT%H:%MZ)"
    git -C "$CLONE_DIR" push
    echo "push ok"
    ;;
  link) ensure_clone; do_link ;;
  *) echo "usage: $0 pull|push|link" >&2; exit 2 ;;
esac
