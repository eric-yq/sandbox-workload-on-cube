#!/usr/bin/env bash
# Runs ON the instance, uploaded by tools/06-run-b9-hosting.sh.
#
# Builds `arl-b9-small`: the same image and the same five services as
# arl-b9, with the vCPU count cut to 1 so the node admits more sandboxes.
# Built under its own alias, so arl-b9 is never replaced - the two are meant
# to be compared, not swapped.
#
# Why this needs its own script rather than a line in 04: a 1 vCPU guest
# cold-starts all five stacks in more than the 30s CubeMaster allows for the
# snapshot gate (cubelet_conf.common_timeout_insec), so the build dies in
# CREATING_TEMPLATE - "context deadline exceeded" on x86, "statuscode:503"
# on arm64, same phase and same cause. The deadline has to be raised for the
# build and put straight back, because it also bounds sandbox-create RPCs at
# run time and the measurement should happen on the shipped config. Creating
# a sandbox from the finished snapshot is ~85-103ms, so the raise has no
# effect on any measured number.
#
# Memory stays at 4096 MiB and that is not negotiable: 2048 builds fine AND
# passes smoke_test, then kills B5's DuckDB mid-query under a real session
# (CubeProxy starts returning 502 for :8005 and ~98% of rollouts fail). The
# build gate and the smoke test each touch a service once, so neither can
# see a service that only dies once TPC-H queries start allocating.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
BAK="${CONF}.pre-b9s"
ALIAS="${TEMPLATE_B9:-arl-b9-small}"
CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

restored=0
restore() {
  (( restored )) && return 0
  restored=1
  if sudo test -f "${BAK}"; then
    sudo cp -a "${BAK}" "${CONF}"
    sudo systemctl restart cube-sandbox-cubemaster.service
    sleep 12
    log "restored: $(sudo grep -n 'common_timeout_insec' "${CONF}" | tr -d '\n')"
  fi
}
# Restore on every exit path, including Ctrl-C. Leaving the deadline at 180s
# would silently change how a saturated run fails later.
trap restore EXIT INT TERM

sudo test -f "${CONF}" || die "CubeMaster config not found at ${CONF}.
     Is CubeSandbox installed? Run: sudo bash scripts/ensure-cube.sh"

current="$(sudo grep -oP 'common_timeout_insec:\s*\K\d+' "${CONF}" | head -1 || true)"
log "current cubelet_conf.common_timeout_insec: ${current:-<unset>}"

sudo test -f "${BAK}" || sudo cp -a "${CONF}" "${BAK}"

if [[ "${current:-0}" -lt 180 ]]; then
  sudo sed -i 's/^\([[:space:]]*\)common_timeout_insec:[[:space:]]*[0-9]\+/\1common_timeout_insec: 180/' "${CONF}"
  raised="$(sudo grep -oP 'common_timeout_insec:\s*\K\d+' "${CONF}" | head -1 || true)"
  [[ "${raised}" == "180" ]] \
    || die "could not raise common_timeout_insec (still ${raised:-<unset>}).
     Edit ${CONF} by hand, restart cube-sandbox-cubemaster, then re-run."
  log "raised to 180 for the build"
  sudo systemctl restart cube-sandbox-cubemaster.service
  sleep 12
  systemctl is-active --quiet cube-sandbox-cubemaster.service \
    || die "CubeMaster did not come back after the restart.
     journalctl -u cube-sandbox-cubemaster -n 80 --no-pager"
  curl -fsS --max-time 10 -o /dev/null "${CUBE_API_URL}/templates" \
    || die "CubeAPI is not answering after the CubeMaster restart."
else
  log "already >= 180, leaving it alone"
fi

# A previous failed attempt leaves a FAILED template row with NO alias, and
# provision-templates.sh only deletes by alias - so it would never clear it,
# and the node would keep the dead row forever.
log "clearing any FAILED template rows left by an earlier attempt"
curl -fsS --max-time 15 "${CUBE_API_URL}/templates" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    for t in json.load(sys.stdin):
        if t.get("status") != "READY" and not (t.get("aliases") or []):
            print(t.get("templateID", ""))
except Exception:
    pass
' 2>/dev/null | while read -r tid; do
  [[ -z "${tid}" ]] && continue
  code="$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    "${CUBE_API_URL}/templates/${tid}?sync=true" || true)"
  log "  deleted ${tid} -> HTTP ${code}"
done

log "building ${ALIAS} (1 vCPU / 4096 MiB, five stacks)"
SANDBOX_VCPU_B9S="${SANDBOX_VCPU_B9S:-1}" \
SANDBOX_MEM_MB_B9S="${SANDBOX_MEM_MB_B9S:-4096}" \
  bash scripts/provision-templates.sh B9S \
  || die "build of ${ALIAS} failed; see the output above.
     'context deadline exceeded' / 'statuscode:503' in CREATING_TEMPLATE means
     the guest still did not answer :8010/healthz in time - raise
     common_timeout_insec further in ${CONF} and retry."

# Put the deadline back BEFORE verifying, so the verification runs on the
# same config the benchmark will.
restore

log "verifying ${ALIAS} by actually creating a sandbox from it"
# status READY is a database fact; this boots a sandbox and runs one step
# through every one of the five services.
TEMPLATE_B9="${ALIAS}" .venv/bin/python orchestrator/smoke_test.py B9 \
  || die "${ALIAS} is READY but a sandbox could not be created from it.
     If the failure is on B5 (:8005) under load rather than at create, the
     template is memory-starved - SANDBOX_MEM_MB_B9S must stay at 4096."

log "${ALIAS} is built and usable"
