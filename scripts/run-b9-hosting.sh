#!/usr/bin/env bash
# Run B9 in the *agent hosting* shape instead of the Agentic-RL shape.
#
#   bash scripts/run-b9-hosting.sh
#   B9_CONCURRENCIES=64,96,117 bash scripts/run-b9-hosting.sh
#
# Same task mix, same runner, different archetype: a long-lived pool of
# sandboxes each holding one agent session, instead of a fresh sandbox per
# short RL rollout. Everything here is env knobs - no separate code path -
# and the mechanics are CUBE_STRATEGY=pool + THINK_TIME_MS + B9_EPISODE_*.
# See `env.example` section 6b and the B9 hosting section of RUNBOOK.md.
#
# Preconditions this script checks rather than assumes, because each one
# fails in a way that still produces a plausible-looking result file:
#
#   1. The template exists. arl-b9-small must be built first; it needs
#      CubeMaster's snapshot deadline raised temporarily (a 1 vCPU guest
#      cold-starts all five stacks in more than the shipped 30s).
#   2. Concurrency is at or below what the node will admit. Above it,
#      creates get rejected and the level measures the admission race.
#   3. Think time does not dominate. If sessions/think_time asks for less
#      than the host can serve, throughput is `concurrency / think_time`
#      on any machine and the comparison is vacuous.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh

# Exported before load_env so the hosting profile wins over a .env that
# carries the RL defaults, while anything the caller exported still wins
# over this script. load_env only sets variables that are unset.
export BENCH_MODE="${BENCH_MODE:-hosting}"
export CUBE_STRATEGY="${CUBE_STRATEGY:-pool}"
export THINK_TIME_MS="${THINK_TIME_MS:-200}"
export THINK_TIME_JITTER_MS="${THINK_TIME_JITTER_MS:-50}"
export THINK_TIME_PAUSE="${THINK_TIME_PAUSE:-0}"
export B9_EPISODE_MIN_STEPS="${B9_EPISODE_MIN_STEPS:-60}"
export B9_EPISODE_MAX_STEPS="${B9_EPISODE_MAX_STEPS:-100}"
export TEMPLATE_B9="${TEMPLATE_B9:-arl-b9-small}"
export B9_CONCURRENCIES="${B9_CONCURRENCIES:-32,64,96}"
export B9_DURATION_SEC="${B9_DURATION_SEC:-150}"
export WARMUP_SEC="${WARMUP_SEC:-25}"
export COOLDOWN_SEC="${COOLDOWN_SEC:-15}"

load_env

CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"
CUBE_OPS_URL="${CUBE_OPS_URL:-http://127.0.0.1:3010}"
export CUBE_API_URL CUBE_OPS_URL

require_cube
require_venv

# ------------------------------------------------------------------
# 1. The template has to exist, and READY is not the same as usable
# ------------------------------------------------------------------
template_status() {
  curl -fsS --max-time 10 "${CUBE_API_URL}/templates" 2>/dev/null \
    | .venv/bin/python -c '
import json, sys
want = sys.argv[1]
try:
    for t in json.load(sys.stdin):
        if want in (t.get("aliases") or []) or want == t.get("templateID"):
            print(t.get("status", "?"))
            break
    else:
        print("ABSENT")
except Exception:
    print("UNKNOWN")
' "${TEMPLATE_B9}" 2>/dev/null || echo UNKNOWN
}

status="$(template_status)"
case "${status}" in
  READY) log "template ${TEMPLATE_B9}: READY" ;;
  ABSENT)
    die "template ${TEMPLATE_B9} does not exist on this node.

     Build it (B9S is the 1 vCPU / 4096 MiB variant of the B9 template,
     built under its own alias so it never replaces arl-b9):

       CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
       sudo cp -a \$CONF \$CONF.bak
       sudo sed -i 's/^  common_timeout_insec: 30\$/  common_timeout_insec: 180/' \$CONF
       sudo systemctl restart cube-sandbox-cubemaster.service && sleep 12
       bash scripts/provision-templates.sh B9S
       sudo cp -a \$CONF.bak \$CONF
       sudo systemctl restart cube-sandbox-cubemaster.service

     The deadline has to be raised because a 1 vCPU guest cold-starts all
     five stacks in more than the shipped 30s, so the snapshot gate
     expires mid-build. It bounds template build only - creating a sandbox
     from the finished snapshot is ~85-103ms - so put it back afterwards
     and measure on the shipped config." ;;
  UNKNOWN)
    warn "could not read ${CUBE_API_URL}/templates; skipping the template check" ;;
  *)
    die "template ${TEMPLATE_B9} is ${status}, not READY.
     A FAILED template keeps its row but never gets an alias, so
     provision-templates.sh will not replace it. Delete it by ID first:
       curl -s ${CUBE_API_URL}/templates | grep -o 'tpl-[0-9a-f]*'
       curl -X DELETE '${CUBE_API_URL}/templates/<tid>?sync=true'" ;;
esac

