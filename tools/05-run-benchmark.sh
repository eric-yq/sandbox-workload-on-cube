#!/usr/bin/env bash
# 05 - run the benchmark and bring the results home.
#
#   bash tools/05-run-benchmark.sh                     # full suite: B1 B3 B4 B5 B7 B8 B9
#   bash tools/05-run-benchmark.sh B1                  # one benchmark
#   bash tools/05-run-benchmark.sh B3 B5               # a subset
#   BENCH_PROFILE=quick bash tools/05-run-benchmark.sh # short run, no re-setup
#   CUBE_STRATEGY=clone bash tools/05-run-benchmark.sh B3     # override any knob
#
# BENCH_PROFILE applies the override block from tools/config.env for THIS run
# only, without touching the instance's .env - so switching between a quick
# shakeout and a real sweep costs nothing.
#
# Runs under nohup on the instance so a dropped SSH connection does not kill
# a multi-hour sweep, and tails the log. Re-attach after a disconnect with:
#   bash tools/05-run-benchmark.sh --tail
#
# Pulls results back to ./results/ at the end. Without that they would be
# destroyed with the instance, and 99-teardown.sh deletes the volumes.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config
ensure_ssh_key

REMOTE_LOG="${REMOTE_REPO}/tools-run.log"

# --tail re-attaches to a run already in flight.
if [[ "${1:-}" == "--tail" ]]; then
  PRIVATE_IP="$(state_require PRIVATE_IP)"
  ensure_ssh_key
  # Follow to the run's end when it is still going, otherwise just show the
  # log - `tail -f` on a finished run would hang with nothing to report.
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

# Validate arguments BEFORE touching the network. A typo'd benchmark id used
# to cost an SSH timeout first and only then get rejected.
targets=()
if (( $# > 0 )); then
  for t in "$@"; do
    case "${t}" in
      B1|B3|B4|B5|B7|B8|B9) targets+=("${t}") ;;
      *) die "unknown benchmark '${t}' (valid: B1 B3 B4 B5 B7 B8 B9)
     B2 and B6 were never implemented." ;;
    esac
  done
fi

# Resolve the profile here too, before any network work: a mistyped profile
# name used to surface only after an SSH wait, for the same reason a mistyped
# benchmark id did.
prof_block=""
case "${BENCH_PROFILE:-}" in
  "")      : ;;
  quick)   prof_block="${BENCH_ENV_OVERRIDES_QUICK}" ;;
  default) prof_block="${BENCH_ENV_OVERRIDES}" ;;
  *) die "unknown BENCH_PROFILE=${BENCH_PROFILE} (use 'quick' or 'default')" ;;
esac

PRIVATE_IP="$(state_require PRIVATE_IP)"
[[ "$(state_get TEMPLATES_READY || echo 0)" == "1" ]] || die "templates are not built.
     Run: bash tools/04-provision-templates.sh"

wait_for_ssh 300

# Knobs that can be overridden for a single run. Forwarded as environment
# variables, which genuinely win: load_env() in scripts/_lib.sh only sets a
# key that is not already set.
BENCH_KNOBS=(
  CUBE_STRATEGY CUBE_TRANSPORT CUBE_POOL_SIZE CUBE_CLONE_BATCH
  DURATION_SEC WARMUP_SEC COOLDOWN_SEC CONCURRENCIES
  THINK_TIME_MS THINK_TIME_JITTER_MS THINK_TIME_PAUSE
  CLIENT_NPROCS CLIENT_NPROCS_MAX SKIP
  B1_CONCURRENCIES B3_CONCURRENCIES B4_CONCURRENCIES B5_CONCURRENCIES
  B7_CONCURRENCIES B8_CONCURRENCIES B9_CONCURRENCIES
  B9_DURATION_SEC B8_TRIALS B8_CLONE_COUNT
  B1_EPISODE_STEPS B5_EPISODE_STEPS B7_EPISODES_PER_ROLLOUT
  TPCH_SF
)

