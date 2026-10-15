#!/usr/bin/env bash
#
# run.sh - collect core-to-core latency matrices from one or more machines.
#
#   bash run.sh                         # measure THIS machine
#   bash run.sh 10.0.0.1 10.0.0.2       # measure those hosts over SSH
#   WITH_RUST=1 bash run.sh 10.0.0.1    # also run the Rust core-to-core-latency tool
#
# Everything lands in $OUTDIR as, per machine <label>:
#   <label>.meta.txt       topology + instance type, needed to read the numbers
#   <label>.siblings.txt   cpu -> thread_siblings_list, so SMT pairs can be split out
#   <label>.c2c.csv        this repo's C ping-pong        (a,b,one_way_ns)
#   <label>.tool-b1.csv    Rust tool bench 1, CAS         (WITH_RUST=1 only)
#   <label>.tool-b2.csv    Rust tool bench 2, two lines   (WITH_RUST=1 only)
#   <label>.tool-b*.txt    the tool's own 96x96 tables
#
# Then:  python3 analyze.py $OUTDIR
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ITERS="${ITERS:-4000}"
REPS="${REPS:-3}"
WARMUP="${WARMUP:-2000}"
WITH_RUST="${WITH_RUST:-0}"
RUST_ITERS="${RUST_ITERS:-1000}"
RUST_SAMPLES="${RUST_SAMPLES:-100}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-}"
OUTDIR="${OUTDIR:-${PWD}/c2c-$(date +%Y%m%d-%H%M%S)}"

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '%s  WARN: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '%s  ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

[[ -f "${HERE}/c2c.c" ]] || die "c2c.c not found next to run.sh"
mkdir -p "${OUTDIR}" || die "cannot create ${OUTDIR}"

SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes)
[[ -n "${SSH_KEY}" ]] && SSH_OPTS+=(-i "${SSH_KEY}")

# The script that actually runs on the measured machine. Kept in one place so
# the local and remote paths cannot drift apart.
remote_payload() {
cat <<PAYLOAD
set -u
cd "\${TMPDIR:-/tmp}" || exit 1
command -v gcc >/dev/null || { echo "MISSING_GCC" >&2; exit 2; }
gcc -O2 -pthread -o c2c_bin c2c.c || { echo "COMPILE_FAILED" >&2; exit 3; }

{
  T=\$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)
  echo "instance_type : \$(curl -s -m 3 -H "X-aws-ec2-metadata-token: \$T" \
        http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo unknown)"
  echo "hostname      : \$(hostname)"
  echo "arch/kernel   : \$(uname -m) \$(uname -r)"
  echo "nproc         : \$(nproc)"
  echo "loadavg       : \$(cut -d' ' -f1-3 /proc/loadavg)"
  lscpu 2>/dev/null | grep -E '^(Model name|Socket|Core\(s\)|Thread\(s\)|NUMA node\(s\)|L1d|L2 |L3 )'
  lscpu 2>/dev/null | grep -E '^NUMA node[0-9]'
} > meta.out 2>&1

: > siblings.out
for d in /sys/devices/system/cpu/cpu[0-9]*; do
  c=\${d##*/cpu}
  if [ -f "\$d/topology/thread_siblings_list" ]; then
    echo "\$c \$(cat "\$d/topology/thread_siblings_list")" >> siblings.out
  else
    echo "\$c \$c" >> siblings.out
  fi
done

./c2c_bin ${ITERS} ${REPS} ${WARMUP} > c2c.out 2> c2c.err

if [ "${WITH_RUST}" = "1" ]; then
  if ! command -v core-to-core-latency >/dev/null && [ ! -x "\$HOME/.cargo/bin/core-to-core-latency" ]; then
    # --locked is required: without it dependency resolution picks a textwrap
    # that needs a newer rustc than distro cargo provides.
    command -v cargo >/dev/null && cargo install --locked core-to-core-latency >/dev/null 2>&1
  fi
  B=\$(command -v core-to-core-latency || echo "\$HOME/.cargo/bin/core-to-core-latency")
  if [ -x "\$B" ]; then
    for b in 1 2; do
      "\$B" -b \$b ${RUST_ITERS} ${RUST_SAMPLES} > tool-b\${b}.out 2>&1
      "\$B" -b \$b --csv ${RUST_ITERS} ${RUST_SAMPLES} > tool-b\${b}.csvout 2>&1
    done
  else
    echo "RUST_TOOL_UNAVAILABLE" >&2
  fi
fi
PAYLOAD
}

