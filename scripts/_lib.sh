#!/usr/bin/env bash
# Shared helpers. Intended to be sourced, not executed.

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

have_cmd() { command -v "$1" >/dev/null 2>&1; }

need_cmd() {
  have_cmd "$1" || die "required command not found: $1"
}

# Match a pattern in a command's output WITHOUT a pipeline.
#
# `cmd | grep -q pat` is unsafe in these scripts because they all run under
# `set -o pipefail`: grep -q exits at the first match, the producer then dies
# of SIGPIPE (141), and pipefail promotes that to the pipeline's status - so
# the test reports FAILURE precisely when the pattern was found.
#
# This cost real time on a live host. `cube_installed()` used
# `systemctl list-unit-files | grep -q ...`, so it always answered "not
# installed" and every re-run of setup-host.sh re-downloaded and re-installed
# CubeSandbox; the same shape silently skipped the systemd LimitNOFILE
# drop-ins. Capture first, then match a here-string (no producer to kill).
#
#   out_matches "$(systemctl list-unit-files)" '^cube-sandbox-cube-api\.service'
out_matches() {
  local haystack="$1" pattern="$2"
  grep -qE "${pattern}" <<< "${haystack}"
}

# Install the AWS CLI v2 if it is missing.
#
# Arch suffix for the sandbox image tag.
#
# build-all.sh does a NATIVE single-arch `docker build` (the buildx
# container driver cannot push to a localhost registry, which is why the
# multi-arch `imagetools create` step from the Docker-era suite is gone).
# Cube pulls this image once, at template-build time, and converts it to a
# microVM rootfs; it is not a runtime dependency of any sandbox.
# That makes the pushed tag arch-specific in content but not in name, so
# an x86_64 host and an aarch64 host sharing one registry overwrite each
# other's manifest: the later push wins and the other host's template
# build then fails with
#   no OCI manifest entry matches linux/arm64; available: [linux/amd64, ...]
#
# Qualifying the tag with the host arch keeps concurrent runs on different
# instance types independent, which is what the one-instance-per-run
# workflow needs. (A real manifest list would also work but requires both
# builds to exist before composing, i.e. coordination between hosts.)
image_arch_suffix() {
  case "$(uname -m)" in
    x86_64|amd64)  echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) die "unsupported host arch $(uname -m)" ;;
  esac
}

# The single image reference that build-all.sh and provision-templates.sh
# must agree on. Both call this rather than assembling it themselves.
sandbox_image_ref() {
  : "${REGISTRY:?REGISTRY is not set}"
  echo "${REGISTRY}/arl-sandbox:${IMAGE_TAG:-v2}-$(image_arch_suffix)"
}

# `python3 -m venv` needs ensurepip, which Debian/Ubuntu split out into a
# separate python3-venv package that minimal cloud images do not install.
# Without this, venv creation dies with "ensurepip is not available" *after*
# it has already laid down a half-built .venv/, so the retry then skips
# creation and fails on pip instead. Check and install up front.
ensure_venv_support() {
  if python3 -c 'import ensurepip' 2>/dev/null; then
    return 0
  fi

  local sudo_cmd=""
  [[ "${EUID}" -ne 0 ]] && sudo_cmd="sudo"

  if have_cmd apt-get; then
    # Ubuntu names it after the exact minor version (python3.12-venv). The
    # unversioned python3-venv is a metapackage that may lag, so try the
    # versioned name first and fall back.
    local pyver pkg
    pyver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
    log "python3 -m venv is unavailable; installing python${pyver}-venv"
    ${sudo_cmd} apt-get update -y >/dev/null 2>&1 || true
    for pkg in "python${pyver}-venv" "python3-venv"; do
      [[ -n "${pyver}" ]] || [[ "${pkg}" == "python3-venv" ]] || continue
      if ${sudo_cmd} apt-get install -y "${pkg}" >/dev/null 2>&1; then
        log "installed ${pkg}"
        break
      fi
    done
  elif have_cmd dnf; then
    ${sudo_cmd} dnf install -y python3-libs >/dev/null 2>&1 || true
  fi

  if python3 -c 'import ensurepip' 2>/dev/null; then
    return 0
  fi
  warn "ensurepip is still unavailable after attempting to install it.
     Install the venv package for your interpreter manually, e.g.
       sudo apt-get install -y python3-venv"
  return 1
}

