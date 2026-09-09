#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

HERMES_IDENTITY_DIR="$repo_root" \
HERMES_HOME="$tmp_dir/.hermes" \
  "$repo_root/hermes-sync-identity.sh" link >/dev/null

assert_link_target() {
  local link="$1" expected="$2"
  [[ -L "$link" ]] || { echo "expected symlink: $link" >&2; exit 1; }
  [[ "$(readlink "$link")" == "$expected" ]] || {
    echo "unexpected target for $link: $(readlink "$link")" >&2
    exit 1
  }
}

assert_link_target "$tmp_dir/.hermes/SOUL.md" "$repo_root/src/hermes-identity/SOUL.md"
assert_link_target "$tmp_dir/.hermes/memories/MEMORY.md" "$repo_root/src/hermes-identity/memories/MEMORY.md"
assert_link_target "$tmp_dir/.hermes/memories/USER.md" "$repo_root/src/hermes-identity/memories/USER.md"
