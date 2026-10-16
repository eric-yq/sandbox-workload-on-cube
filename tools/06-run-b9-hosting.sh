#!/usr/bin/env bash
# 06 - run B9 in the agent-hosting shape and bring the results home.
#
#   bash tools/06-run-b9-hosting.sh --build-template   # once, before the first run
#   bash tools/06-run-b9-hosting.sh                    # the sweep
#   B9_CONCURRENCIES=64,96,117 bash tools/06-run-b9-hosting.sh
#   THINK_TIME_MS=100 bash tools/06-run-b9-hosting.sh
#   bash tools/06-run-b9-hosting.sh --tail             # re-attach after a disconnect
#
# Same task mix and the same runner as 05, different archetype: a long-lived
# pool of sandboxes each holding one agent session, instead of a fresh
# sandbox per short RL rollout. It is all env knobs - scripts/run-b9-hosting.sh
# sets the profile and checks the preconditions. See RUNBOOK.md's "B9 hosting
# 模式" section and env.example section 6b.
#
# Separate from 05 rather than a flag on it, because the two shapes cannot go
# in the same table: 05's headline is rollouts/sec with sandbox churn inside
# the measurement, and this one's is steps/sec plus SLO density with
# provisioning reported separately as a fixture.
#
# Needs `arl-b9-small`, which 04 does NOT build - it requires raising
# CubeMaster's snapshot deadline for the build. `--build-template` does that
# and puts it back.
#
# Runs detached under setsid on the instance, so a dropped SSH connection
# does not kill the sweep, and pulls results back to ./results/ at the end.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config
ensure_ssh_key

REMOTE_LOG="${REMOTE_REPO}/tools-run.log"
TEMPLATE_ALIAS="${TEMPLATE_B9:-arl-b9-small}"

# --tail re-attaches to a run already in flight. Same contract as 05: the
# remote launcher writes tools-run.pid first and tools-run.rc last.
if [[ "${1:-}" == "--tail" ]]; then
  PRIVATE_IP="$(state_require PRIVATE_IP)"
  pid="$(ssh_run "cat ${REMOTE_REPO}/tools-run.pid 2>/dev/null" | tr -d '\r\n ' || true)"
  if [[ -n "${pid}" ]]; then
    # shellcheck disable=SC2046
    exec ssh $(_ssh_opts) -t "$(ssh_host)" "tail -f --pid=${pid} -n +1 ${REMOTE_LOG}"
  fi
  rc="$(ssh_run "cat ${REMOTE_REPO}/tools-run.rc 2>/dev/null" | tr -d '\r\n ' || true)"
  [[ -n "${rc}" ]] && log "the run has finished (rc=${rc}); showing the log"
  # shellcheck disable=SC2046
  exec ssh $(_ssh_opts) -t "$(ssh_host)" "cat ${REMOTE_LOG}"
fi

BUILD_ONLY=0
case "${1:-}" in
  "")               : ;;
  --build-template) BUILD_ONLY=1 ;;
  *) die "unknown argument '${1}'
     usage: 06-run-b9-hosting.sh [--build-template | --tail]
     Concurrency and think time are env knobs, not positional arguments:
       B9_CONCURRENCIES=64,96 bash tools/06-run-b9-hosting.sh" ;;
esac

PRIVATE_IP="$(state_require PRIVATE_IP)"
[[ "$(state_get ENV_READY || echo 0)" == "1" ]] || die "the environment is not installed.
     Run: bash tools/03-setup-env.sh"

wait_for_ssh 300

# The repo on the instance has to carry scripts/run-b9-hosting.sh and the
# b9 hosting knobs in config.py. An instance set up before those existed
# would silently run the RL shape instead, which is exactly the confusion
# this whole mode exists to prevent.
if ! ssh_run "test -x ${REMOTE_REPO}/scripts/run-b9-hosting.sh"; then
  die "the instance's copy of the repo has no scripts/run-b9-hosting.sh.
     Re-sync it:  bash tools/03-setup-env.sh"
fi

# ------------------------------------------------------------------
# --build-template: build arl-b9-small, then stop
# ------------------------------------------------------------------
if (( BUILD_ONLY )); then
  step "building ${TEMPLATE_ALIAS}"
  warn "this raises cubelet_conf.common_timeout_insec to 180s for the build
     and puts it back afterwards. A 1 vCPU guest cold-starts all five stacks
     in more than the shipped 30s, so the snapshot gate would otherwise
     expire mid-build. It bounds template build only."
  # shellcheck disable=SC2046
  scp $(_ssh_opts) -q "${TOOLS_DIR}/remote/build-b9-small.sh" \
    "$(ssh_host):${REMOTE_REPO}/tools/remote/build-b9-small.sh"
  # -t so sudo can prompt if the instance ever needs it, and so the build's
  # progress is visible live - it takes a couple of minutes.
  # shellcheck disable=SC2046
  ssh $(_ssh_opts) -t "$(ssh_host)" \
    "cd ${REMOTE_REPO} && TEMPLATE_B9=$(printf %q "${TEMPLATE_ALIAS}") bash tools/remote/build-b9-small.sh" \
    || die "building ${TEMPLATE_ALIAS} failed; see the output above."
  state_set B9_SMALL_READY 1
  echo >&2
  log "Next: bash tools/06-run-b9-hosting.sh"
  exit 0
