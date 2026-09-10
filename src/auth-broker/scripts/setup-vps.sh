#!/usr/bin/env bash
# Prepare a checked broker checkout. It never generates or prints secrets.
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: setup-vps.sh /absolute/path/to/auth-broker.env'
  printf '%s\n' 'Installs the local virtualenv, validates required configuration, and runs tests.'
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

if [[ ${1:-} == '--help' || ${1:-} == '-h' ]]; then
  usage
  exit 0
fi

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
config_path=$1
[[ $config_path == /* ]] || fail 'configuration path must be absolute'
[[ -f $config_path ]] || fail 'configuration file does not exist'

# The configuration is local, owner-controlled shell assignments only.
set -a
# shellcheck disable=SC1090
. "$config_path"
set +a

required=(
  AUTH_BROKER_DATABASE_PATH
  AUTH_BROKER_AUDIENCE
  HERMES_A2A_URL
  HERMES_BROKER_TOKEN
  GITHUB_OAUTH_CLIENT_ID
  GITHUB_OAUTH_CLIENT_SECRET
  GITHUB_ALLOWED_USER_ID
)
for name in "${required[@]}"; do
  [[ -n ${!name:-} && ${!name//[[:space:]]/} ]] || fail "missing or empty $name"
done

[[ $AUTH_BROKER_DATABASE_PATH == /* ]] || fail 'AUTH_BROKER_DATABASE_PATH must be absolute'

broker_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
python_bin=${PYTHON_BIN:-python3.12}
command -v "$python_bin" >/dev/null || fail "Python executable not found: $python_bin"

install -d -m 700 "$(dirname -- "$AUTH_BROKER_DATABASE_PATH")"
"$python_bin" -m venv "$broker_dir/.venv"
"$broker_dir/.venv/bin/python" -m pip install --upgrade pip
"$broker_dir/.venv/bin/python" -m pip install -e "$broker_dir[dev]"
"$broker_dir/.venv/bin/python" -m pytest "$broker_dir/tests" -q

printf '%s\n' 'Broker checkout prepared. Start Uvicorn separately after reviewing the Cloudflare Tunnel and Access configuration.'
