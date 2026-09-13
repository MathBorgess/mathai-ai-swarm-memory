#!/usr/bin/env bash
# Hardened container launch for the isolated ask worker.
# Fail closed: missing runtime/image/job/docker, owner Hermes home, host network, or broker token.
set -euo pipefail

fail() { echo "ask-worker: $*" >&2; exit 2; }

[[ -n "${ASK_RUNTIME_HOME:-}" ]] || fail "ASK_RUNTIME_HOME missing"
[[ -n "${ASK_IMAGE:-}" ]] || fail "ASK_IMAGE missing"
ASK_JOB="${ASK_JOB:-${1:-}}"
[[ -n "${ASK_JOB}" ]] || fail "ASK_JOB missing"
[[ -f "${ASK_JOB}" ]] || fail "job file missing"
[[ -d "${ASK_RUNTIME_HOME}" ]] || fail "runtime home missing"
[[ -n "${ASK_WORKER_SCRIPT:-}" && -f "${ASK_WORKER_SCRIPT}" ]] || fail "ASK_WORKER_SCRIPT missing"
[[ -n "${ASK_STYLE:-}" && -f "${ASK_STYLE}" ]] || fail "ASK_STYLE missing"

# runtime home must not be the owner HOME/Hermes profile
OWNER_HOME="${HOME:-}"
if [[ -n "${OWNER_HOME}" && ( "${ASK_RUNTIME_HOME}" == "${OWNER_HOME}" || "${ASK_RUNTIME_HOME}" == "${OWNER_HOME}/.hermes" || "${ASK_RUNTIME_HOME}" == "${OWNER_HOME}/.hermes/"* ) ]]; then
  fail "runtime home must not be the owner HOME/Hermes profile"
fi

NETWORK="${ASK_NETWORK:-none}"
[[ "${NETWORK}" != "host" ]] || fail "host network is not allowed"

DOCKER="${ASK_DOCKER:-docker}"
command -v "${DOCKER}" >/dev/null 2>&1 || fail "container runtime missing"

# Never forward the owner peer token into the worker.
unset HERMES_BROKER_TOKEN || true
unset HERMES_A2A_URL || true
unset HERMES_REAL_HOME || true

USER_NS=(--user "$(id -u):$(id -g)")

ARGS=(
  run --rm
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --network "${NETWORK}"
  --pids-limit 64
  --memory 512m
  --tmpfs /tmp:rw,nosuid,size=32m
  --tmpfs /isolated/home:rw,nosuid,size=8m
  --tmpfs /isolated/workspace:rw,nosuid,size=8m
  "${USER_NS[@]}"
  -e HERMES_HOME=/isolated
  -e HOME=/isolated/home
  -e PYTHONNOUSERSITE=1
  -e HERMES_ASK_STUB="${HERMES_ASK_STUB:-}"
  -e ASK_MODEL="${ASK_MODEL:-}"
  --mount "type=bind,src=${ASK_RUNTIME_HOME},dst=/isolated,readonly"
  --mount "type=bind,src=${ASK_JOB},dst=/job.json,readonly"
  --mount "type=bind,src=${ASK_WORKER_SCRIPT},dst=/opt/ask-worker/worker_main.py,readonly"
  --mount "type=bind,src=${ASK_STYLE},dst=/opt/ask-worker/style/SOUL.md,readonly"
)

exec "${DOCKER}" "${ARGS[@]}" "${ASK_IMAGE}" python3 /opt/ask-worker/worker_main.py /job.json
