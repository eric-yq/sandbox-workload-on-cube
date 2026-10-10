#!/usr/bin/env bash
# Shared helpers for tools/00..99-teardown.sh.
#
# These scripts run on your WORKSTATION and drive the metal instance over
# SSH. Nothing here is meant to be executed on the instance itself - that
# is what scripts/ is for. Keeping the split that way means one place to
# stand: you never have to remember which host you are on.
#
# State (instance id, private IP, volume ids) lives in tools/.state so the
# numbered scripts can be run independently, re-run, and resumed after an
# interruption. It is gitignored - it names real infrastructure.

set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TOOLS_DIR}/.." && pwd)"
# Overridable so several instances can be driven at once, each with its own
# state file: TOOLS_STATE=tools/.state.c8g bash tools/01-create-instance.sh c8g.metal-24xl
STATE_FILE="${TOOLS_STATE:-${TOOLS_DIR}/.state}"

# ---------------------------------------------------------------- logging
_c_red=$'\033[31m'; _c_yel=$'\033[33m'; _c_grn=$'\033[32m'; _c_off=$'\033[0m'
[[ -t 2 ]] || { _c_red=""; _c_yel=""; _c_grn=""; _c_off=""; }

log()  { echo "${_c_grn}==>${_c_off} $*" >&2; }
warn() { echo "${_c_yel}WARN:${_c_off} $*" >&2; }
die()  { echo "${_c_red}ERROR:${_c_off} $*" >&2; exit 1; }
step() { echo >&2; echo "${_c_grn}#### $* ####${_c_off}" >&2; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

need_cmd() {
  have_cmd "$1" || die "required command not found: $1
     install it and re-run (${2:-})"
}

# --------------------------------------------------------------- config
load_config() {
  local f="${TOOLS_DIR}/config.env"
  [[ -f "${f}" ]] || die "no ${f}
     run: cp tools/config.env.example tools/config.env && \$EDITOR tools/config.env"
  # shellcheck disable=SC1090
  source "${f}"

  : "${AWS_REGION:?set AWS_REGION in tools/config.env}"
  : "${INSTANCE_TYPE:?set INSTANCE_TYPE in tools/config.env}"
  : "${KEY_NAME:?set KEY_NAME in tools/config.env}"
  : "${SSH_KEY_PATH:?set SSH_KEY_PATH in tools/config.env}"
  : "${REGISTRY:?set REGISTRY in tools/config.env}"

  SSH_USER="${SSH_USER:-ubuntu}"
  ROOT_VOLUME_GB="${ROOT_VOLUME_GB:-100}"
  DATA_VOLUME_GB="${DATA_VOLUME_GB:-1024}"
  DATA_VOLUME_IOPS="${DATA_VOLUME_IOPS:-40000}"
  DATA_VOLUME_THROUGHPUT="${DATA_VOLUME_THROUGHPUT:-1000}"
  DATA_DEVICE_NAME="${DATA_DEVICE_NAME:-/dev/sdb}"
  DATA_MOUNT="${DATA_MOUNT:-/data}"
  INSTANCE_NAME="${INSTANCE_NAME:-arl-bench}"
  REMOTE_REPO="${REMOTE_REPO:-/home/${SSH_USER}/sandbox-workload-on-cube}"
  UBUNTU_RELEASE="${UBUNTU_RELEASE:-24.04}"
  # Every OPTIONAL setting gets a default here, so a config.env that simply
  # omits a line cannot crash a script under `set -u`. Relying on
  # config.env.example to carry an empty assignment is not enough - a
  # hand-written or trimmed config is normal, and the failure mode is an
  # "unbound variable" abort partway through, sometimes after an instance has
  # already been created. tests assert this list stays complete.
  VPC_ID="${VPC_ID:-}"
  SUBNET_ID="${SUBNET_ID:-}"
  AVAILABILITY_ZONE="${AVAILABILITY_ZONE:-}"
  SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"
  IAM_INSTANCE_PROFILE="${IAM_INSTANCE_PROFILE:-}"
  EXTRA_TAGS="${EXTRA_TAGS:-}"
  S3_BUCKET="${S3_BUCKET:-}"
  BENCH_ENV_OVERRIDES="${BENCH_ENV_OVERRIDES:-}"
  BENCH_ENV_OVERRIDES_QUICK="${BENCH_ENV_OVERRIDES_QUICK:-}"
  ASSOCIATE_PUBLIC_IP="${ASSOCIATE_PUBLIC_IP:-auto}"
  case "${ASSOCIATE_PUBLIC_IP}" in
    auto|yes|no) ;;
    *) die "ASSOCIATE_PUBLIC_IP must be auto, yes or no (got '${ASSOCIATE_PUBLIC_IP}')" ;;
  esac
}

