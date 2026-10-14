#!/usr/bin/env bash
#
# cube-bench-sweep.sh - repeat cube-bench create-only across a concurrency
# matrix, clean the node between every run, then aggregate into one report.
#
#   usage:  bash cube-bench-sweep.sh
#           REPS=3 MATRIX="1:20 10:200" bash cube-bench-sweep.sh
#
# Each (concurrency, total) pair runs REPS times. After every single run the
# node is drained and we POLL until the node's REPORTED quota usage is back to
# zero - a fixed sleep is not enough, because CubeMaster admits creates against
# a node metric refreshed only every 1s, so starting a run while the previous
# run's usage is still being reported silently changes the result.
#
set -uo pipefail

BENCH_DIR="${BENCH_DIR:-/home/ubuntu/CubeSandbox/examples/cube-bench}"
BENCH_BIN="${BENCH_BIN:-./bin/cube-bench}"
REPS="${REPS:-10}"
WARMUP="${WARMUP:-3}"
MATRIX="${MATRIX:-1:20 10:200 20:300}"
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-240}"
RUN_GAP="${RUN_GAP:-2}"

CUBEAPI="${E2B_API_URL:-http://127.0.0.1:3000}"
CUBEOPS="${CUBE_OPS_URL:-http://127.0.0.1:3010}"
OUTDIR="${OUTDIR:-${HOME}/cube-bench-sweep-$(date +%Y%m%d-%H%M%S)}"

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '%s  WARN: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '%s  ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- environment
cd "${BENCH_DIR}" || die "BENCH_DIR not found: ${BENCH_DIR}"
[[ -x "${BENCH_BIN}" ]] || die "cube-bench not executable at ${BENCH_DIR}/${BENCH_BIN}"
command -v cubemastercli >/dev/null || die "cubemastercli not on PATH"

mkdir -p "${OUTDIR}/runs" || die "cannot create ${OUTDIR}"

export E2B_API_URL="${CUBEAPI}"
export E2B_API_KEY="${E2B_API_KEY:-e2b_000000}"
# E2B_DEBUG would repoint the SDK data plane at localhost:49983; irrelevant for
# cube-bench but unset it so a stray export cannot affect anything downstream.
unset E2B_DEBUG

# Template: honour the environment, else take the single READY template.
if [[ -z "${CUBE_TEMPLATE_ID:-}" ]]; then
  CUBE_TEMPLATE_ID="$(cubemastercli tpl ls 2>/dev/null \
    | awk '$2=="READY"||$3=="READY"{print $1}' | grep -E '^tpl-' | head -1)"
fi
[[ -n "${CUBE_TEMPLATE_ID}" ]] || die "no READY template found; set CUBE_TEMPLATE_ID"
export CUBE_TEMPLATE_ID

instance_type() {
  local t
  t="$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)"
  curl -s -m 3 -H "X-aws-ec2-metadata-token: ${t}" \
    http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo unknown
}
INSTANCE_TYPE="$(instance_type)"; : "${INSTANCE_TYPE:=unknown}"

# Per-sandbox request, read from the template's node spec (e.g. cpu=2000m,mem=2000Mi).
TPL_CPU_MILLI="$(cubemastercli tpl info "${CUBE_TEMPLATE_ID}" 2>/dev/null \
  | grep -oE 'cpu=[0-9]+m' | head -1 | tr -dc '0-9')"
: "${TPL_CPU_MILLI:=0}"

# ------------------------------------------------------------------ node state
# echoes "<QuotaCpuUsage> <mvm_num> <QuotaCpu> <MaxMvmLimit>"; zeros when absent
# (every numeric field in the registry is omitempty, so an idle node omits them)
node_state() {
  curl -s -m 8 "${CUBEOPS}/internal/v1/nodes" 2>/dev/null | python3 -c '
import json,sys
try: n=json.load(sys.stdin)[0]
except Exception: print("ERR ERR ERR ERR"); sys.exit()
print(n.get("QuotaCpuUsage",0), n.get("mvm_num",0), n.get("QuotaCpu",0), n.get("MaxMvmLimit",0))
' 2>/dev/null || echo "ERR ERR ERR ERR"
}

listed() { cubemastercli list --all -q 2>/dev/null | grep -cE '^[0-9a-f]{8,}$'; }

drain() {
  local n
  n="$(listed)"
  (( n > 0 )) || return 0
  cubemastercli list --all -q 2>/dev/null \
    | xargs -r -n 25 -P 4 cubemastercli --timeout 300s cubebox destroy >/dev/null 2>&1
}

