#!/usr/bin/env bash
# Build the unified rollout-sandbox image for this host's architecture and
# push it to REGISTRY, so CubeSandbox can pull it as a template source.
#
# Docker is used here as an image builder, and separately by Cube's
# one-click deployment to run its dependency containers (MySQL, Redis,
# CubeProxy, CoreDNS, WebUI). It is not the sandbox runtime: the sandboxes
# are microVMs, and this image is converted into a microVM rootfs once, at
# template-build time, then never pulled again.
#
# Single-architecture, single-host: the benchmark runs on one metal
# instance at a time, so there is no reason to build a fat multi-arch
# manifest. Each instance builds natively for itself, which also avoids
# QEMU emulation entirely.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

: "${REGISTRY:?set REGISTRY in .env (ECR, or localhost:5000/... for a local registry)}"
: "${IMAGE_TAG:=v2}"

need_cmd docker

case "$(uname -m)" in
  x86_64|amd64)   HOST_ARCH=amd64 ;;
  aarch64|arm64)  HOST_ARCH=arm64 ;;
  *) die "unsupported host arch $(uname -m)" ;;
esac
PLATFORM="${PLATFORM:-linux/${HOST_ARCH}}"

log "native build for ${PLATFORM} (host: $(uname -m))"

# Right after `ensure-builder.sh` installs Docker, the invoking user is in
# the docker group but that only applies at next login, so the socket is
# reachable only via sudo. Detect and adapt rather than making the operator
# log out and back in mid-setup.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
    log "using 'sudo docker' (docker group membership applies at next login)"
  else
    die "docker daemon not reachable. run: sudo bash scripts/ensure-builder.sh
     or: sudo systemctl start docker"
  fi
fi

# HumanEval + MBPP land in orchestrator/datasets/. The orchestrator now
# runs on the host from a venv, so these are read directly off disk rather
# than baked into an image - but B1's corpus loader falls back to 8 toy
# snippets if they are missing, which would quietly make B1 and B9
# meaningless. Fetch them here so a plain build always has them.
bash scripts/fetch-datasets.sh

# name : dockerfile : context
IMAGES=(
  "arl-sandbox:workers/arl-sandbox/Dockerfile:."
)

