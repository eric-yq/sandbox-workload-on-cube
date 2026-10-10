#!/usr/bin/env bash
# One-command host preparation. Run this once after cloning the repo onto
# a fresh metal instance:
#
#   git clone <repo> && cd sandbox-workload-on-cube
#   cp env.example .env && vim .env      # set REGISTRY at minimum
#   sudo bash scripts/setup-host.sh
#
# Then:
#   bash scripts/run-suite.sh             # B1 B3 B4 B5 B7 B8 B9
#   bash scripts/run-single.sh B9
#
# Steps, in order:
#   1. Docker                (image builder AND Cube's dependency containers)
#   2. Data disk             (find the spare volume, XFS+reflink, mount /data)
#   3. CubeSandbox runtime   (KVM / XFS-reflink checks, install, tune, start,
#                             then relocate the template image directory)
#   3b. AWS CLI              (only when ECR or S3 is configured)
#   4. Datasets              (HumanEval + MBPP)
#   5. Build + push image    (the unified rollout-sandbox image)
#   6. Templates             (7 templates, per-benchmark VM shapes)
#   7. Orchestrator venv     (host process, needs all the host's vCPUs)
#   8. Smoke test            (boot one sandbox, run one rollout step)
#
# Note Docker comes first here, where the AgentENV version installed the
# sandbox runtime first: Cube's one-click install runs MySQL, Redis,
# CubeProxy, CoreDNS and the WebUI as containers, so it needs a working
# Docker daemon before it starts.
#
# The data disk comes before the runtime because ensure-cube.sh refuses to
# install onto a non-reflink filesystem, which is what a fresh Ubuntu host
# always has. Pass --skip-disk if you prepared /data yourself.
#
# Idempotent: safe to re-run. Use --skip-build to re-provision templates
# without rebuilding the image.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh

SKIP_BUILD=0
SKIP_TEMPLATES=0
SKIP_DISK=0
for arg in "$@"; do
  case "${arg}" in
    --skip-build)     SKIP_BUILD=1 ;;
    --skip-templates) SKIP_TEMPLATES=1 ;;
    --skip-disk)      SKIP_DISK=1 ;;
    -h|--help)
      # Print the header comment block, whatever length it is. This used to
      # be `sed -n '2,30p'`, which silently truncated the help text every
      # time a step was added to the header.
      awk 'NR==1 && /^#!/ {next} /^#/ {print; next} {exit}' "$0"
      exit 0
      ;;
    *) die "unknown flag: ${arg}" ;;
  esac
done

if [[ ! -f .env ]]; then
  die "no .env found.
     run: cp env.example .env && \$EDITOR .env
     REGISTRY is the only strictly required value."
fi
load_env
: "${REGISTRY:?set REGISTRY in .env}"
: "${IMAGE_TAG:=v2}"
CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"

# The CubeSandbox install writes systemd units, starts containers and
# adjusts /dev/kvm permissions, so this script needs root. The build and
# venv steps are then run back as the invoking user so the venv and docker
# credentials are not left root-owned.
if [[ "${EUID}" -ne 0 ]]; then
  die "must run as root: sudo bash scripts/setup-host.sh"
fi
RUN_USER="${SUDO_USER:-root}"
as_user() {
  if [[ "${RUN_USER}" == "root" ]]; then
    bash -lc "cd '$(pwd)' && $*"
  else
    sudo -u "${RUN_USER}" --preserve-env=REGISTRY,IMAGE_TAG,CUBE_API_URL,CUBE_API_KEY,CUBE_PROXY_URL,AWS_REGION \
      bash -lc "cd '$(pwd)' && $*"
  fi
}

echo
echo "############################################################"
echo "# 1/8  Docker (image builder + Cube's dependency containers)"
echo "############################################################"
# Docker is needed twice over here: to build the template source image, and
# by Cube's one-click install, which runs MySQL, Redis, CubeProxy, CoreDNS
# and the WebUI as containers. So it has to come before step 2.
# Fail loudly if the script is missing rather than silently degrading to a
# bare `need_cmd docker` - a missing script means a broken checkout.
bash scripts/ensure-builder.sh

echo
echo "############################################################"
echo "# 2/8  Data disk (XFS with reflink, mounted at /data)"
echo "############################################################"
# Must precede step 3: ensure-cube.sh refuses to install onto a filesystem
# that cannot do FICLONE, and a fresh Ubuntu host is always ext4. A host
# that is already prepared (or has a btrfs root) is a no-op here.
if (( SKIP_DISK )); then
  log "skipped (--skip-disk)"