# Needed only when REGISTRY points at ECR (login + repo creation) or when
# S3_BUCKET is set. Result upload itself goes through boto3 in the
# orchestrator venv, not the CLI. Nothing else in the suite needs it, so
# this is called on demand rather than unconditionally - a local-registry
# run should not have to pull ~60 MB it will never use.
ensure_aws_cli() {
  if have_cmd aws; then
    log "aws cli: $(aws --version 2>&1 | head -1)"
    return 0
  fi
  log "installing AWS CLI v2 (needed for ECR / S3)"

  local arch
  case "$(uname -m)" in
    x86_64|amd64)  arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) warn "no AWS CLI build for $(uname -m); install it manually"; return 1 ;;
  esac

  local sudo_cmd=""
  [[ "${EUID}" -ne 0 ]] && sudo_cmd="sudo"

  if ! have_cmd unzip; then
    if have_cmd apt-get; then
      ${sudo_cmd} apt-get update -y >/dev/null 2>&1 || true
      ${sudo_cmd} apt-get install -y unzip >/dev/null 2>&1 || true
    elif have_cmd dnf; then
      ${sudo_cmd} dnf install -y unzip >/dev/null 2>&1 || true
    elif have_cmd yum; then
      ${sudo_cmd} yum install -y unzip >/dev/null 2>&1 || true
    fi
  fi
  have_cmd unzip || { warn "unzip unavailable; cannot install AWS CLI"; return 1; }

  local tmp
  tmp="$(mktemp -d)"
  if ! curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" \
        -o "${tmp}/awscliv2.zip"; then
    warn "could not download the AWS CLI installer"
    rm -rf "${tmp}"
    return 1
  fi
  unzip -q "${tmp}/awscliv2.zip" -d "${tmp}"
  ${sudo_cmd} "${tmp}/aws/install" --update >/dev/null 2>&1 \
    || ${sudo_cmd} "${tmp}/aws/install" >/dev/null 2>&1 \
    || { warn "AWS CLI install failed"; rm -rf "${tmp}"; return 1; }
  rm -rf "${tmp}"
  hash -r 2>/dev/null || true

  if have_cmd aws; then
    log "aws cli: $(aws --version 2>&1 | head -1)"
    return 0
  fi
  warn "AWS CLI still not on PATH after install (check /usr/local/bin/aws)"
  return 1
}

