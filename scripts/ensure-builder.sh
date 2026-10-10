#!/usr/bin/env bash
# Detect (and optionally install) Docker.
#
# Docker is not the sandbox runtime - the sandboxes are CubeSandbox microVMs.
# It is needed for two other reasons:
#
#   1. Building the template source image. Cube templates are built from OCI
#      images, and our image COPYs in the worker apps and the datasets, so a
#      real builder is required.
#   2. Cube's own one-click deployment, which runs MySQL, Redis, CubeProxy,
#      CoreDNS and the WebUI as containers. That makes Docker a runtime
#      dependency of the platform, not just of the build - which is why
#      setup-host.sh installs it before installing Cube.
#
# Scope note: this deliberately does NOT deal with docker compose or QEMU
# binfmt. We build a single native architecture, and Cube's installer brings
# its own compose handling, so requiring either here would only add ways for
# setup to fail on things this suite never invokes directly.
#
# Usage:
#   ./ensure-builder.sh          # detect; install missing parts
#   ./ensure-builder.sh --check  # detect only; non-zero exit if anything missing
#
# Supported install paths:
#   - Amazon Linux 2 / 2023        (dnf or yum)
#   - Ubuntu / Debian              (apt + Docker official repo)
#   - RHEL / Rocky / CentOS Stream (dnf + Docker official repo)
#   - macOS                        (instructions only - Docker Desktop is GUI)
#
# Safe to source from another script: callers can use the helper
# functions (need_cmd, run_priv) directly.

set -euo pipefail

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

# ---------- pretty printing ----------
log()  { printf '\033[1;34m[ensure-builder]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ensure-builder]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[ensure-builder]\033[0m %s\n' "$*" >&2; }

# ---------- privilege helpers ----------
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  fi
fi

run_priv() {
  if [[ -n "${SUDO}" ]]; then
    ${SUDO} "$@"
  else
    "$@"
  fi
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- OS detection ----------
detect_os() {
  local kind="unknown" id="" ver=""
  case "$(uname -s)" in
    Darwin) kind="macos" ;;
    Linux)
      kind="linux"
      if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"
        ver="${VERSION_ID:-}"
      fi
      ;;
  esac
  echo "${kind}|${id}|${ver}"
}

OS_INFO="$(detect_os)"
OS_KIND="${OS_INFO%%|*}"
OS_ID="$(echo "${OS_INFO}" | cut -d'|' -f2)"
OS_VER="$(echo "${OS_INFO}" | cut -d'|' -f3)"

log "host: $(uname -srm) | distro: ${OS_ID:-?} ${OS_VER:-?}"

# ---------- check helpers ----------
docker_ok()  { need_cmd docker; }
buildx_ok()  { docker buildx version >/dev/null 2>&1; }
# Check the daemon, not the caller's group membership. Adding a user to the
# `docker` group only takes effect on their next login, so immediately
# after an install `docker info` fails as that user even though the daemon
# is perfectly healthy - which used to make this script report a spurious
# hard failure right after a successful install.
daemon_ok()  { docker info >/dev/null 2>&1 || run_priv docker info >/dev/null 2>&1; }
# True when the daemon is up but only reachable via sudo.
daemon_needs_priv() { ! docker info >/dev/null 2>&1 && run_priv docker info >/dev/null 2>&1; }

# ---------- installers ----------
install_amzn() {
  # Amazon Linux 2023: docker is in the default repos
  # Amazon Linux 2:    docker comes via amazon-linux-extras
  log "installing docker for Amazon Linux ${OS_VER}"
  case "${OS_VER}" in
    2023*)
      run_priv dnf install -y docker
      ;;
    2*)
      run_priv amazon-linux-extras enable docker
      run_priv yum clean metadata
      run_priv yum install -y docker
      ;;
    *)
      warn "unknown Amazon Linux version ${OS_VER}, trying dnf docker"
      run_priv dnf install -y docker || run_priv yum install -y docker
      ;;
  esac
  # AL repos do not ship docker-buildx-plugin; we install the binary
  # via install_buildx_plugin() below.
}

install_rhel_family() {
  # RHEL / Rocky / AlmaLinux / CentOS Stream / Fedora - use Docker official repo
  log "installing docker for ${OS_ID} ${OS_VER} via Docker official repo"
  run_priv dnf install -y dnf-plugins-core 2>/dev/null \
    || run_priv yum install -y yum-utils

  local repo_distro="${OS_ID}"
  # Rocky / AlmaLinux / CentOS Stream all use the centos repo file
  case "${OS_ID}" in
    rocky|almalinux|centos) repo_distro="centos" ;;
  esac

  run_priv dnf config-manager --add-repo \
    "https://download.docker.com/linux/${repo_distro}/docker-ce.repo" 2>/dev/null \
    || run_priv yum-config-manager --add-repo \
       "https://download.docker.com/linux/${repo_distro}/docker-ce.repo"

  run_priv dnf install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin 2>/dev/null \
    || run_priv yum install -y docker-ce docker-ce-cli containerd.io \
       docker-buildx-plugin
}

