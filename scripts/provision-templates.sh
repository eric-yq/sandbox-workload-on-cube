#!/usr/bin/env bash
# Create the CubeSandbox templates the benchmark runs from.
#
# All seven templates are built from ONE image (workers/arl-sandbox) and
# differ only in which services their entrypoint launches, plus their
# per-sandbox CPU/memory shape.
#
# Why the HTTP API and not cubemastercli
# --------------------------------------
# `cubemastercli tpl create-from-image` can do this, but the REST API is
# what the orchestrator already speaks, it needs no CLI on PATH, and it
# gives us the raw job/status objects to poll. One call does both create
# and build:
#
#   POST /templates                                  -> templateID, jobID (202)
#   GET  /templates/{tid}                            -> poll status until READY
#
# Note this is one call where AgentENV needed two (create then start
# build), and there is no separate buildID to track for the create path.
#
# The template probe is the load-bearing part
# -------------------------------------------
# Cube boots the image in a temporary microVM, polls `probePort`+`probePath`
# until it answers 2xx, and only *then* freezes the snapshot. Everything
# running at that moment is captured, so every sandbox resumes with its
# stack already live. That is what makes B5 fast: `dbgen` runs once here,
# during the build, and each rollout resumes with the TPC-H tables already
# in memory instead of spending ~30s regenerating them.
#
# Because Cube probes exactly one endpoint, templates that run more than
# one service probe the aggregate readiness gate on 8010
# (workers/arl-sandbox/readyz.py), which answers 2xx only once all of their
# services do. Single-service templates probe their own port directly and
# do not run the aggregator at all.
#
# Idempotent: an existing template with the same alias is deleted first, so
# re-running picks up a new image.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

: "${REGISTRY:?set REGISTRY in .env}"
: "${IMAGE_TAG:=v2}"
CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"
CUBE_API_KEY="${CUBE_API_KEY:-}"
BUILD_TIMEOUT="${TEMPLATE_BUILD_TIMEOUT:-1800}"
# Writable layer per sandbox. The workloads write very little (B1's temp
# files, B4's browser profile), but the layer is also where a rollout's
# CoW pages land.
WRITABLE_LAYER_SIZE="${SANDBOX_WRITABLE_LAYER_SIZE:-1G}"
# Readiness aggregator port; must match workers/arl-sandbox/readyz.py and
# PORT_READY in orchestrator/rollout.py.
READY_PORT="${READY_PORT:-8010}"

# Arch-qualified: a native build pushed to a bare tag would be clobbered by
# a host of the other architecture sharing this registry. See
# sandbox_image_ref() in _lib.sh.
IMAGE="$(sandbox_image_ref)"

need_cmd curl
need_cmd python3

api() {
  local method="$1" path="$2" body="${3:-}"
  local -a auth=()
  # CubeAPI runs unauthenticated when neither CUBE_API_KEY nor
  # AUTH_CALLBACK_URL is configured, which is the one-click default.
  # Sending an empty header would be worse than sending none.
  if [[ -n "${CUBE_API_KEY}" ]]; then
    auth=(-H "Authorization: Bearer ${CUBE_API_KEY}" -H "X-API-Key: ${CUBE_API_KEY}")
  fi
  if [[ -n "${body}" ]]; then
    curl -fsS -X "${method}" "${auth[@]}" \
      -H 'Content-Type: application/json' -d "${body}" \
      "${CUBE_API_URL}${path}"
  else
    curl -fsS -X "${method}" "${auth[@]}" "${CUBE_API_URL}${path}"
  fi
}

jget() {
  # jget <json> <dotted.path> - stdlib only, no jq dependency.
  python3 -c '
import json, sys
doc = json.loads(sys.argv[1] or "{}")
for key in sys.argv[2].split("."):
    if isinstance(doc, list):
        doc = doc[int(key)] if doc else None
    elif isinstance(doc, dict):
        doc = doc.get(key)
    else:
        doc = None
    if doc is None:
        break
print(doc if doc is not None else "")
' "$1" "$2"
}