# Poll until the control plane AND the node metric both read empty.
wait_settled() {
  local deadline=$(( SECONDS + SETTLE_TIMEOUT )) l qu mv
  while (( SECONDS < deadline )); do
    l="$(listed)"
    read -r qu mv _ _ <<<"$(node_state)"
    if [[ "${l}" == "0" && "${qu}" == "0" && "${mv}" == "0" ]]; then
      return 0
    fi
    [[ "${l}" != "0" ]] && drain
    sleep 3
  done
  warn "node did not settle within ${SETTLE_TIMEOUT}s (listed=${l} QuotaCpuUsage=${qu} mvm_num=${mv})"
  return 1
}

# --------------------------------------------------------------------- banner
read -r _qu _mv QUOTA_CPU MAX_MVM <<<"$(node_state)"
CAP="n/a"
if [[ "${QUOTA_CPU}" =~ ^[0-9]+$ ]] && (( TPL_CPU_MILLI > 0 )) && (( QUOTA_CPU > 0 )); then
  CAP=$(( QUOTA_CPU / TPL_CPU_MILLI ))
fi

log "instance        ${INSTANCE_TYPE}  (nproc=$(nproc))"
log "template        ${CUBE_TEMPLATE_ID}  (cpu=${TPL_CPU_MILLI}m per sandbox)"
log "node quota      QuotaCpu=${QUOTA_CPU} milli   MaxMvmLimit=${MAX_MVM}"
log "steady-state    ${CAP} sandboxes before the cpu filter rejects"
log "matrix          ${MATRIX}   reps=${REPS}  warmup=${WARMUP}"
log "output          ${OUTDIR}"
echo

cat > "${OUTDIR}/meta.json" <<JSON
{
  "instance_type": "${INSTANCE_TYPE}",
  "nproc": $(nproc),
  "template": "${CUBE_TEMPLATE_ID}",
  "template_cpu_milli": ${TPL_CPU_MILLI},
  "quota_cpu_milli": ${QUOTA_CPU:-0},
  "max_mvm_limit": ${MAX_MVM:-0},
  "steady_state_cap": "${CAP}",
  "reps": ${REPS},
  "warmup": ${WARMUP},
  "matrix": "${MATRIX}"
}
JSON

# ----------------------------------------------------------------- the sweep
log "draining node before starting"
wait_settled || warn "starting anyway"

for pair in ${MATRIX}; do
  C="${pair%%:*}"; N="${pair##*:}"
  for (( rep = 1; rep <= REPS; rep++ )); do
    out="${OUTDIR}/runs/c${C}_n${N}_rep$(printf '%02d' "${rep}").json"

    read -r qu mv _ _ <<<"$(node_state)"
    printf '%s  [c=%-3s n=%-4s rep %2d/%-2d] pre: listed=%s QuotaCpuUsage=%s mvm_num=%s\n' \
      "$(date +%H:%M:%S)" "${C}" "${N}" "${rep}" "${REPS}" "$(listed)" "${qu}" "${mv}"

    "${BENCH_BIN}" -c "${C}" -n "${N}" -w "${WARMUP}" -m create-only \
                   -no-tui -o "${out}" >/dev/null 2>&1
    rc=$?
    if [[ ! -s "${out}" ]]; then
      warn "c=${C} n=${N} rep=${rep}: no report written (exit ${rc}); skipping"
    else
      python3 -c "
import json,sys
d=json.load(open('${out}'))
s=d.get('summary',{}); c=d.get('create',{})
print('           -> successful=%s/%s  total_time=%.3fs  qps=%.1f  create avg=%.1fms p99=%.1fms'
      % (s.get('successful'), d.get('config',{}).get('total'),
         s.get('total_time_s',0), s.get('throughput_qps',0),
         c.get('avg',0), c.get('p99',0)))
" 2>/dev/null || warn "could not parse ${out}"
    fi

    drain
    wait_settled || warn "c=${C} n=${N} rep=${rep}: node unsettled before next run"
    sleep "${RUN_GAP}"
  done
  echo
done

# ----------------------------------------------------------------- aggregate
log "aggregating"
python3 - "${OUTDIR}" <<'PY'
import json, glob, os, statistics as st, sys, unicodedata