fi

# ------------------------------------------------------------------
# Preconditions, checked before spending instance-hours
# ------------------------------------------------------------------
step "preflight: is ${TEMPLATE_ALIAS} usable?"
# Deliberately a real create, not a status check. A template reports READY
# from MySQL even when the node-local image registry no longer has it - the
# state after a reboot, since Cubelet keeps that registry on tmpfs. Finding
# that out 10 minutes into a sweep wastes the time and the instance-hours.
# shellcheck disable=SC2046
if ! ssh $(_ssh_opts) -n "$(ssh_host)" \
     "cd ${REMOTE_REPO} && TEMPLATE_B9=$(printf %q "${TEMPLATE_ALIAS}") \
      .venv/bin/python orchestrator/smoke_test.py B9 >/dev/null 2>&1"; then
  die "cannot create a sandbox from ${TEMPLATE_ALIAS}.

     If it was never built:
       bash tools/06-run-b9-hosting.sh --build-template

     If it was built and the instance has since rebooted, the template
     definition survived in MySQL but the node-local image did not (Cubelet
     keeps its registry on tmpfs). Same fix - rebuild it."
fi
log "${TEMPLATE_ALIAS} is usable"

# ------------------------------------------------------------------
# Knobs forwarded for this run only
# ------------------------------------------------------------------
# load_env() in scripts/_lib.sh only sets keys that are not already set, and
# scripts/run-b9-hosting.sh uses ${VAR:-default}, so anything exported here
# beats both the instance's .env and the script's own profile defaults.
#
# BENCH_PROFILE is deliberately NOT honoured: its override blocks carry RL
# shapes (CONCURRENCIES, DURATION_SEC) that would half-apply here and leave
# a run that is neither archetype.
HOSTING_KNOBS=(
  BENCH_MODE CUBE_STRATEGY CUBE_TRANSPORT
  THINK_TIME_MS THINK_TIME_JITTER_MS THINK_TIME_PAUSE
  B9_EPISODE_MIN_STEPS B9_EPISODE_MAX_STEPS
  TEMPLATE_B9 B9_CONCURRENCIES B9_DURATION_SEC
  WARMUP_SEC COOLDOWN_SEC
  SLO_DEGRADE_FACTOR SLO_METRIC SLO_STEP_P95_MS
  CLIENT_NPROCS CLIENT_NPROCS_MAX
)
shell_env=""
for v in "${HOSTING_KNOBS[@]}"; do
  [[ -n "${!v:-}" ]] && shell_env+="$(printf '%s=%q ' "${v}" "${!v}")"
done
# Pin the template even when the caller did not name it, so the remote side
# cannot fall back to arl-b9 from .env and quietly measure a 4 vCPU shape.
[[ -n "${TEMPLATE_B9:-}" ]] || shell_env+="$(printf 'TEMPLATE_B9=%q ' "${TEMPLATE_ALIAS}")"
[[ -n "${shell_env}" ]] && log "overrides: ${shell_env}"

step "running B9 in hosting mode"
warn "safe to disconnect: the run is detached. Re-attach with
       bash tools/06-run-b9-hosting.sh --tail"

# shellcheck disable=SC2046
scp $(_ssh_opts) -q "${TOOLS_DIR}/remote/run-bench.sh" \
  "$(ssh_host):${REMOTE_REPO}/tools/remote/run-bench.sh"

env_prefix="${shell_env}"
[[ -n "${env_prefix}" ]] && env_prefix="env ${env_prefix}"

# Redirections go on the OUTSIDE of the brace group. Redirecting only the
# inner command leaves the backgrounded subshell holding ssh's stdout, so
# ssh cannot close the channel and blocks until the run ends. See the long
# comment in 05-run-benchmark.sh.
ssh_run "{ cd ${REMOTE_REPO} && rm -f tools-run.log tools-run.pid tools-run.rc && \
  setsid ${env_prefix}bash tools/remote/run-bench.sh B9-HOSTING ; } \
  >/dev/null 2>&1 </dev/null & echo launched"

REMOTE_PID=""
for _ in $(seq 1 30); do
  REMOTE_PID="$(ssh_run "cat ${REMOTE_REPO}/tools-run.pid 2>/dev/null" | tr -d '\r\n ' || true)"
  [[ -n "${REMOTE_PID}" ]] && break
  if ssh_run "test -f ${REMOTE_REPO}/tools-run.rc" 2>/dev/null; then break; fi
  sleep 1