# ------------------------------------------------------------------
# Template definitions
#
#   bid | alias | services | exposed ports
#
# Ports: b1=8001 b3=8003 b4=8004 b5=8005 b7=8007 static=80 readyz=8010
#
# The probe port is derived below: the single service's own port when there
# is one service, otherwise the readiness aggregator.
#
# B5 gets the most memory of the single-stack templates because DuckDB
# holds the whole TPC-H sf=1 dataset in memory; B4 needs headroom for
# Chromium; B9 carries all five stacks at once.
#
# B9S is the same image and services as B9 at 1 vCPU / 1 GiB, built under
# its own alias so it never replaces arl-b9 - the two are meant to be
# compared, not swapped. It exists because the node admits
# `QuotaCpu / template_cpu` sandboxes, so B9's 4 vCPU shape caps a
# 192,000-millicore host at 48 sandboxes. That is nowhere near enough to
# load the host when each session idles through a think-time delay, which
# is the hosting archetype. At 1 vCPU the same host admits 192.
#
# Memory, not CPU, is what cannot be cut, and it keeps the full 4096 MiB.
# Only the vCPU count drops. That was established by measurement, in two
# stages, and both stages are worth knowing because each failure mode
# hides the other:
#
#  1. Build. At 1024 MiB the build fails in CREATING_TEMPLATE on both
#     architectures - the rootfs artifact distributes fine (1/1 ready),
#     then the microVM never gets :8010/healthz to answer and the
#     snapshot deadline expires. Bisecting separated the dimensions: 1
#     vCPU with 2048 MiB builds, 2 vCPU with 1024 MiB does not.
#  2. Load. 2048 MiB then *builds and smoke-tests clean* and still fails
#     ~98% of rollouts under a real session, because B5's DuckDB gets
#     killed mid-query and CubeProxy starts returning 502 for :8005.
#     The build gate and the smoke test each touch a service once, so
#     neither can see a service that dies only once TPC-H queries start
#     allocating. The standalone arl-b5 template is given 3072 MiB for
#     DuckDB *alone*; B9 also carries Chromium and three Python
#     services, hence 4096.
#
# Density still improves, just less than the vCPU arithmetic suggests.
# The node admits min(QuotaCpu/cpu, QuotaMem/mem) sandboxes:
#   arl-b9       4 vCPU / 4096 MiB -> min(48, 117)  =  48
#   arl-b9-small 1 vCPU / 4096 MiB -> min(192, 117) = 117
# So the cap moves from CPU-bound 48 to memory-bound 117 (2.4x) and
# cutting vCPU further buys nothing. Going past 117 needs a smaller
# workload, not a smaller template.
#
# Because a 1 vCPU guest cold-starts all five stacks in more than the 30s
# CubeMaster allows (cubelet_conf.common_timeout_insec in
# CubeMaster/conf.yaml), building this template needs that raised
# temporarily. It gates template build only - sandbox create from the
# finished snapshot is ~85ms - so raise it, build, and put it back.
#
# Select the template at run time with TEMPLATE_B9=arl-b9-small.
# ------------------------------------------------------------------
templates_spec() {
  cat <<'SPEC'
B1|arl-b1|b1|8001
B3|arl-b3|b3|8003
B4|arl-b4|b4 static|8004 80
B5|arl-b5|b5|8005
B7|arl-b7|b7|8007
B8|arl-b8|b3|8003
B9|arl-b9|b1 b3 b4 b5 b7 static|8001 8003 8004 8005 8007 80
B9S|arl-b9-small|b1 b3 b4 b5 b7 static|8001 8003 8004 8005 8007 80
SPEC
}

env_or() {
  # env_or VAR default
  local val="${!1:-}"
  echo "${val:-$2}"
}