def dw(s):
    """Display width: CJK full/wide chars occupy two terminal columns."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))

def pad(s, width):
    s = str(s)
    return s + " " * max(0, width - dw(s))

outdir = sys.argv[1]
meta = json.load(open(os.path.join(outdir, "meta.json")))
rows = {}

for f in sorted(glob.glob(os.path.join(outdir, "runs", "*.json"))):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    cfg, s, c = d.get("config", {}), d.get("summary", {}), d.get("create", {})
    key = (cfg.get("concurrency"), cfg.get("total"))
    if key[0] is None:
        continue
    succ = s.get("successful") or 0
    tt = s.get("total_time_s") or 0.0
    rows.setdefault(key, []).append({
        "successful": succ,
        "total_time_s": tt,
        "throughput_qps": s.get("throughput_qps") or 0.0,
        "avg": c.get("avg") or 0.0,
        "min": c.get("min") or 0.0,
        "p95": c.get("p95") or 0.0,
        "p99": c.get("p99") or 0.0,
        "max": c.get("max") or 0.0,
        # wall-clock cost amortised over the sandboxes actually delivered
        "per_sandbox_ms": (tt * 1000.0 / succ) if succ else float("nan"),
    })

FIELDS = ["successful", "total_time_s", "throughput_qps",
          "avg", "min", "p95", "p99", "max", "per_sandbox_ms"]
HDR = ["实例类型", "并发", "请求数", "successful", "total_time_s",
       "throughput_qps", "avg", "min", "p95", "p99", "max", "单沙箱均摊"]

def mean(v):
    return sum(v) / len(v) if v else float("nan")

table, csv = [], [",".join(HDR + ["reps", "successful_min", "successful_max", "successful_std"])]
for (c, n) in sorted(rows, key=lambda k: (k[0], k[1])):
    runs = rows[(c, n)]
    m = {k: mean([r[k] for r in runs]) for k in FIELDS}
    sl = [r["successful"] for r in runs]
    sd = st.stdev(sl) if len(sl) > 1 else 0.0
    table.append([meta["instance_type"], str(c), str(n),
                  "%.1f" % m["successful"], "%.3f" % m["total_time_s"],
                  "%.2f" % m["throughput_qps"], "%.2f" % m["avg"],
                  "%.2f" % m["min"], "%.2f" % m["p95"], "%.2f" % m["p99"],
                  "%.2f" % m["max"], "%.2f" % m["per_sandbox_ms"],
                  len(runs), min(sl), max(sl), "%.1f" % sd])
    csv.append(",".join(str(x) for x in table[-1]))

# fixed-width table
disp = [HDR + ["reps", "succ_min", "succ_max", "succ_std"]] + [[str(x) for x in r] for r in table]
w = [max(dw(r[i]) for r in disp) for i in range(len(disp[0]))]
lines = []
for i, r in enumerate(disp):
    lines.append("  ".join(pad(cell, w[j]) for j, cell in enumerate(r)).rstrip())
    if i == 0:
        lines.append("  ".join("-" * w[j] for j in range(len(w))))

report = []
report.append("# cube-bench create-only sweep")
report.append("")
report.append("instance        : %s (nproc=%s)" % (meta["instance_type"], meta["nproc"]))
report.append("template        : %s  (cpu=%sm per sandbox)" % (meta["template"], meta["template_cpu_milli"]))
report.append("node quota      : QuotaCpu=%s milli, MaxMvmLimit=%s" % (meta["quota_cpu_milli"], meta["max_mvm_limit"]))
report.append("steady-state cap: %s sandboxes" % meta["steady_state_cap"])
report.append("reps per point  : %s   (warmup %s, mode create-only)" % (meta["reps"], meta["warmup"]))
report.append("")
report.append("All latency columns are CREATE latency in ms, averaged over reps.")
report.append("单沙箱均摊 = total_time_s * 1000 / successful (ms of wall clock per delivered sandbox).")
report.append("")
report += lines
report.append("")

cap = meta["steady_state_cap"]
if str(cap).isdigit():
    over = [(c, n) for (c, n) in rows if n > int(cap)]
    if over:
        report.append("CAVEAT: these points request more sandboxes (n) than the node's steady-state")
        report.append("quota allows (%s). Everything above %s is admitted only because CubeMaster" % (cap, cap))
        report.append("gates on a node metric refreshed every 1s, so a burst slips past before the")
        report.append("filter closes. `successful` there measures that overshoot, not capacity.")
        for c, n in sorted(over):
            sl = [r["successful"] for r in rows[(c, n)]]
            spread = max(sl) - min(sl)
            verdict = ("stable across reps here, but it is still overshoot and can collapse "
                       "on a busier or freshly booted node" if spread == 0
                       else "NOT reproducible - do not use the mean")
            report.append("  c=%-3s n=%-4s successful min=%s max=%s spread=%s  <- %s"
                          % (c, n, min(sl), max(sl), spread, verdict))
        report.append("")

txt = "\n".join(report)
print(txt)
open(os.path.join(outdir, "report.txt"), "w").write(txt + "\n")
open(os.path.join(outdir, "report.csv"), "w").write("\n".join(csv) + "\n")
json.dump({"meta": meta, "per_point": {f"c{c}_n{n}": rows[(c, n)] for (c, n) in rows}},
          open(os.path.join(outdir, "report.json"), "w"), indent=2)
PY

echo
log "wrote ${OUTDIR}/report.txt  report.csv  report.json"
log "final: listed=$(listed)  node=$(node_state)"