aws_() { aws --region "${AWS_REGION}" "$@"; }

# --------------------------------------------------------- instance type
# An optional first argument overrides INSTANCE_TYPE from config.env, so the
# same config can drive a sweep across machine types without being edited:
#   bash tools/01-create-instance.sh m8g.metal-48xl
# Sets INSTANCE_TYPE and INSTANCE_TYPE_SOURCE (for honest logging).
resolve_instance_type() {
  local arg="${1:-}"
  if [[ -n "${arg}" ]]; then
    INSTANCE_TYPE="${arg}"
    INSTANCE_TYPE_SOURCE="command line"
  else
    INSTANCE_TYPE_SOURCE="tools/config.env"
  fi
  [[ -n "${INSTANCE_TYPE:-}" ]] || die "no instance type.
     Pass one as the first argument, or set INSTANCE_TYPE in tools/config.env."
  log "instance type: ${INSTANCE_TYPE} (from ${INSTANCE_TYPE_SOURCE})"
}

# Hard-fail on a type that cannot work, warn on things that only degrade the
# results. Sets TYPE_AZS to the AZs where it is offered.
#
# Shared by 00-preflight and 01-create-instance because a type given on the
# command line has not been through the preflight: validating only in the
# preflight would leave the argument path unchecked.
validate_instance_type() {
  local t="$1" rc=0
  TYPE_AZS=""

  # CubeSandbox needs /dev/kvm, which virtualised instance types do not
  # expose. This is not a preference - Cubelet cannot start without it.
  case "${t}" in
    *.metal|*.metal-*) log "ok: ${t} is bare metal" ;;
    *) warn "${t} is NOT bare metal. CubeSandbox needs /dev/kvm, which only
     .metal / .metal-NNxl types expose."; rc=1 ;;
  esac

  TYPE_AZS="$(aws_ ec2 describe-instance-type-offerings \
    --location-type availability-zone \
    --filters "Name=instance-type,Values=${t}" \
    --query 'InstanceTypeOfferings[].Location' --output text 2>/dev/null || true)"
  if [[ -n "${TYPE_AZS}" ]]; then
    log "ok: offered in ${TYPE_AZS}"
  else
    warn "${t} is not offered in any AZ of ${AWS_REGION}"; rc=1
  fi

  # A type absent from INSTANCE_PRICES still benchmarks fine, but every
  # cost-per-unit figure in the results comes out null.
  if ! grep -q "${t}=" "${REPO_DIR}/env.example" 2>/dev/null; then
    warn "${t} is not in INSTANCE_PRICES (env.example), so cost_per_1k_units_usd
     will be null in the results. Add its on-demand price to get cost figures."
  fi

  # The configured volume performance is only useful up to what the instance
  # can drive; over-provisioning past it is money spent on nothing.
  local iops
  iops="$(aws_ ec2 describe-instance-types --instance-types "${t}" \
    --query 'InstanceTypes[0].EbsInfo.EbsOptimizedInfo.MaximumIops' \
    --output text 2>/dev/null || true)"
  if [[ -n "${iops}" && "${iops}" != "None" ]] \
     && (( ${DATA_VOLUME_IOPS:-0} > iops )); then
    warn "DATA_VOLUME_IOPS=${DATA_VOLUME_IOPS} exceeds what ${t} can drive (${iops});
     the excess is billed and unusable."
  fi

  # Memory decides how many sandboxes fit, and the shipped concurrency
  # levels assume a 768 GiB .metal-48xl.
  local mem_mib
  mem_mib="$(aws_ ec2 describe-instance-types --instance-types "${t}" \
    --query 'InstanceTypes[0].MemoryInfo.SizeInMiB' --output text 2>/dev/null || true)"
  if [[ -n "${mem_mib}" && "${mem_mib}" != "None" ]]; then
    local mem_gib=$(( mem_mib / 1024 ))
    log "ok: $(aws_ ec2 describe-instance-types --instance-types "${t}" \
              --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null) vCPU / ${mem_gib} GiB"
    if (( mem_gib < 700 )); then
      warn "${mem_gib} GiB is well under the 768 GiB the default concurrency
     levels assume. The node sandbox ceiling scales with memory (125 GiB gave
     313 on the c7g.metal trial), so 512/1024 will not be reachable - use
     BENCH_PROFILE=quick, or lower CONCURRENCIES in tools/config.env."
    fi
  fi

  return ${rc}
}