count_words() { echo $#; }

# Fetch one template's entry out of GET /templates.
#
# Used instead of GET /templates/{tid} for status polling: the detail route
# 404s for a template whose build failed (CubeMaster removes the definition
# row but leaves the collection entry with status=FAILED), and it is the
# collection that carries `lastError`.
template_entry() {
  local tid="$1"
  api GET /templates 2>/dev/null | python3 -c '
import json, sys
tid = sys.argv[1]
try:
    for t in json.load(sys.stdin):
        if t.get("templateID") == tid:
            print(json.dumps(t))
            break
    else:
        print("{}")
except Exception:
    print("{}")
' "${tid}" 2>/dev/null || echo '{}'
}

delete_existing() {
  local alias="$1"
  # Templates accept an alias wherever an ID is accepted. `sync=true` waits
  # for the delete to finish, so a rebuild cannot race the old template's
  # replica cleanup.
  if api DELETE "/templates/${alias}?sync=true" >/dev/null 2>&1; then
    echo "    [replace] removed existing template ${alias}"
  fi
}

provision_one() {
  local bid="$1" alias="$2" services="$3" ports="$4"
  local vcpu mem cpu_milli probe_port nservices

  vcpu="$(env_or "SANDBOX_VCPU_${bid}" 2)"
  mem="$(env_or "SANDBOX_MEM_MB_${bid}" 1024)"
  # Cube takes CPU in millicores, not whole vCPU: 2 vCPU is 2000.
  cpu_milli=$(( vcpu * 1000 ))

  # shellcheck disable=SC2086
  nservices="$(count_words ${services})"
  if (( nservices > 1 )); then
    probe_port="${READY_PORT}"
    ports="${ports} ${READY_PORT}"
  else
    probe_port="${ports%% *}"
  fi

  echo "==> ${alias}: ${vcpu} vCPU (${cpu_milli}m) / ${mem} MiB / services: ${services}"
  echo "    probe: :${probe_port}/healthz   exposed: ${ports}"
  delete_existing "${alias}"

  local body resp tid job
  body="$(python3 -c '
import json, sys
alias, image, services, ports = sys.argv[1:5]
cpu_milli, memory, probe_port, layer = sys.argv[5:9]
registry_user, registry_pass = sys.argv[9:11]
body = {
    "name": alias,
    "image": image,
    # The image entrypoint selects which services to start. Cube maps
    # `command` onto ENTRYPOINT and `args` onto CMD.
    "command": ["/app/start.sh"],
    "args": services.split(),
    "cpu": int(cpu_milli),
    "memory": int(memory),
    # Every port we may proxy into has to be declared.
    "exposedPorts": [int(p) for p in ports.split()],
    # Snapshot gate: Cube polls this until 2xx, then freezes.
    "probePort": int(probe_port),
    "probePath": "/healthz" if int(probe_port) != 80 else "/",
    "writableLayerSize": layer,
    # The workloads are entirely self-contained inside the sandbox (the B4
    # target SPA is served by nginx in the same microVM), so outbound
    # internet is not needed and leaving it off keeps a network hiccup from
    # showing up as workload latency.
    "allowInternetAccess": False,
}
if registry_user:
    body["registryUsername"] = registry_user
    body["registryPassword"] = registry_pass
print(json.dumps(body))
' "${alias}" "${IMAGE}" "${services}" "${ports}" "${cpu_milli}" "${mem}" \
  "${probe_port}" "${WRITABLE_LAYER_SIZE}" \
  "${REGISTRY_USERNAME:-}" "${REGISTRY_PASSWORD:-}")"

  resp="$(api POST /templates "${body}")"
  tid="$(jget "${resp}" templateID)"
  job="$(jget "${resp}" jobID)"
  if [[ -z "${tid}" ]]; then
    echo "ERROR: template create returned no templateID: ${resp}" >&2
    exit 1
  fi
  echo "    templateID=${tid} jobID=${job}"
  echo "    build submitted; waiting (timeout ${BUILD_TIMEOUT}s)"

  local waited=0 status last_err
  while (( waited < BUILD_TIMEOUT )); do
    # Read status from the list, not from GET /templates/{tid}.
    # CubeMaster deletes the template *definition* row when a build fails
    # but keeps the template in the collection with status=FAILED, so the
    # detail route answers 404 "template not found" for exactly the case we
    # most need to detect. Polling it alone means a failed build looks
    # identical to one still starting up, and the loop burns the whole
    # BUILD_TIMEOUT before reporting anything.
    resp="$(template_entry "${tid}")"
    status="$(jget "${resp}" status)"
    case "${status^^}" in
      READY)
        echo "    [ok]      ${alias} ready"
        return 0
        ;;
      ERROR|FAILED)
        last_err="$(jget "${resp}" lastError)"
        echo "ERROR: build of ${alias} failed: ${last_err:-<no lastError>}" >&2
        echo "       full status: ${resp}" >&2
        if [[ -n "${job}" ]]; then
          echo "       build logs: cubemastercli tpl watch --job-id ${job}" >&2
        fi
        exit 1
        ;;
    esac
    sleep 5
    waited=$(( waited + 5 ))
  done
  echo "ERROR: build of ${alias} did not finish within ${BUILD_TIMEOUT}s (last status: ${status:-unknown})" >&2
  if [[ -n "${job}" ]]; then
    echo "       still running? check: cubemastercli tpl watch --job-id ${job}" >&2
  fi
  echo "       a probe that never answers 2xx is the usual cause - the build" >&2
  echo "       then times out rather than failing fast. Confirm the image" >&2
  echo "       serves :${probe_port}/healthz." >&2
  exit 1
}

main() {
  echo "==> CubeAPI: ${CUBE_API_URL}"
  if ! curl -fsS -o /dev/null "${CUBE_API_URL}/health"; then
    echo "ERROR: CubeAPI is not healthy at ${CUBE_API_URL}" >&2
    echo "       run: sudo bash scripts/ensure-cube.sh" >&2
    exit 1
  fi

  # Get our own credentials rather than relying on the caller. This script
  # is the documented recovery when a host reboot wipes Cubelet's
  # tmpfs-backed local image registry, and it gets run standalone far more
  # often than through setup-host.sh. Without this, a standalone run sends
  # no credential, Cube's native export falls back to the host's docker
  # credential store, and a >12h-old ECR token there fails the pull with
  # "authorization token has expired" - several minutes in, and attributed
  # to the template rather than to the login. Idempotent: it returns
  # immediately if REGISTRY_USERNAME is already set.
  export_registry_credentials

  echo "==> source image: ${IMAGE}"

  local only="${1:-}"
  while IFS='|' read -r bid alias services ports; do
    [[ -z "${bid}" ]] && continue
    if [[ -n "${only}" && "${only}" != "${bid}" ]]; then
      continue
    fi
    provision_one "${bid}" "${alias}" "${services}" "${ports}"
  done <<< "$(templates_spec)"

  echo "==> templates:"
  api GET /templates 2>/dev/null | python3 -c '
import json, sys
try:
    for t in json.load(sys.stdin):
        aliases = ",".join(t.get("aliases") or t.get("names") or []) or "-"
        tid = (t.get("templateID") or "")[:20]
        print("    %-10s %-22s %s" % (aliases, tid, t.get("status", "?")))
except Exception as e:
    print("    (could not parse template list: %r)" % (e,))
' || true
}

main "$@"