collect_local() {
  local label="$1" tmp
  tmp="$(mktemp -d)" || die "mktemp failed"
  cp "${HERE}/c2c.c" "${tmp}/c2c.c"
  log "[${label}] measuring locally in ${tmp}"
  ( export TMPDIR="${tmp}"; remote_payload | bash ) || warn "[${label}] payload returned non-zero"
  _gather "${label}" "${tmp}"
  rm -rf "${tmp}"
}

collect_remote() {
  local label="$1" tgt="${SSH_USER}@${2}"
  log "[${label}] measuring ${tgt}"
  scp "${SSH_OPTS[@]}" "${HERE}/c2c.c" "${tgt}:/tmp/c2c.c" >/dev/null 2>&1 \
    || { warn "[${label}] scp failed, skipping"; return 1; }
  remote_payload | ssh "${SSH_OPTS[@]}" "${tgt}" 'bash -s' \
    || warn "[${label}] payload returned non-zero"
  for f in meta.out siblings.out c2c.out c2c.err \
           tool-b1.out tool-b2.out tool-b1.csvout tool-b2.csvout; do
    scp "${SSH_OPTS[@]}" "${tgt}:/tmp/${f}" "${OUTDIR}/.stage_${f}" >/dev/null 2>&1 || true
  done
  _stage_to_final "${label}"
}

_gather() {  # local case: files already in $2
  local label="$1" dir="$2" f
  for f in meta.out siblings.out c2c.out c2c.err \
           tool-b1.out tool-b2.out tool-b1.csvout tool-b2.csvout; do
    [[ -f "${dir}/${f}" ]] && cp "${dir}/${f}" "${OUTDIR}/.stage_${f}"
  done
  _stage_to_final "${label}"
}

_stage_to_final() {
  local label="$1"
  mv -f "${OUTDIR}/.stage_meta.out"       "${OUTDIR}/${label}.meta.txt"     2>/dev/null
  mv -f "${OUTDIR}/.stage_siblings.out"   "${OUTDIR}/${label}.siblings.txt" 2>/dev/null
  mv -f "${OUTDIR}/.stage_c2c.out"        "${OUTDIR}/${label}.c2c.csv"      2>/dev/null
  mv -f "${OUTDIR}/.stage_tool-b1.out"    "${OUTDIR}/${label}.tool-b1.txt"  2>/dev/null
  mv -f "${OUTDIR}/.stage_tool-b2.out"    "${OUTDIR}/${label}.tool-b2.txt"  2>/dev/null
  mv -f "${OUTDIR}/.stage_tool-b1.csvout" "${OUTDIR}/${label}.tool-b1.csv"  2>/dev/null
  mv -f "${OUTDIR}/.stage_tool-b2.csvout" "${OUTDIR}/${label}.tool-b2.csv"  2>/dev/null
  rm -f "${OUTDIR}"/.stage_* 2>/dev/null

  local csv="${OUTDIR}/${label}.c2c.csv"
  if [[ -s "${csv}" ]]; then
    log "[${label}] $(( $(wc -l < "${csv}") - 1 )) pairs -> ${csv}"
  else
    warn "[${label}] no matrix produced"
  fi
}

log "iters=${ITERS} reps=${REPS} warmup=${WARMUP} with_rust=${WITH_RUST}"
log "outdir=${OUTDIR}"

if [[ $# -eq 0 ]]; then
  collect_local "$(hostname)"
else
  for h in "$@"; do
    collect_remote "${h//[^A-Za-z0-9._-]/_}" "${h}"
  done
fi

echo
log "done. analyse with:"
echo "    python3 ${HERE}/analyze.py ${OUTDIR}"