# ------------------------------------------------- this machine's network
# The default placement for the benchmark instance is "wherever this
# workstation is": same VPC, same subnet, same security groups. That makes
# reachability a property of the construction rather than something to hope
# for - a security group whose inbound rule is self-referencing (which the
# VPC default group's is) permits traffic precisely because both ends are in
# it.
#
# Populates SELF_* and returns 1 when this is not an EC2 instance, so the
# caller can fall back instead of dying.
SELF_DISCOVERED=0
discover_self_network() {
  (( SELF_DISCOVERED )) && return 0
  local tok mac base
  tok="$(curl -s --max-time 3 -X PUT "http://169.254.169.254/latest/api/token" \
          -H "X-aws-ec2-metadata-token-ttl-seconds: 120" 2>/dev/null || true)"
  # IMDSv2 only: v1 is disabled on hardened AMIs and a v1 read would
  # silently return an HTML error page rather than failing.
  [[ ${#tok} -gt 20 ]] || return 1
  local H="X-aws-ec2-metadata-token: ${tok}"
  local md="http://169.254.169.254/latest/meta-data"
  mac="$(curl -s --max-time 3 -H "${H}" "${md}/mac" 2>/dev/null || true)"
  [[ -n "${mac}" ]] || return 1
  base="${md}/network/interfaces/macs/${mac}"

  SELF_INSTANCE_ID="$(curl -s --max-time 3 -H "${H}" "${md}/instance-id" || true)"
  SELF_AZ="$(curl -s --max-time 3 -H "${H}" "${md}/placement/availability-zone" || true)"
  SELF_PRIVATE_IP="$(curl -s --max-time 3 -H "${H}" "${md}/local-ipv4" || true)"
  SELF_VPC_ID="$(curl -s --max-time 3 -H "${H}" "${base}/vpc-id" || true)"
  SELF_SUBNET_ID="$(curl -s --max-time 3 -H "${H}" "${base}/subnet-id" || true)"
  # Newline-separated in IMDS; collapse to a single space-separated line.
  SELF_SG_IDS="$(curl -s --max-time 3 -H "${H}" "${base}/security-group-ids" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')"
  SELF_SG_NAMES="$(curl -s --max-time 3 -H "${H}" "${base}/security-groups" 2>/dev/null | tr '\n' ',' | sed 's/,$//')"

  [[ -n "${SELF_VPC_ID}" && -n "${SELF_SUBNET_ID}" ]] || return 1
  SELF_DISCOVERED=1
  return 0
}

# Would a host at $2 (private IP), holding security groups $3, be able to
# SSH to an instance carrying security groups $1?
#
# Worth checking rather than assuming: sharing a security group only helps
# if that group actually has a self-referencing inbound rule. Prints a
# verdict and returns nonzero when no rule can be found, so the failure
# shows up here instead of as an unexplained SSH timeout.
verify_ssh_reachable() {
  local target_sgs="$1" src_ip="$2" src_sgs="$3"
  local rules rc
  # shellcheck disable=SC2086
  rules="$(aws_ ec2 describe-security-groups --group-ids ${target_sgs} \
            --query 'SecurityGroups[].{id:GroupId,name:GroupName,perms:IpPermissions}' \
            --output json 2>/dev/null || echo '[]')"
  set +e
  # shellcheck disable=SC2086
  SSH_REACH_WHY="$(printf '%s' "${rules}" \
    | python3 "${TOOLS_DIR}/sg_reachable.py" "${src_ip}" ${src_sgs} 2>&1)"
  rc=$?
  set -e
  return ${rc}
}

# ---------------------------------------------------------------- state
# Flat KEY=VALUE. Rewritten in place so a re-run of 01 updates rather than
# appends, which would otherwise leave two instance ids in the file.
state_set() {
  local key="$1" val="$2" tmp
  touch "${STATE_FILE}"
  chmod 600 "${STATE_FILE}"
  tmp="$(mktemp)"
  grep -v "^${key}=" "${STATE_FILE}" > "${tmp}" 2>/dev/null || true
  echo "${key}=${val}" >> "${tmp}"
  mv "${tmp}" "${STATE_FILE}"
  chmod 600 "${STATE_FILE}"
}

state_get() {
  [[ -f "${STATE_FILE}" ]] || return 1
  local line
  line="$(grep -m1 "^${1}=" "${STATE_FILE}" 2>/dev/null)" || return 1
  echo "${line#*=}"
}

state_require() {
  local key="$1" val
  val="$(state_get "${key}")" || die "${key} is not in ${STATE_FILE}.
     Run the earlier steps first (tools/01-create-instance.sh creates the instance)."
  [[ -n "${val}" ]] || die "${key} is empty in ${STATE_FILE}"
  echo "${val}"
}

state_show() {
  [[ -f "${STATE_FILE}" ]] && sed 's/^/    /' "${STATE_FILE}" >&2 || echo "    (no state yet)" >&2
}

# ------------------------------------------------------------------- ssh
# The key has to be mode 600 or OpenSSH refuses it outright. Shared pem
# files routinely arrive as 0644/0755, so rather than telling the user to
# chmod a file they may share with other tooling, keep a private copy.
ensure_ssh_key() {
  [[ -f "${SSH_KEY_PATH}" ]] || die "SSH key not found: ${SSH_KEY_PATH}"
  local mode
  mode="$(stat -c '%a' "${SSH_KEY_PATH}")"
  if [[ "${mode}" == "600" || "${mode}" == "400" ]]; then
    SSH_KEY="${SSH_KEY_PATH}"
    return 0
  fi
  local priv="${HOME}/.ssh/arl-bench-$(basename "${SSH_KEY_PATH}")"
  mkdir -p "${HOME}/.ssh"; chmod 700 "${HOME}/.ssh"
  if [[ ! -f "${priv}" ]] || ! cmp -s "${SSH_KEY_PATH}" "${priv}"; then
    cp "${SSH_KEY_PATH}" "${priv}"
    log "copied key to ${priv} (source is mode ${mode}; ssh requires 600)"
  fi
  chmod 600 "${priv}"
  SSH_KEY="${priv}"
}

_ssh_opts() {
  # No known-hosts churn: these instances are created and destroyed
  # constantly and their host keys are new every time.
  echo -n "-i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
  echo -n " -o LogLevel=ERROR -o ConnectTimeout=15 -o ServerAliveInterval=30"
  echo -n " -o ServerAliveCountMax=20"
}

ssh_host() { echo "${SSH_USER}@$(state_require PRIVATE_IP)"; }

# Run a command on the instance. Stdin is closed so a remote sudo prompt
# cannot silently hang the script.
ssh_run() {
  # shellcheck disable=SC2046
  ssh $(_ssh_opts) -n "$(ssh_host)" "$@"
}

# Run a local script file remotely. Used instead of heredocs-over-ssh,
# which silently produce no output often enough to be untrustworthy.
#
# Set SSH_SCRIPT_ENV to a "K=V K=V" string to export variables for the
# remote run; ssh does not forward the local environment, and SendEnv needs
# server-side AcceptEnv, so pass them explicitly on the command line.
ssh_script() {
  local script="$1"; shift
  local remote="/tmp/arl-$(basename "${script}")"
  local prefix=""
  [[ -n "${SSH_SCRIPT_ENV:-}" ]] && prefix="env ${SSH_SCRIPT_ENV} "
  # shellcheck disable=SC2046
  scp $(_ssh_opts) -q "${script}" "$(ssh_host):${remote}"
  # shellcheck disable=SC2046
  ssh $(_ssh_opts) -n "$(ssh_host)" "${prefix}bash ${remote} $*"
}

wait_for_ssh() {
  local deadline=$(( SECONDS + ${1:-600} ))
  log "waiting for SSH on $(state_require PRIVATE_IP)"
  while (( SECONDS < deadline )); do
    # shellcheck disable=SC2046
    if ssh $(_ssh_opts) -o ConnectTimeout=5 -n "$(ssh_host)" true 2>/dev/null; then
      log "SSH is up"
      return 0
    fi
    sleep 5
  done
  die "SSH did not come up within ${1:-600}s.
     Check the security group allows 22 from this host, and that the
     instance is in a subnet this host can reach:
       aws ec2 describe-instances --instance-ids $(state_get INSTANCE_ID)"
}

# Ubuntu's first boot runs cloud-init and unattended-upgrades, both of
# which hold the apt lock. Installing Docker before they finish fails with
# "Could not get lock /var/lib/dpkg/lock-frontend", so every path that
# touches apt waits here first.
wait_for_cloud_init() {
  log "waiting for cloud-init and any first-boot apt work to finish"
  ssh_run "sudo cloud-init status --wait >/dev/null 2>&1 || true"
  ssh_run "
    for i in \$(seq 1 120); do
      if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 ||
         sudo fuser /var/lib/apt/lists/lock  >/dev/null 2>&1; then
        sleep 5
      else
        exit 0
      fi
    done
    echo 'apt lock still held after 10 minutes' >&2; exit 1
  " || die "apt is still locked on the instance.
     Something is holding it; check: ssh \$(tools/ssh.sh) 'ps aux | grep -i apt'"
  log "instance is ready for package installs"
}

# ------------------------------------------------------------------ sync
rsync_repo_up() {
  need_cmd rsync
  log "syncing repo -> $(state_require PRIVATE_IP):${REMOTE_REPO}"
  ssh_run "mkdir -p ${REMOTE_REPO}"
  # --delete keeps the remote a faithful copy so a stale file from an
  # earlier iteration cannot change behaviour. .env and results are
  # excluded because they are generated ON the instance.
  rsync -az --delete \
    --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
    --exclude 'results' --exclude '.env' --exclude 'tools/.state' \
    --exclude 'tools/config.env' \
    -e "ssh $(_ssh_opts)" \
    "${REPO_DIR}/" "$(ssh_host):${REMOTE_REPO}/"
}

rsync_results_down() {
  local dest="${REPO_DIR}/results"
  mkdir -p "${dest}"
  log "pulling results -> ${dest}"
  rsync -az -e "ssh $(_ssh_opts)" \
    "$(ssh_host):${REMOTE_REPO}/results/" "${dest}/" || {
      warn "no results pulled (did the run produce any?)"
      return 0
    }
}

# ------------------------------------------------------------------ misc
confirm() {
  local prompt="$1"
  if [[ "${ASSUME_YES:-0}" == "1" ]]; then
    log "${prompt} -> yes (ASSUME_YES=1)"
    return 0
  fi
  read -r -p "${prompt} [y/N] " ans
  [[ "${ans}" == "y" || "${ans}" == "Y" ]]
}

hourly_price() {
  # Best effort, for the cost warning only.
  local t="$1"
  grep -o "${t}=[0-9.]*" "${REPO_DIR}/env.example" 2>/dev/null | head -1 | cut -d= -f2
}
