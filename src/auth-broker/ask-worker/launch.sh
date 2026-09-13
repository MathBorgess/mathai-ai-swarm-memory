#!/usr/bin/env bash
# Hardened container launch for the isolated ask worker.
# Fail closed: missing inference file/profile/image/job/docker, owner Hermes home, host network.
set -euo pipefail

fail() { echo "ask-worker: $*" >&2; exit 2; }

[[ -n "${ASK_IMAGE:-}" ]] || fail "ASK_IMAGE missing"
ASK_JOB="${ASK_JOB:-${1:-}}"
[[ -n "${ASK_JOB}" ]] || fail "ASK_JOB missing"
[[ -f "${ASK_JOB}" ]] || fail "job file missing"
[[ -n "${ASK_PROFILE:-}" && -d "${ASK_PROFILE}" ]] || fail "ASK_PROFILE missing"
[[ -n "${ASK_WORKER_SCRIPT:-}" && -f "${ASK_WORKER_SCRIPT}" ]] || fail "ASK_WORKER_SCRIPT missing"
[[ -n "${ASK_STYLE:-}" && -f "${ASK_STYLE}" ]] || fail "ASK_STYLE missing"
[[ -n "${ASK_INFERENCE:-}" && -f "${ASK_INFERENCE}" ]] || fail "ASK_INFERENCE missing"
[[ -n "${ASK_CONTAINER_NAME:-}" ]] || fail "ASK_CONTAINER_NAME missing"

OWNER_HOME="${HOME:-}"
if [[ -n "${OWNER_HOME}" ]]; then
  case "${ASK_PROFILE}" in
    "${OWNER_HOME}"|"${OWNER_HOME}/.hermes"|"${OWNER_HOME}/.hermes"/*) fail "profile must not be the owner HOME/Hermes profile" ;;
  esac
  case "${ASK_INFERENCE}" in
    "${OWNER_HOME}/.hermes"|"${OWNER_HOME}/.hermes"/*|"${OWNER_HOME}/.env") fail "inference file must not be an owner credential/config" ;;
  esac
fi

NETWORK="${ASK_NETWORK:-}"
[[ -n "${NETWORK}" ]] || fail "ASK_NETWORK missing"
[[ "${NETWORK}" != "host" ]] || fail "host network is not allowed"

DOCKER="${ASK_DOCKER:-docker}"
command -v "${DOCKER}" >/dev/null 2>&1 || fail "container runtime missing"

unset HERMES_BROKER_TOKEN || true
unset HERMES_A2A_URL || true
unset HERMES_REAL_HOME || true
unset OPENROUTER_API_KEY || true
unset OPENAI_API_KEY || true
unset ANTHROPIC_API_KEY || true
unset HERMES_ASK_STUB || true

USER_NS=(--user "$(id -u):$(id -g)")

ARGS=(
  run --rm
  --name "${ASK_CONTAINER_NAME}"
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
  -e HERMES_ASK_SRC=/opt/hermes-agent
  -e HERMES_BUNDLED_PLUGINS=/isolated/bundled-plugins
  -e ASK_INFERENCE=/inference.json
  -e ASK_OWNER_SENTINEL="${ASK_OWNER_SENTINEL:-}"
  --mount "type=bind,src=${ASK_PROFILE},dst=/isolated,readonly"
  --mount "type=bind,src=${ASK_JOB},dst=/job.json,readonly"
  --mount "type=bind,src=${ASK_WORKER_SCRIPT},dst=/opt/ask-worker/worker_main.py,readonly"
  --mount "type=bind,src=${ASK_STYLE},dst=/opt/ask-worker/style/SOUL.md,readonly"
  --mount "type=bind,src=${ASK_INFERENCE},dst=/inference.json,readonly"
)

# Image ENTRYPOINT is python3 worker_main.py; pass only the job path.
exec "${DOCKER}" "${ARGS[@]}" "${ASK_IMAGE}" /job.json
