#!/usr/bin/env bash
# Clone and install the pinned Hermes commit. No model/provider call.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PIN="${ROOT}/HERMES_PIN"
[[ -f "${PIN}" ]] || { echo "HERMES_PIN missing" >&2; exit 2; }

commit="$(sed -n 's/^commit=//p' "${PIN}" | head -1)"
repo="$(sed -n 's/^repo=//p' "${PIN}" | head -1)"
[[ -n "${commit}" && -n "${repo}" ]] || { echo "HERMES_PIN incomplete" >&2; exit 2; }

dest="${HERMES_ASK_SRC:-/opt/hermes-agent}"
mkdir -p "${dest}"
if [[ ! -d "${dest}/.git" ]]; then
  git init "${dest}"
  git -C "${dest}" remote add origin "${repo}"
fi
git -C "${dest}" fetch --depth 1 origin "${commit}"
git -C "${dest}" checkout --detach FETCH_HEAD
head="$(git -C "${dest}" rev-parse HEAD)"
[[ "${head}" == "${commit}" ]] || { echo "Hermes HEAD ${head} != pin ${commit}" >&2; exit 2; }

python3 -m pip install --no-cache-dir \
  "openai==2.24.0" \
  "requests==2.33.0" \
  "pyyaml==6.0.3" \
  "python-dotenv==1.2.2" \
  "pydantic==2.13.4" \
  "httpx==0.28.1" \
  "tenacity==9.1.4" \
  "rich==14.3.3" \
  "jinja2==3.1.6" \
  "fire==0.7.1" \
  "croniter==6.0.0" \
  "snowballstemmer==3.1.1" \
  "packaging" \
  "prompt_toolkit==3.0.52" \
  "ruamel.yaml==0.18.17" \
  "certifi==2026.5.20"

export PYTHONPATH="${dest}${PYTHONPATH:+:${PYTHONPATH}}"
export HERMES_ASK_SRC="${dest}"
python3 "${ROOT}/verify_hermes.py"
echo "Hermes ${commit} installed at ${dest}"