# A profile can be applied HERE, without re-running 03-setup-env.sh. That
# matters because 03 also reinstalls the platform and rebuilds the container
# image, which is a lot of work just to change a concurrency list. The
# per-benchmark keys (B1_CONCURRENCIES, B8_TRIALS, ...) are why this exists:
# they are part of the profile, so forwarding only the global knobs would
# apply half of it.
prof_env=""
if [[ -n "${BENCH_PROFILE:-}" ]]; then
  [[ -n "${prof_block//[[:space:]]/}" ]] \
    || warn "BENCH_PROFILE=${BENCH_PROFILE} but its override block in tools/config.env is empty"
  while IFS= read -r line; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    # Only well-formed KEY=VALUE lines, so a stray line in config.env cannot
    # turn into something else on the remote command line.
    if [[ ! "${line}" =~ ^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      warn "ignoring unparseable profile line: ${line}"
      continue
    fi
    prof_env+="$(printf '%s=%q ' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}")"
  done <<< "${prof_block}"
  log "profile ${BENCH_PROFILE}: ${prof_env}"
fi

# Explicit shell variables come AFTER the profile, so they win: `env A=1 A=2`
# takes the last assignment.
shell_env=""
for v in "${BENCH_KNOBS[@]}"; do
  [[ -n "${!v:-}" ]] && shell_env+="$(printf '%s=%q ' "${v}" "${!v}")"
done
[[ -n "${shell_env}" ]] && log "inline overrides: ${shell_env}"

step "preflight: are the templates actually usable?"
# Cheap insurance. A reboot between 04 and 05 leaves templates reporting
# READY while create fails, and finding that out 20 minutes into a sweep
# wastes both the time and the instance-hours.
# shellcheck disable=SC2046
if ! ssh $(_ssh_opts) -n "$(ssh_host)" \
     "cd ${REMOTE_REPO} && .venv/bin/python orchestrator/smoke_test.py >/dev/null 2>&1"; then
  die "the smoke test fails, so the suite would too.
     Most likely the instance rebooted since tools/04-provision-templates.sh: Cubelet's local
     image registry is on tmpfs, so templates need rebuilding.
     Run: bash tools/04-provision-templates.sh"
fi
log "templates are usable"

if (( ${#targets[@]} > 0 )); then
  desc="${targets[*]}"
else
  desc="full suite (B1 B3 B4 B5 B7 B8 B9)"
fi

step "running: ${desc}"
warn "a full sweep at the default concurrency levels takes hours.
     Safe to disconnect: the run is detached. Re-attach with
       bash tools/05-run-benchmark.sh --tail"

# The work is done by a SCRIPT on the instance, not by a command string built
# here. Building the string is what broke this before: a benchmark banner's
# own single quotes closed the outer quoting early, the next word began with
# '#', and bash discarded the rest of the line as a comment - taking the
# redirect, the '&' and the confirmation echo with it. Nothing ran, and there
# was no log to say so.
# shellcheck disable=SC2046
scp $(_ssh_opts) -q "${TOOLS_DIR}/remote/run-bench.sh" \
  "$(ssh_host):${REMOTE_REPO}/tools/remote/run-bench.sh"

# Overrides go through `env K=V`, each value already quoted with %q, so a
# value containing anything unusual cannot re-open the quoting hole that broke
# this script before.
env_prefix="${prof_env}${shell_env}"
[[ -n "${env_prefix}" ]] && env_prefix="env ${env_prefix}"

# The redirections go on the OUTSIDE of the brace group, not on the inner
# command. `&` backgrounds the whole `cd && rm && setsid ...` chain in a
# subshell, and redirecting only the inner command left that subshell holding
# ssh's stdout and stderr - so ssh could not close the channel and blocked
# until the benchmark finished, hours later. Measured over real ssh: inner
# redirect only, ssh blocked >20s (until the payload ended); group redirect,
# ssh returned in 1s and the payload still ran to completion.
ssh_run "{ cd ${REMOTE_REPO} && rm -f tools-run.log tools-run.pid tools-run.rc && \
  setsid ${env_prefix}bash tools/remote/run-bench.sh ${targets[*]:-} ; } \
  >/dev/null 2>&1 </dev/null & echo launched"

# The launcher writes its PID before anything else, so waiting for that file
# is deterministic - no sleep-and-hope, and it catches a launch that failed.
REMOTE_PID=""
for _ in $(seq 1 30); do
  REMOTE_PID="$(ssh_run "cat ${REMOTE_REPO}/tools-run.pid 2>/dev/null" | tr -d '\r\n ' || true)"
  [[ -n "${REMOTE_PID}" ]] && break
  # A run short enough to finish before the first poll leaves rc but no pid.
  if ssh_run "test -f ${REMOTE_REPO}/tools-run.rc" 2>/dev/null; then break; fi
  sleep 1
done
if [[ -z "${REMOTE_PID}" ]] && ! ssh_run "test -f ${REMOTE_REPO}/tools-run.rc" 2>/dev/null; then
  die "the run never started: no ${REMOTE_REPO}/tools-run.pid appeared.
     Check by hand:
       ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} \\
         'cd ${REMOTE_REPO} && bash tools/remote/run-bench.sh ${targets[*]:-}'"
fi
[[ -n "${REMOTE_PID}" ]] && log "running as pid ${REMOTE_PID}"

# Follow the log until that specific PID exits. Liveness is `kill -0` on the
# recorded PID, never a pgrep pattern: `pgrep -f 'run-single.sh'` matched its
# OWN command line (the pattern is in it), so the old check was always true
# and the wait loop never ended.
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

# The real exit status, recorded by the launcher - not inferred from whether
# some artifact happens to exist.
RUN_RC="$(ssh_run "cat ${REMOTE_REPO}/tools-run.rc 2>/dev/null" | tr -d '\r\n ' || true)"
if [[ -z "${RUN_RC}" ]]; then
  warn "no exit status was recorded; the run may have been killed.
     Full log: bash tools/05-run-benchmark.sh --tail"
elif [[ "${RUN_RC}" != "0" ]]; then
  warn "the benchmark exited ${RUN_RC}. Results below may be incomplete.
     Full log: bash tools/05-run-benchmark.sh --tail"
else
  log "benchmark exited 0"
fi

rsync_results_down

step "leftovers on the instance"
# A clean run leaves nothing. Anything here means a leak worth knowing about
# before the next measurement, since a stray sandbox holds scheduler quota.
ssh_run "curl -s --max-time 10 http://127.0.0.1:3000/templates | python3 -c '
import json,sys
ts=json.load(sys.stdin)
snaps=[t[\"templateID\"] for t in ts if str(t.get(\"templateID\",\"\")).startswith(\"snap-\")]
print(\"    leftover snapshots:\", snaps or \"none\")
print(\"    templates READY  :\", sum(1 for t in ts if t.get(\"status\")==\"READY\"), \"/\", len(ts))'" || true
ssh_run "curl -s --max-time 10 'http://127.0.0.1:3000/v2/sandboxes?limit=500' | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(\"    leftover sandboxes:\", len(d) if isinstance(d,list) else d)'" || true

echo >&2
log "results are in ${REPO_DIR}/results/"
ls -1t "${REPO_DIR}"/results/*/summary_*.json 2>/dev/null | head -3 | sed 's/^/    /' >&2 || true
echo >&2
warn "the instance is still running and still billing.
     When you are done:  bash tools/99-teardown.sh"