# ------------------------------------------------------------------
# 2. Admission ceiling: min(QuotaCpu/cpu, QuotaMem/mem, MaxMvmLimit)
# ------------------------------------------------------------------
# The node admits whichever of CPU quota, memory quota and the derived
# sandbox ceiling binds first. For the 1 vCPU / 4096 MiB template on a
# 192000m / ~482GiB node that is memory: min(192, 117) = 117. Asking for
# more does not fail loudly - CubeMaster rejects creates using 1s-stale
# metrics, so the level half-fills and reports a throughput number.
#
# The template's shape is not in the API response, so it is taken from the
# SANDBOX_VCPU_* / SANDBOX_MEM_MB_* values that provision-templates.sh
# used to build it. Keep them in sync or this check misleads.
case "${TEMPLATE_B9}" in
  *-small) tpl_bid="B9S" ;;
  *)       tpl_bid="B9"  ;;
esac
eval "tpl_vcpu=\${SANDBOX_VCPU_${tpl_bid}:-}"
eval "tpl_mem=\${SANDBOX_MEM_MB_${tpl_bid}:-}"
tpl_vcpu="${tpl_vcpu:-1}"
tpl_mem="${tpl_mem:-4096}"

cap="$(curl -fsS --max-time 10 "${CUBE_OPS_URL}/internal/v1/nodes" 2>/dev/null \
  | .venv/bin/python -c '
import json, sys
cpu_milli = int(sys.argv[1]) * 1000
mem_mb = int(sys.argv[2])
try:
    nodes = json.load(sys.stdin) or []
except Exception:
    sys.exit(0)
best = 0
for n in nodes:
    lim = [int(n.get("MaxMvmLimit") or 0) or 10**9]
    if int(n.get("QuotaCpu") or 0) and cpu_milli:
        lim.append(int(n["QuotaCpu"]) // cpu_milli)
    if int(n.get("QuotaMem") or 0) and mem_mb:
        lim.append(int(n["QuotaMem"]) // mem_mb)
    best = max(best, min(lim))
if best and best < 10**9:
    print(best)
' "${tpl_vcpu}" "${tpl_mem}" 2>/dev/null || true)"

highest="$(echo "${B9_CONCURRENCIES}" | tr ',' '\n' | sort -n | tail -1)"
if [[ -n "${cap}" ]]; then
  log "admission ceiling: ${cap} sandboxes (template ${tpl_vcpu} vCPU / ${tpl_mem} MiB)"
  if (( highest > cap )); then
    warn "B9_CONCURRENCIES tops out at ${highest}, above the ${cap} this node
     admits for a ${tpl_vcpu} vCPU / ${tpl_mem} MiB template. That level will
     fail creates and measure the admission race instead of the workload.
     Lower it, or shrink the template - but note 4096 MiB is a measured
     floor: at 2048 the build passes and B5's DuckDB then dies under load."
  fi
else
  warn "could not derive the admission ceiling from ${CUBE_OPS_URL}; make sure
     the highest level in B9_CONCURRENCIES (${highest}) fits this node"
fi

# ------------------------------------------------------------------
# 3. Think-time ceiling: is this run going to measure the host at all?
# ------------------------------------------------------------------
# Per-session step period is work + think. Work in this mix is ~96ms (B4
# browser is ~75% of it), so at think time T the fleet asks for
# concurrency/(T+0.096) steps/s. These hosts serve roughly 1000 steps/s,
# so if the ask lands far below that, both architectures return the same
# arithmetic at single-digit CPU.
.venv/bin/python - "${highest}" "${THINK_TIME_MS}" <<'PY'
import sys
conc = int(sys.argv[1]); think = float(sys.argv[2]) / 1000.0
WORK_S, HOST_CAP = 0.096, 1000.0
ask = conc / (think + WORK_S)
print(f"==> think-time ceiling: {ask:.0f} steps/s asked at {conc} sessions "
      f"/ {think*1000:.0f}ms think (host serves roughly {HOST_CAP:.0f})")
if ask < 0.25 * HOST_CAP:
    print(
        "WARN: that is under a quarter of host capacity, so this run will sit "
        "at single-digit\n"
        "      CPU and both architectures will return the same number. "
        "Raise B9_CONCURRENCIES\n"
        "      or lower THINK_TIME_MS - think time here is a duty-cycle knob, "
        "not a realistic pause.",
        file=sys.stderr)
PY

log "mode=${BENCH_MODE} strategy=${CUBE_STRATEGY} template=${TEMPLATE_B9}"
log "sessions=${B9_CONCURRENCIES} steps/session=${B9_EPISODE_MIN_STEPS}-${B9_EPISODE_MAX_STEPS}"
log "think=${THINK_TIME_MS}ms (+/-${THINK_TIME_JITTER_MS}) duration=${B9_DURATION_SEC}s"

# Hand off to run-single.sh so this takes the same code path as every
# other run: same SKIP plumbing, same reap-on-exit.
exec bash scripts/run-single.sh B9