done
if [[ -z "${REMOTE_PID}" ]] && ! ssh_run "test -f ${REMOTE_REPO}/tools-run.rc" 2>/dev/null; then
  die "the run never started: no ${REMOTE_REPO}/tools-run.pid appeared.
     Check by hand:
       ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} \\
         'cd ${REMOTE_REPO} && bash scripts/run-b9-hosting.sh'"
fi
[[ -n "${REMOTE_PID}" ]] && log "running as pid ${REMOTE_PID}"

log "tailing ${REMOTE_LOG} (Ctrl-C detaches; the run keeps going)"
while :; do
  # shellcheck disable=SC2046
  ssh $(_ssh_opts) -t "$(ssh_host)" \
    "tail -f --pid=${REMOTE_PID:-1} -n +1 ${REMOTE_LOG} 2>/dev/null || tail -n 40 ${REMOTE_LOG}" || true
  if [[ -z "${REMOTE_PID}" ]] || ! ssh_run "kill -0 ${REMOTE_PID} 2>/dev/null"; then
    break
  fi
  warn "reconnecting to the log (the run is still alive)"
  sleep 5
done

step "results"
# shellcheck disable=SC2046
ssh $(_ssh_opts) -n "$(ssh_host)" "tail -n 25 ${REMOTE_LOG}"

RUN_RC="$(ssh_run "cat ${REMOTE_REPO}/tools-run.rc 2>/dev/null" | tr -d '\r\n ' || true)"
if [[ -z "${RUN_RC}" ]]; then
  warn "no exit status was recorded; the run may have been killed.
     Full log: bash tools/06-run-b9-hosting.sh --tail"
elif [[ "${RUN_RC}" != "0" ]]; then
  warn "the run exited ${RUN_RC}. Results below may be incomplete.
     Full log: bash tools/06-run-b9-hosting.sh --tail"
else
  log "run exited 0"
fi

rsync_results_down

# ------------------------------------------------------------------
# The two numbers that decide whether this run says anything
# ------------------------------------------------------------------
step "hosting verdict"
latest="$(ls -1dt "${REPO_DIR}"/results/*/concurrent_result_* 2>/dev/null | head -1 || true)"
if [[ -z "${latest}" || ! -f "${latest}/rollup.json" ]]; then
  warn "no concurrent_result_*/rollup.json found under ${REPO_DIR}/results/"
else
  log "reading ${latest#"${REPO_DIR}/"}/rollup.json"
  python3 - "${latest}/rollup.json" <<'PY' || warn "could not summarise the rollup"
import json, sys
d = json.load(open(sys.argv[1]))
print(f"    mode            : {d.get('mode')}")
sd = d.get("slo_density") or {}
if not sd:
    print("    slo_density     : MISSING - was BENCH_MODE=hosting actually applied?")
else:
    print(f"    knee            : {sd.get('knee_sessions')} sessions @ "
          f"{(sd.get('knee_steps_per_sec') or 0):.1f} steps/s")
    print(f"    max passing     : {sd.get('max_passing_sessions')}")
    print(f"    sweep exhausted : {sd.get('sweep_exhausted')}"
          + ("   <- capacity is ABOVE the sweep; knee is a floor, raise B9_CONCURRENCIES"
             if sd.get("sweep_exhausted") else ""))
    for lv in sd.get("levels", []):
        v = ", ".join(f"{k} {x['observed_ms']:.0f}>{x['budget_ms']:.0f}ms"
                      for k, x in (lv.get("violations") or {}).items())
        print(f"      c={lv['sessions']:<5} met={str(lv['met']):<5} {v}")
# A think-time-bound run is the failure mode that still produces a tidy file.
worst = None
for r in d.get("runs", []):
    cpu = ((r.get("resource") or {}).get("cpu_util_avg") or 0.0)
    if worst is None or cpu > worst:
        worst = cpu
if worst is not None:
    print(f"    peak host CPU   : {worst:.1f}%"
          + ("   <- too idle to be a host comparison; the run is think-time "
             "bound. Raise B9_CONCURRENCIES or lower THINK_TIME_MS."
             if worst < 15 else ""))
errs = sum(((r.get("latency_ms") or {}).get("rollout") or {}).get("errors") or 0
           for r in d.get("runs", []))
done = sum((r.get("throughput") or {}).get("total_rollouts") or 0 for r in d.get("runs", []))
print(f"    rollouts        : {done} completed, {errs} errored"
      + ("   <- check this BEFORE the throughput numbers; survivors' "
         "percentiles look healthy regardless." if errs > done else ""))
PY
fi

echo >&2
log "results are in ${REPO_DIR}/results/"
echo >&2
warn "the instance is still running and still billing.
     When you are done:  bash tools/99-teardown.sh"