else
  bash scripts/ensure-data-disk.sh
fi

echo
echo "############################################################"
echo "# 3/8  CubeSandbox runtime"
echo "############################################################"
bash scripts/ensure-cube.sh

# Relocate the converted-rootfs directory onto the data volume, now that the
# install exists. This cannot happen earlier: the target lives inside
# CubeSandbox's install root and the one-click installer refuses to run when
# that root is non-empty but holds none of its own markers. Without it the
# 7 template images (~3 GiB each) land on the root volume and provisioning
# fills it partway through, reporting a misleading "insufficient disk space".
if findmnt -no TARGET "${DATA_MOUNT:-/data}" >/dev/null 2>&1; then
  bash tools/remote/bind-image-dir.sh "${DATA_MOUNT:-/data}"
else
  warn "${DATA_MOUNT:-/data} is not a separate mount, so template rootfs
     images will stay on the root volume. That is fine for a smoke test and
     a problem for a full sweep (~21 GiB for 7 templates)."
fi
# The invoking user needs to reach the docker socket for the build step.
if [[ "${RUN_USER}" != "root" ]]; then
  # Not a pipeline: see out_matches() in _lib.sh (pipefail + grep -q).
  if ! out_matches " $(id -nG "${RUN_USER}") " ' docker '; then
    log "adding ${RUN_USER} to the docker group"
    usermod -aG docker "${RUN_USER}" || warn "could not add ${RUN_USER} to docker group"
    warn "group membership applies to new logins; this run uses root's socket access"
  fi
fi

echo
echo "############################################################"
echo "# 3b/8 AWS CLI (only when ECR or S3 is configured)"
echo "############################################################"
# Done here, before the build, so a missing AWS CLI surfaces as one clear
# message up front instead of aborting step 4 halfway through with
# "required command not found: aws".
#
# Only the ECR path and S3 result upload need it; result upload itself goes
# through boto3 in the venv. A local-registry run skips this entirely.
if needs_aws; then
  if registry_is_ecr; then
    log "REGISTRY is ECR -> AWS CLI required"
  else
    log "S3_BUCKET is set -> AWS CLI useful for credential checks"
  fi
  ensure_aws_cli || die "could not install the AWS CLI.
     Either install it manually, or point REGISTRY at a local registry:
       REGISTRY=127.0.0.1:5000/arl
     (setup-host.sh will then start a registry:2 container for you)"
  # Check credentials in BOTH contexts, because they are not the same.
  # This script runs as root under sudo, but the build/push step runs back
  # as the invoking user via as_user. IAM *user* credentials live in
  # ~/.aws/credentials, so root can fail while the invoking user succeeds -
  # and it is the invoking user's context that actually does the ECR push.
  # (An instance profile works for both, which is why this went unnoticed.)
  root_arn="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)"
  user_arn=""
  if [[ "${RUN_USER}" != "root" ]]; then
    user_arn="$(as_user "aws sts get-caller-identity --query Arn --output text" 2>/dev/null | tail -1 || true)"
    [[ "${user_arn}" == arn:* ]] || user_arn=""
  fi

  if [[ -n "${root_arn}" ]]; then
    log "credentials ok (root): ${root_arn}"
  elif [[ -n "${user_arn}" ]]; then
    log "credentials ok (${RUN_USER}): ${user_arn}"
    # build-all.sh and ecr_login_if_needed run as root, so root needs to see
    # these credentials too. Point the CLI at the invoking user's files
    # rather than copying secret values into the environment.
    run_home="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
    if [[ -r "${run_home}/.aws/credentials" ]]; then
      export AWS_SHARED_CREDENTIALS_FILE="${run_home}/.aws/credentials"
      [[ -r "${run_home}/.aws/config" ]] && export AWS_CONFIG_FILE="${run_home}/.aws/config"
      log "root will use ${RUN_USER}'s credentials at ${run_home}/.aws/ for the ECR steps"
    else
      warn "root has no credentials and ${run_home}/.aws/credentials is not readable.
     The ECR build/push step runs as root and will likely fail.
     Attach an IAM instance profile, or re-run with: sudo -E bash scripts/setup-host.sh"
    fi
  else
    warn "aws sts get-caller-identity failed for both root and ${RUN_USER}.
     Attach an IAM instance profile (AmazonEC2ContainerRegistryPowerUser for
     ECR push, s3:PutObject for result upload), or put credentials in
     ~${RUN_USER}/.aws/credentials, and re-run.
     If you exported AWS_* variables in your shell, note that plain sudo
     drops them - use 'sudo -E bash scripts/setup-host.sh'."
  fi
