#!/usr/bin/env bash
# Run one benchmark by ID, for iterative work.
#
#   bash scripts/run-single.sh B9
#   DURATION_SEC=60 B9_DURATION_SEC=60 bash scripts/run-single.sh B9
#
# Implemented by passing SKIP for every other benchmark, so it takes the
# exact same code path as run-suite.sh.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

ALL_IDS=(B1 B3 B4 B5 B7 B8 B9)

BID="${1:-}"
if [[ -z "${BID}" ]]; then
  die "usage: run-single.sh <${ALL_IDS[*]}>"
fi
BID="$(echo "${BID}" | tr '[:lower:]' '[:upper:]')"

found=0
for id in "${ALL_IDS[@]}"; do
  [[ "${id}" == "${BID}" ]] && found=1
done
(( found )) || die "unknown benchmark id: ${BID} (known: ${ALL_IDS[*]})"

CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"
CUBE_PROXY_URL="${CUBE_PROXY_URL:-http://127.0.0.1:80}"
CUBE_OPS_URL="${CUBE_OPS_URL:-http://127.0.0.1:3010}"
export CUBE_API_URL CUBE_PROXY_URL CUBE_OPS_URL

require_cube
require_venv
mkdir -p results

# Everything except the requested benchmark.
skip_list=""
for id in "${ALL_IDS[@]}"; do
  [[ "${id}" == "${BID}" ]] && continue
  skip_list="${skip_list:+${skip_list},}${id}"
done
export SKIP="${skip_list}"

cleanup() {
  local rc=$?
  # Reap by the arl_bench metadata marker, so this can only ever delete
  # sandboxes this suite created. Cube ships with no cluster-wide idle
  # timeout, so a leaked sandbox would otherwise hold node quota forever.
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python orchestrator/reap.py || true
  fi
  exit "${rc}"
}

trap cleanup EXIT

log "running ${BID} only (SKIP=${SKIP})"
log "template: $(eval echo "\${TEMPLATE_${BID}:-arl-$(echo "${BID}" | tr '[:upper:]' '[:lower:]')}")"

rc=0
.venv/bin/python orchestrator/main.py || rc=$?

if (( rc != 0 )); then
  echo >&2
  warn "orchestrator exited ${rc}; try:"
  echo "     .venv/bin/python orchestrator/smoke_test.py ${BID}" >&2
  echo "     journalctl -u cube-sandbox-cube-api -n 120 --no-pager" >&2
  echo "     journalctl -u cube-sandbox-cubelet -n 120 --no-pager" >&2
fi

log "done (rc=${rc})"
exit "${rc}"