install_debian_family() {
  log "installing docker for ${OS_ID} ${OS_VER} via Docker official repo"
  run_priv apt-get update -y
  run_priv apt-get install -y ca-certificates curl gnupg

  local keyring="/etc/apt/keyrings/docker.gpg"
  run_priv install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f "${keyring}" ]]; then
    curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" \
      | run_priv gpg --dearmor -o "${keyring}"
    run_priv chmod a+r "${keyring}"
  fi

  local codename
  codename="$( . /etc/os-release && echo "${VERSION_CODENAME:-}" )"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=${keyring}] \
https://download.docker.com/linux/${OS_ID} ${codename} stable" \
    | run_priv tee /etc/apt/sources.list.d/docker.list >/dev/null

  run_priv apt-get update -y
  run_priv apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin
}

install_via_get_docker() {
  log "no native install path for ${OS_ID}; falling back to get.docker.com"
  if need_cmd curl; then
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  elif need_cmd wget; then
    wget -qO /tmp/get-docker.sh https://get.docker.com
  else
    err "no curl/wget available; install one and re-run"
    return 1
  fi
  run_priv sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
}

install_docker_linux() {
  log "installing Docker Engine + buildx plugin"

  case "${OS_ID}" in
    amzn)                              install_amzn ;;
    rhel|centos|rocky|almalinux|fedora) install_rhel_family ;;
    ubuntu|debian)                     install_debian_family ;;
    *)                                 install_via_get_docker ;;
  esac

  # Enable + start daemon
  if need_cmd systemctl; then
    run_priv systemctl enable --now docker || true
  else
    run_priv service docker start || true
  fi

  # Add invoking user to docker group so subsequent calls don't need sudo.
  # Takes effect on next login; we still use sudo within this run.
  local target_user="${SUDO_USER:-${USER:-}}"
  if [[ -n "${target_user}" && "${target_user}" != "root" && "$(id -u)" -ne 0 ]]; then
    run_priv usermod -aG docker "${target_user}" || true
    warn "added ${target_user} to 'docker' group; log out/in for it to take effect"
  fi
}

install_docker_macos() {
  err "automatic Docker install not supported on macOS"
  err "please install Docker Desktop from:"
  err "  https://www.docker.com/products/docker-desktop/"
  err "or via Homebrew:"
  err "  brew install --cask docker  &&  open -a Docker"
  return 1
}

# Standalone buildx plugin install if the package above didn't pull it in.
install_buildx_plugin() {
  log "installing buildx CLI plugin from GitHub release"
  local arch tag="v0.17.1"
  case "$(uname -m)" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) err "unsupported arch $(uname -m) for buildx plugin"; return 1 ;;
  esac
  local url="https://github.com/docker/buildx/releases/download/${tag}/buildx-${tag}.linux-${arch}"
  local dest="/usr/libexec/docker/cli-plugins/docker-buildx"
  run_priv mkdir -p "$(dirname "${dest}")"
  if need_cmd curl; then
    run_priv sh -c "curl -fsSL '${url}' -o '${dest}'"
  else
    run_priv sh -c "wget -qO '${dest}' '${url}'"
  fi
  run_priv chmod +x "${dest}"
}

# ---------- main ----------
status=0

if docker_ok; then
  log "docker:  $(docker --version)"
else
  warn "docker:  not found"
  status=1
fi

if buildx_ok; then
  log "buildx:  $(docker buildx version | head -1)"
else
  warn "buildx:  not found"
  status=1
fi

if docker_ok && daemon_ok; then
  log "daemon:  running"
else
  warn "daemon:  not running / unreachable"
  status=1
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  exit "${status}"
fi

if [[ "${status}" -eq 0 ]]; then
  log "everything is in place"
  exit 0
fi

# Need to install something
case "${OS_KIND}" in
  linux)
    if ! docker_ok; then
      install_docker_linux
    fi
    if ! buildx_ok; then
      install_buildx_plugin
    fi
    ;;
  macos)
    install_docker_macos
    exit 1
    ;;
  *)
    err "unsupported OS for auto-install"
    exit 1
    ;;
esac

# Re-verify
if ! docker_ok || ! buildx_ok; then
  err "post-install verification failed"
  err "  docker:  $(docker --version 2>&1 || echo missing)"
  err "  buildx:  $(docker buildx version 2>&1 || echo missing)"
  exit 1
fi

if ! daemon_ok; then
  err "docker daemon not reachable after install"
  err "try: ${SUDO} systemctl start docker  (then re-run this script)"
  exit 1
fi

if daemon_needs_priv; then
  warn "daemon reachable only via sudo in this session (docker group membership"
  warn "applies at next login). build-all.sh handles this automatically."
fi

log "install complete"
log "  docker:  $(docker --version)"
log "  buildx:  $(docker buildx version | head -1)"