else
  log "skipped: REGISTRY is not ECR and S3_BUCKET is unset"
fi

echo
echo "############################################################"
echo "# 4/8  Datasets (HumanEval + MBPP)"
echo "############################################################"
bash scripts/fetch-datasets.sh
chown -R "${RUN_USER}" orchestrator/datasets 2>/dev/null || true

echo
echo "############################################################"
echo "# 5/8  Build + push the rollout-sandbox image"
echo "############################################################"
if (( SKIP_BUILD )); then
  log "skipped (--skip-build)"
else
  bash scripts/build-all.sh
fi

echo
echo "############################################################"
echo "# 6/8  CubeSandbox templates"
echo "############################################################"
if (( SKIP_TEMPLATES )); then
  log "skipped (--skip-templates)"
else
  # The template build is a pull performed by CubeSandbox, not by docker, so
  # it needs its own credential. Cube takes it inline on POST /templates,
  # which is why nothing has to be written to a service account's home.
  export_registry_credentials
  bash scripts/provision-templates.sh
fi

echo
echo "############################################################"
echo "# 7/8  Orchestrator venv"
echo "############################################################"
# The orchestrator runs as a host process, not in a sandbox: it needs all
# the host's vCPUs to drive load, it reads /proc/stat and the CubeSandbox
# component cgroups directly, and in `direct` transport it dials sandbox IPs
# that only exist in the host network namespace.
ensure_venv_support || die "cannot create a virtualenv: python3 -m venv is
     unusable because ensurepip is missing, and installing the venv package
     failed. Install it for your interpreter and re-run:
       sudo apt-get install -y python3-venv"

# A failed `python3 -m venv` still leaves a half-built .venv/ behind, so
# "the interpreter exists" is not proof the venv works - the next run would
# skip creation and fail on pip instead. Validate pip and rebuild if broken.
if [[ -e .venv ]] && ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  warn "existing .venv is incomplete (no working pip); recreating it"
  rm -rf .venv
fi
if [[ ! -x .venv/bin/python ]]; then
  log "creating .venv"
  as_user "python3 -m venv .venv"
fi
log "installing orchestrator dependencies"
as_user ".venv/bin/pip install --quiet --upgrade pip"
as_user ".venv/bin/pip install --quiet -r orchestrator/requirements.txt"
chown -R "${RUN_USER}" .venv 2>/dev/null || true

echo
echo "############################################################"
echo "# 8/8  Smoke test"
echo "############################################################"
# Boots one sandbox from the B3 template, runs one trajectory step through
# it, tears it down, and then exercises snapshot / clone / rollback. This
# covers the entire path - template alias resolution, sandbox create,
# CubeProxy routing, the state primitives the `clone` and `rollback`
# strategies depend on, teardown - so a misconfiguration surfaces here
# instead of 30 minutes into a sweep.
#
# Skipped together with the templates, because it cannot pass without
# them: it resolves the B3 alias and creates a sandbox from it. Running it
# anyway made `--skip-templates` unusable on a host that has no templates
# yet - the flag exists precisely to split "install the platform" from
# "build the templates", and step 7 turned that split into a guaranteed
# failure.
if (( SKIP_TEMPLATES )); then
  log "skipped: needs templates, which were skipped (--skip-templates)"
  log "run this once templates exist: .venv/bin/python orchestrator/smoke_test.py --all"
else
  as_user ".venv/bin/python orchestrator/smoke_test.py"
fi

echo
echo "############################################################"
log "host ready."
echo
echo "  Run everything:      bash scripts/run-suite.sh"
echo "  Run one benchmark:   bash scripts/run-single.sh B9"
echo "  Results:             ./results/<arch>/"
echo
echo "  CubeAPI:             ${CUBE_API_URL}   (systemctl status cube-sandbox-cube-api)"
echo "  Web console:         http://<this-host>:12088"
echo "  Templates:           cubemastercli tpl list"
echo "  Live sandboxes:      curl -s ${CUBE_OPS_URL:-http://127.0.0.1:3010}/internal/v1/nodes"
echo "############################################################"