# True when this run needs AWS at all.
needs_aws() {
  [[ "${REGISTRY:-}" == *.dkr.ecr.*.amazonaws.com/* ]] || [[ -n "${S3_BUCKET:-}" ]]
}

# Load .env WITHOUT clobbering variables already present in the
# environment, so `CONCURRENCIES=128 ./scripts/run-suite.sh` actually
# overrides the file instead of being silently squashed by `source .env`.
load_env() {
  local file="${1:-.env}"
  [[ -f "${file}" ]] || return 0
  local key val
  while IFS='=' read -r key val; do
    [[ -z "${key}" || "${key}" =~ ^[[:space:]]*# ]] && continue
    key="${key#export }"
    key="${key// /}"
    [[ -z "${key}" ]] && continue
    if [[ -z "${!key+x}" ]]; then
      val="${val%\"}"; val="${val#\"}"
      val="${val%\'}"; val="${val#\'}"
      export "${key}=${val}"
    fi
  done < "${file}"
}

# ECR login when REGISTRY points at ECR, for the local `docker push`.
ecr_login_if_needed() {
  if [[ "${REGISTRY:-}" != *.dkr.ecr.*.amazonaws.com/* ]]; then
    return 0
  fi
  if ! have_cmd aws; then
    ensure_aws_cli || die "REGISTRY is ECR but the AWS CLI is unavailable.
     Install it (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
     or switch REGISTRY to a local registry, e.g. REGISTRY=127.0.0.1:5000/arl"
  fi
  local host region
  host="$(echo "${REGISTRY}" | cut -d/ -f1)"
  region="$(echo "${host}" | cut -d. -f4)"

  # Verify credentials first. Otherwise the failure surfaces as the very
  # confusing "Cannot perform an interactive login from a non TTY device"
  # - docker login falling back to a prompt because
  # `aws ecr get-login-password` printed nothing.
  if ! aws sts get-caller-identity --region "${region}" >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: AWS credentials are not configured on this host.
       \`aws sts get-caller-identity\` failed.

Fix one of:
  - attach an IAM instance profile with AmazonEC2ContainerRegistryPowerUser
    (needed to create repos and push) - takes effect immediately
  - export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN)
  - run \`aws configure\`

REGISTRY=${REGISTRY}
EOF
    return 1
  fi

  log "ECR login: ${host} (region ${region})"
  aws ecr get-login-password --region "${region}" \
    | docker login --username AWS --password-stdin "${host}"
}

# Registry credentials for the *template build*, which is a separate pull
# performed by CubeSandbox rather than by docker.
#
# This is materially simpler than the AgentENV path it replaces. There, the
# runtime pulled with regctl running as a service account, so the docker
# credential had to be copied into that account's home - a step that failed
# silently on a directory it could not read. Cube instead accepts the
# credential inline on `POST /templates` (`registryUsername` /
# `registryPassword`), so provision-templates.sh just passes it through and
# nothing is written to disk.
#
# Exports REGISTRY_USERNAME / REGISTRY_PASSWORD for provision-templates.sh.
# ECR tokens are valid for 12 hours, which comfortably outlasts a
# provisioning run.
export_registry_credentials() {
  if [[ -n "${REGISTRY_USERNAME:-}" ]]; then
    log "using REGISTRY_USERNAME from the environment for template pulls"
    return 0
  fi
  if [[ "${REGISTRY:-}" != *.dkr.ecr.*.amazonaws.com/* ]]; then
    return 0
  fi
  local host region token
  host="$(echo "${REGISTRY}" | cut -d/ -f1)"
  region="$(echo "${host}" | cut -d. -f4)"
  # Fatal rather than a warning, because the deferred symptom is very hard
  # to read: with no credential inline, Cube's native export falls back to
  # the host's docker credential store, and a stale ECR token there fails
  # the pull minutes later with "authorization token has expired" attributed
  # to the *template*. Better to stop here, where the cause is obvious.
  have_cmd aws || die "\
${REGISTRY} is an ECR registry but the aws CLI is not on PATH, so no pull
     credential can be obtained for the template build.
     run: bash scripts/setup-host.sh   (installs the aws CLI)
     or export REGISTRY_USERNAME / REGISTRY_PASSWORD yourself."
  token="$(aws ecr get-login-password --region "${region}" 2>/dev/null || true)"
  if [[ -z "${token}" ]]; then
    die "\
could not obtain an ECR token for ${host}.
     The template build pull would then fall back to the host docker
     credential store and fail with 'authorization token has expired'
     several minutes in, reported as a template failure.
     check: aws sts get-caller-identity --region ${region}"
  fi
  export REGISTRY_USERNAME="AWS"
  export REGISTRY_PASSWORD="${token}"
  log "ECR credentials prepared for the template build (${host})"
}

# Fail early with an actionable message if the venv is missing.
require_venv() {
  if [[ ! -x .venv/bin/python ]]; then
    die "orchestrator venv not found at .venv/
     run: bash scripts/setup-host.sh    (or: python3 -m venv .venv && .venv/bin/pip install -r orchestrator/requirements.txt)"
  fi
}

cube_healthy() {
  curl -fsS -o /dev/null "${CUBE_API_URL:-http://127.0.0.1:3000}/health" 2>/dev/null
}

require_cube() {
  if ! cube_healthy; then
    die "CubeAPI is not healthy at ${CUBE_API_URL:-http://127.0.0.1:3000}
     run: sudo bash scripts/ensure-cube.sh"
  fi
  # CubeProxy is a separate process and carries the entire data plane, so a
  # sweep that starts without it fails on its first in-sandbox request
  # rather than at startup. It has no health route; any HTTP response proves
  # it is listening.
  if [[ "${CUBE_TRANSPORT:-path}" != "direct" ]]; then
    local url="${CUBE_PROXY_URL:-http://127.0.0.1:80}"
    if [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}/" 2>/dev/null || echo 000)" == "000" ]]; then
      die "CubeProxy is not answering at ${url}, and CUBE_TRANSPORT=${CUBE_TRANSPORT:-path}
     routes every in-sandbox request through it.
     check: docker ps --filter name=cube-proxy
     (it uses host networking; the port comes from CUBE_PROXY_HTTP_PORT)"
    fi
  fi
}

# ------------------------------------------------------------------
# Disk helpers
#
# These two live here rather than in ensure-cube.sh because
# ensure-data-disk.sh needs exactly the same logic: one asks "is this
# directory reflink-capable", the other asks "which disk may I format".
# Keeping one copy means the safety rules cannot drift between the script
# that CHECKS the filesystem and the script that CREATES it.
# ------------------------------------------------------------------

# Ask the kernel whether this directory supports FICLONE, by using it.
#
# This is the capability Cube actually requires - cubecow implements
# snapshot, clone and rollback with that ioctl - so a successful clone here
# is proof, where `xfs_info | grep reflink=1` is only an indicator, and one
# that is easy to read incorrectly (see check_reflink_fs in ensure-cube.sh).
#
# Returns 0 = clone succeeded, 1 = the filesystem refused it,
#         2 = could not test (no permission to write here, for instance).
# The three are kept distinct on purpose: treating "could not test" as
# "unsupported" would abort on a host that is actually fine.
reflink_probe() {
  local dir="$1" src dst
  mkdir -p "${dir}" 2>/dev/null || return 2
  src="$(mktemp -p "${dir}" .reflink-probe.XXXXXX 2>/dev/null)" || return 2
  dst="${src}.clone"
  if ! dd if=/dev/zero of="${src}" bs=4096 count=1 status=none 2>/dev/null; then
    rm -f "${src}" 2>/dev/null || true
    return 2
  fi
  if cp --reflink=always "${src}" "${dst}" 2>/dev/null; then
    rm -f "${src}" "${dst}" 2>/dev/null || true
    return 0
  fi
  rm -f "${src}" "${dst}" 2>/dev/null || true
  return 1
}

# Whole disks that are safe to consider formatting, space separated.
#
# A disk qualifies only when NOTHING in its tree carries a filesystem or a
# mountpoint. Checking the disk's own row is not enough: a root disk with
# partitions has an empty FSTYPE/MOUNTPOINT of its own while its children
# hold the running system, so a scan that looked only at the disk row would
# nominate the root disk and `mkfs` would destroy the host.
#
# Deliberately property-based, never name-based. On Nitro the kernel
# enumerates NVMe devices in the order they answer discovery, which is
# neither the block-device-mapping order nor stable across boots - so
# "the data volume is /dev/nvme1n1" is a guess, and the cost of guessing
# wrong is mkfs on the root filesystem.
#   https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html
spare_block_devices() {
  local disk used out=""
  for disk in $(lsblk -drno NAME,TYPE 2>/dev/null | awk '$2=="disk" {print $1}'); do
    used="$(lsblk -rno FSTYPE,MOUNTPOINT "/dev/${disk}" 2>/dev/null \
            | tr -d ' \n' || true)"
    [[ -z "${used}" ]] && out="${out}${out:+ }/dev/${disk}"
  done
  echo "${out}"
}

# The whole disk backing /, e.g. /dev/nvme0n1. Empty if it cannot be
# determined - callers must treat "unknown" as "do not touch anything".
root_disk_device() {
  local root_src root_disk
  root_src="$(findmnt -no SOURCE / 2>/dev/null || true)"
  [[ -n "${root_src}" ]] || return 0
  root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null | tr -d ' ' | head -1 || true)"
  [[ -n "${root_disk}" ]] && echo "/dev/${root_disk}"
}