ensure_ecr_repos() {
  [[ "${REGISTRY}" == *.dkr.ecr.*.amazonaws.com/* ]] || return 0
  if ! have_cmd aws; then
    ensure_aws_cli || die "REGISTRY is ECR but the AWS CLI is unavailable"
  fi
  local ecr_host ecr_region ecr_prefix name repo
  ecr_host="$(echo "${REGISTRY}" | cut -d/ -f1)"
  ecr_region="$(echo "${ecr_host}" | cut -d. -f4)"
  ecr_prefix="${REGISTRY#*/}"

  log "ensuring ECR repositories exist under ${ecr_prefix}/"
  for entry in "${IMAGES[@]}"; do
    name="${entry%%:*}"
    repo="${ecr_prefix}/${name}"
    if aws ecr describe-repositories --region "${ecr_region}" \
         --repository-names "${repo}" >/dev/null 2>&1; then
      echo "    [ok]      ${repo}"
    else
      echo "    [create]  ${repo}"
      aws ecr create-repository \
        --region "${ecr_region}" \
        --repository-name "${repo}" \
        --image-scanning-configuration scanOnPush=true \
        --image-tag-mutability MUTABLE >/dev/null
    fi
  done
}

# A registry on this host needs no auth and no TLS; anything else does.
is_local_registry() {
  [[ "${REGISTRY}" == localhost:* || "${REGISTRY}" == 127.0.0.1:* ]]
}

start_local_registry() {
  is_local_registry || return 0
  local port="${REGISTRY#*:}"
  port="${port%%/*}"
  if curl -fsS -o /dev/null "http://127.0.0.1:${port}/v2/" 2>/dev/null; then
    log "local registry already up on :${port}"
    return 0
  fi
  log "starting local registry:2 on 127.0.0.1:${port}"
  ${DOCKER} rm -f arl-registry >/dev/null 2>&1 || true
  ${DOCKER} run -d --restart=always --name arl-registry \
    -p "127.0.0.1:${port}:5000" registry:2 >/dev/null
  for _ in $(seq 1 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:${port}/v2/" 2>/dev/null && break
    sleep 1
  done
  curl -fsS -o /dev/null "http://127.0.0.1:${port}/v2/" \
    || die "local registry did not come up on :${port}"
}

# A plain-HTTP local registry is convenient, but whether Cube can pull from
# one depends on which source path CubeTemplateCenter takes, and it chooses
# without asking (CubeTemplateCenter/pkg/image/source.go):
#
#   1. native export, if enabled
#   2. else DOCKERLESS, if BOTH skopeo and umoci are on PATH - resolves the
#      image from the registry with `skopeo inspect`, and the code passes no
#      --src-tls-verify=false, so a cleartext registry fails here
#   3. else the local DOCKER daemon - `docker pull` + `docker image inspect`,
#      which works over plain HTTP as soon as dockerd has the registry in
#      `insecure-registries`, and which finds the image we just built without
#      pulling at all
#
# So the fix differs by mode, and the mode is a property of what happens to
# be installed. Report which one applies rather than letting the operator
# discover it as an opaque TLS error 20 minutes into setup.
warn_if_local_registry() {
  is_local_registry || return 0
  local host="${REGISTRY%%/*}"
  local mode="docker (local daemon)"
  if have_cmd skopeo && have_cmd umoci; then
    mode="dockerless (skopeo)"
  fi
  log "template-build source path: ${mode}"

  if [[ "${mode}" == "dockerless (skopeo)" ]]; then
    warn "REGISTRY=${host} is plain HTTP, and CubeTemplateCenter will take its
     DOCKERLESS path because both skopeo and umoci are installed. That path
     resolves the image with \`skopeo inspect\` and does not disable TLS
     verification, so the template build will likely fail to pull.

     Pick one:
       - use ECR (recommended): provision-templates.sh passes the credential
         inline on POST /templates, so nothing host-side needs configuring
             REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com/agentic-rl-bench
       - serve the local registry over TLS
       - force the docker path by making skopeo/umoci unavailable to the
         CubeTemplateCenter service"
    return 0
  fi

  # Docker path: make sure dockerd will actually talk plain HTTP to it,
  # because the failure is otherwise reported as an HTTPS handshake error
  # against a cleartext port.
  local idx
  idx="$(${DOCKER} info --format '{{json .RegistryConfig.IndexConfigs}}' 2>/dev/null || true)"
  if ! out_matches "${idx}" "${host}"; then
    warn "REGISTRY=${host} is plain HTTP and dockerd does not list it as an
     insecure registry. The push below may still work (127.0.0.1 is treated
     as insecure by default), but CubeTemplateCenter's \`docker pull\` can
     fail on a non-loopback address. If it does:
       echo '{\"insecure-registries\":[\"${host}\"]}' | sudo tee /etc/docker/daemon.json
       sudo systemctl restart docker"
  fi
}

ecr_login_if_needed
ensure_ecr_repos
start_local_registry
warn_if_local_registry

# Plain `docker build` rather than `docker buildx build --push`.
#
# We only ever build one native architecture now, so the container driver
# buys nothing - and it breaks a localhost registry outright, because inside
# the buildkit container "localhost" is the container, not the host.
#
# --provenance is not passed for the same reason it mattered before: the
# classic builder produces a plain image manifest, and an attestation index
# in its place is a known way to make an image resolver fail to find a
# platform match. Cube's template build wants an ordinary image.
for entry in "${IMAGES[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  dockerfile="${rest%%:*}"
  context="${rest#*:}"
  tag="${REGISTRY}/${name}:${IMAGE_TAG}-$(image_arch_suffix)"
  log "building ${name} -> ${tag}  (this pulls Chromium + DuckDB; expect several minutes)"
  DOCKER_BUILDKIT=1 ${DOCKER} build \
    -f "${dockerfile}" \
    -t "${tag}" \
    "${context}"
  log "pushing ${tag}"
  ${DOCKER} push "${tag}"
done

log "done. template source image: $(sandbox_image_ref)"
log "next: bash scripts/provision-templates.sh"
