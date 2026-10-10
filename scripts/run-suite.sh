#!/usr/bin/env bash
# Run the full benchmark suite on this metal host.
#
# There are no worker containers to bring up. Every benchmark creates its
# own microVMs on demand - one per rollout - so the only things that must
# already be running are the CubeSandbox services and the orchestrator
# process itself.
#
# Results land in ./results/<arch>/ and, if S3_BUCKET is set, under
# s3://${S3_BUCKET}/${S3_PREFIX}/<arch>/.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"
CUBE_PROXY_URL="${CUBE_PROXY_URL:-http://127.0.0.1:80}"
CUBE_OPS_URL="${CUBE_OPS_URL:-http://127.0.0.1:3010}"
export CUBE_API_URL CUBE_PROXY_URL CUBE_OPS_URL

require_cube
require_venv

mkdir -p results

# Direct transport reads the sandbox IP out of CubeMaster's Redis routing
# map, so it needs the redis client that the base requirements leave out.
check_direct_transport_deps() {
  [[ "${CUBE_TRANSPORT:-path}" == "direct" ]] || return 0
  if ! .venv/bin/python -c 'import redis' 2>/dev/null; then
    warn "CUBE_TRANSPORT=direct needs the redis package to read the sandbox
     routing map, and it is not installed in .venv. Every sandbox will
     silently fall back to CubeProxy, which is not what you asked for.
     Fix: .venv/bin/pip install redis"
  fi
}

cleanup() {
  local rc=$?
  # Reap any sandbox the suite leaked. A leaked sandbox holds scheduler
  # quota, which would silently skew whatever runs next - and Cube ships
  # with no cluster-wide idle timeout, so nothing else will collect it.
  #
  # Filtering is by the arl_bench metadata marker the orchestrator stamps
  # on every sandbox it creates, so this cannot touch anything else on the
  # node.
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python orchestrator/reap.py || true
  fi
  exit "${rc}"
}
trap cleanup EXIT

check_direct_transport_deps

log "instance: $(uname -m), $(nproc) vCPU"
log "CubeAPI:  ${CUBE_API_URL}"
log "CubeProxy: ${CUBE_PROXY_URL}  (transport: ${CUBE_TRANSPORT:-path})"
log "strategy: ${CUBE_STRATEGY:-fresh} (one sandbox per rollout)"
log "skipping: ${SKIP:-<none>}"

rc=0
.venv/bin/python orchestrator/main.py || rc=$?

if (( rc != 0 )); then
  echo >&2
  warn "orchestrator exited ${rc}. Useful next steps:"
  echo "     journalctl -u cube-sandbox-cube-api -n 120 --no-pager" >&2
  echo "     journalctl -u cube-sandbox-cubelet -n 120 --no-pager" >&2
  echo "     docker logs --tail 120 cube-proxy" >&2
  echo "     cubemastercli tpl list" >&2
  echo "     .venv/bin/python orchestrator/smoke_test.py --all" >&2
fi

log "done (rc=${rc}). Local results in ./results/"
[[ -n "${S3_BUCKET:-}" ]] && log "S3: s3://${S3_BUCKET}/${S3_PREFIX:-agentic-rl-bench}/"
exit "${rc}"
