#!/usr/bin/env bash
# Install and tune CubeSandbox on this host.
#
# Idempotent: safe to re-run. Skips the install when the services are
# already present, but always re-applies the tuning and re-verifies health.
#
# What this does, and the two things that will bite you if it does not:
#
#   1. /data/cubelet MUST be XFS with reflink enabled. Cube's default
#      storage backend (cubecow) implements snapshots with the FICLONE
#      ioctl, which ext4 does not support - and Ubuntu/Debian default to
#      ext4. Cubelet refuses to start on a non-reflink filesystem, so this
#      is checked before anything is downloaded.
#   2. TAP device pool sizing. Cubelet pre-allocates `tap_init_num` TAP
#      devices (default 500) and a create burst that exhausts the idle pool
#      stalls on TAP allocation. Sweeping to c=1024 needs it raised, and
#      the symptom otherwise is create latency degrading with concurrency -
#      which looks exactly like the result you are trying to measure.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

CUBE_DATA_DIR="${CUBE_DATA_DIR:-/data/cubelet}"
CUBE_TOOLBOX="${CUBE_TOOLBOX:-/usr/local/services/cubetoolbox}"
CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:3000}"
CUBE_PROXY_URL="${CUBE_PROXY_URL:-http://127.0.0.1:80}"
CUBE_OPS_URL="${CUBE_OPS_URL:-http://127.0.0.1:3010}"
CUBE_REPO="${CUBE_REPO:-TencentCloud/CubeSandbox}"

# Disk thresholds for the filesystem holding $CUBE_DATA_DIR.
MIN_FREE_GB="${MIN_FREE_GB:-50}"
RECOMMENDED_GB="${RECOMMENDED_GB:-500}"

# Density tuning. Defaults sized for the top of the default sweep
# (CONCURRENCIES up to 1024) with headroom.
CUBE_TAP_INIT_NUM="${CUBE_TAP_INIT_NUM:-1200}"
CUBE_CGROUP_POOL_SIZE="${CUBE_CGROUP_POOL_SIZE:-3000}"
CUBE_WORKFLOW_CONCURRENT="${CUBE_WORKFLOW_CONCURRENT:-256}"

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "must run as root (installs systemd units, touches /dev/kvm perms)
     try: sudo bash scripts/ensure-cube.sh"
  fi
}

# ------------------------------------------------------------------
# 1. Preflight
# ------------------------------------------------------------------
check_arch() {
  case "$(uname -m)" in
    x86_64|amd64)  CUBE_ARCH=amd64 ;;
    aarch64|arm64) CUBE_ARCH=arm64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  log "arch: $(uname -m) (cube bundle: ${CUBE_ARCH})"
  if [[ "${CUBE_ARCH}" == "arm64" ]]; then
    # Native ARM64 support landed in v0.5.0 (build, guest kernel, UEFI
    # boot path, seccomp filters). Two consequences for us:
    #   * the `online-install.sh` one-liner only auto-discovers x86_64
    #     packages, so ARM64 installs from the release tarball;
    #   * PVM nested virtualisation is x86-only, so an ARM host must
    #     expose native KVM - i.e. bare metal. That is fine here: the whole
    #     suite targets *.metal instances.
    log "aarch64: installing from the arm64 one-click tarball (the"
    log "         online-install.sh one-liner is x86_64-only for now)"
  fi
}

check_kvm() {
  if [[ ! -e /dev/kvm ]]; then
    die "/dev/kvm is missing.
     On EC2, KVM is only exposed on *.metal / *.metal-* instance types.
     This benchmark targets m7i.metal-48xl / m8i.metal-48xl /
     m8g.metal-48xl / m9g.metal-48xl.
     (Cube's PVM path can provide KVM on ordinary x86 cloud VMs, but it
     does not support ARM64 and is not what these numbers are for.)"
  fi
  log "/dev/kvm present"
}

check_glibc() {
  # CubeSandbox binaries are built on Ubuntu 20.04 (glibc 2.31). An older
  # host produces a dynamic-linker error at first start that reads like a
  # corrupt download rather than an unsupported distro.
  local ver
  ver="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo "")"
  if [[ -z "${ver}" ]]; then
    warn "could not determine the glibc version; CubeSandbox needs >= 2.31"
    return 0
  fi
  log "glibc: ${ver}"
  if [[ "$(printf '%s\n2.31\n' "${ver}" | sort -V | head -1)" != "2.31" ]]; then
    die "glibc ${ver} is too old; CubeSandbox binaries need >= 2.31.
     Ubuntu 20.04+ / OpenCloudOS 9 / TencentOS 4 are fine."
  fi
}

check_docker() {
  # Cube's one-click deployment runs MySQL, Redis, CubeProxy, CoreDNS and
  # the WebUI as containers, so Docker is a runtime dependency here - not
  # just an image builder the way it was under AgentENV.
  if ! have_cmd docker; then
    die "docker is required by the CubeSandbox one-click install (MySQL,
     Redis, CubeProxy, CoreDNS and the WebUI run as containers).
     run: bash scripts/ensure-builder.sh"
  fi
  if ! docker info >/dev/null 2>&1; then
    die "the docker daemon is not reachable.
     run: systemctl start docker"
  fi
  log "docker: $(docker --version 2>/dev/null | head -1)"
}

# The check most likely to stop a fresh Ubuntu host.
# Ask the kernel whether this directory supports FICLONE, by using it.
#
# This is the capability Cube actually requires - cubecow implements
# snapshot, clone and rollback with that ioctl - so a successful clone here
# is proof, where `xfs_info | grep reflink=1` is only an indicator, and one
# that is easy to read incorrectly (see check_reflink_fs).
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

check_reflink_fs() {
  local dir="${CUBE_DATA_DIR}"
  if [[ ! -d "${dir}" ]]; then
    warn "${dir} does not exist yet; it will be created on the root filesystem"
    mkdir -p "${dir}"
  fi

  local fstype
  fstype="$(stat -f -c %T "${dir}" 2>/dev/null || echo unknown)"
  # `stat -f` reports every ext[234] as "ext2/ext3", which reads like a
  # detection bug when the volume is plainly ext4. Prefer findmnt's answer
  # and keep stat as the fallback.
  local fstype_precise
  fstype_precise="$(findmnt -no FSTYPE --target "${dir}" 2>/dev/null || true)"
  [[ -n "${fstype_precise}" ]] && fstype="${fstype_precise}"
  log "filesystem for ${dir}: ${fstype}"

  local reflink_ok=0
  if [[ "${fstype}" == "xfs" ]]; then
    # xfs_info takes a MOUNT POINT, a block device or an image file - not an
    # arbitrary directory. $CUBE_DATA_DIR is normally a subdirectory of the
    # mount (the recommended layout is the volume at /data with
    # CUBE_DATA_DIR=/data/cubelet), and passing that gets
    # "xfs_info: cannot open ...: Is a directory" with an empty result, which
    # then reads as "reflink is off" and aborts a perfectly good host. So
    # resolve to the mount point first.
    local mp xinfo=""
    mp="$(findmnt -no TARGET --target "${dir}" 2>/dev/null || true)"
    [[ -n "${mp}" ]] || mp="${dir}"
    have_cmd xfs_info && xinfo="$(xfs_info "${mp}" 2>/dev/null || true)"
    if [[ -n "${xinfo}" ]] && out_matches "${xinfo}" 'reflink=1'; then
      reflink_ok=1
      log "xfs_info(${mp}) reports reflink=1"
    else
      # Inconclusive: xfsprogs missing, or xfs_info refused the path. Ask the
      # kernel directly instead of guessing - FICLONE is the exact capability
      # Cube's cubecow backend needs, so this is ground truth rather than a
      # proxy for it, and it costs one 4 KiB file.
      [[ -n "${xinfo}" ]] || warn "xfs_info gave no answer for ${mp}; probing FICLONE directly"
      reflink_probe "${dir}"; local probe=$?
      case ${probe} in
        0) reflink_ok=1; log "FICLONE works on ${dir}" ;;
        2) warn "could not test FICLONE in ${dir} (no write permission?); \
letting Cubelet be the judge"
           reflink_ok=1 ;;
      esac
    fi
  elif [[ "${fstype}" == "btrfs" ]]; then
    reflink_ok=1
  else
    # Some other filesystem could still support FICLONE; let the kernel say
    # so rather than maintaining a whitelist.
    reflink_probe "${dir}"; local probe=$?
    case ${probe} in
      0) reflink_ok=1; log "FICLONE works on ${dir} despite fstype=${fstype}" ;;
      2) warn "could not test FICLONE in ${dir} (no write permission?)" ;;
    esac
  fi

  if (( reflink_ok != 1 )); then
    # Never name the device currently backing this path in a mkfs command.
    # Until $CUBE_DATA_DIR has its own volume, that path resolves to the
    # ROOT filesystem, and a copy-pasteable `mkfs.xfs -f /dev/<root>` would
    # destroy the host. Only offer devices that carry no filesystem and are
    # not mounted.
    local cur_dev candidates remedy disk
    cur_dev="$(findmnt -no SOURCE --target "${dir}" 2>/dev/null || true)"
    # A disk is only a candidate when NOTHING in its tree carries a
    # filesystem or a mountpoint. Checking the disk's own row is not enough:
    # a root disk with partitions has an empty FSTYPE/MOUNTPOINT of its own
    # while its children hold the running system, and suggesting mkfs on it
    # would destroy the host.
    candidates=""
    for disk in $(lsblk -drno NAME,TYPE 2>/dev/null | awk '$2=="disk" {print $1}'); do
      local used
      used="$(lsblk -rno FSTYPE,MOUNTPOINT "/dev/${disk}" 2>/dev/null \
              | tr -d ' \n' || true)"
      [[ -z "${used}" ]] && candidates="${candidates}${candidates:+ }/dev/${disk}"
    done

    if [[ -n "${candidates}" ]]; then
      local dev
      dev="${candidates%% *}"
      remedy="     An unused block device is available: ${dev}
     (all candidates: ${candidates})
     Confirm it is the volume you mean before formatting it.

     Mount it at /data, not at ${dir}: CubeMaster stages rootfs artifacts in
     /data/CubeMaster, and the converted per-template rootfs images (~3 GB
     EACH) go to ${CUBE_TOOLBOX}/cubebox_os_image. Mounting one level down
     leaves both of those on the root volume, which then fills up partway
     through provisioning. Bind-mount the image directory too.

       sudo mkfs.xfs -m reflink=1 ${dev}
       sudo mkdir -p /data
       echo \"${dev} /data xfs defaults,noatime 0 2\" | sudo tee -a /etc/fstab
       sudo mount /data
       sudo mkdir -p /data/cubebox_os_image ${CUBE_TOOLBOX}/cubebox_os_image
       echo \"/data/cubebox_os_image ${CUBE_TOOLBOX}/cubebox_os_image none bind 0 0\" \\
         | sudo tee -a /etc/fstab
       sudo mount ${CUBE_TOOLBOX}/cubebox_os_image"
    else
      remedy="     There is no spare block device on this host - ${dir} currently
     resolves to ${cur_dev:-the root filesystem}, and reformatting that
     would destroy the machine, so no mkfs command is offered here.

     Attach an EBS volume (gp3, >= 500 GiB for a full sweep, provisioned
     IOPS) and mount it at /data - not at ${dir}. CubeMaster stages rootfs
     artifacts in /data/CubeMaster and the converted per-template rootfs
     images (~3 GB EACH) go to ${CUBE_TOOLBOX}/cubebox_os_image, so mounting
     one level down leaves both on the root volume and provisioning fills it:

       sudo mkfs.xfs -m reflink=1 /dev/<new-device>
       sudo mkdir -p /data
       echo \"/dev/<new-device> /data xfs defaults,noatime 0 2\" | sudo tee -a /etc/fstab
       sudo mount /data
       sudo mkdir -p /data/cubebox_os_image ${CUBE_TOOLBOX}/cubebox_os_image
       echo \"/data/cubebox_os_image ${CUBE_TOOLBOX}/cubebox_os_image none bind 0 0\" \\
         | sudo tee -a /etc/fstab
       sudo mount ${CUBE_TOOLBOX}/cubebox_os_image

     For FUNCTIONAL verification only, where disk numbers do not matter, a
     loopback XFS on the existing volume also satisfies reflink:

       sudo truncate -s 25G /var/lib/cube-xfs.img
       sudo mkfs.xfs -m reflink=1 /var/lib/cube-xfs.img
       sudo mkdir -p ${dir}
       sudo mount -o loop /var/lib/cube-xfs.img ${dir}

     Do not benchmark storage on that - it is an extra block layer on top
     of the volume you actually care about."
    fi

    die "${dir} is on ${fstype}, which cannot do reflink copy-on-write.

     CubeSandbox's default storage backend (cubecow) implements snapshot,
     clone and rollback with the FICLONE ioctl, so it requires XFS created
     with reflink enabled (or btrfs). Cubelet refuses to start otherwise.
     Ubuntu, Debian and WSL all default to ext4.

${remedy}

     Then re-run this script. Reference:
     https://github.com/TencentCloud/CubeSandbox/issues/311"
  fi
  log "reflink-capable filesystem confirmed for ${dir}"
}

_fs_free_gb() {
  local dir="$1"
  while [[ ! -d "${dir}" && "${dir}" != "/" ]]; do dir="$(dirname "${dir}")"; done
  df -BG --output=avail "${dir}" 2>/dev/null | tail -1 | tr -dc '0-9'
}

_fs_total_gb() {
  local dir="$1"
  while [[ ! -d "${dir}" && "${dir}" != "/" ]]; do dir="$(dirname "${dir}")"; done
  df -BG --output=size "${dir}" 2>/dev/null | tail -1 | tr -dc '0-9'
}

_fs_source() {
  local dir="$1"
  while [[ ! -d "${dir}" && "${dir}" != "/" ]]; do dir="$(dirname "${dir}")"; done
  findmnt -no SOURCE --target "${dir}" 2>/dev/null || echo "?"
}

# Checking only $CUBE_DATA_DIR is not enough, and getting this wrong wastes a
# lot of time: the check passes, template builds start, and the fourth one
# dies with "insufficient disk space: available=4 GiB, estimated_required=5
# GiB" while the volume you provisioned sits nearly empty.
#
# Three different filesystems are involved, and by default two of them are the
# ROOT volume:
#
#   $CUBE_DATA_DIR (/data/cubelet)   Cubelet's CoW volumes and snapshots
#   /data/CubeMaster                 rootfs artifact staging
#   $CUBE_TOOLBOX/cubebox_os_image   the converted ext4 rootfs, and this is
#                                    the expensive one: ~3 GiB PER TEMPLATE,
#                                    so seven templates need ~21 GiB
#
# Neither of the latter two has a config knob, so the fix is a mount: put the
# volume on /data and bind-mount cubebox_os_image onto it. Measured on a
# c7g.metal with a 38 GiB root and a 200 GiB data volume, that moved root from
# 89% used to 26%.
check_disk() {
  local rootfs_dir="${CUBE_TOOLBOX}/cubebox_os_image"
  local per_template_gb="${CUBE_ROOTFS_GB_PER_TEMPLATE:-4}"
  local n_templates="${CUBE_TEMPLATE_COUNT:-7}"
  local need_rootfs=$(( per_template_gb * n_templates ))

  local d_data d_master d_rootfs
  d_data="$(_fs_free_gb "${CUBE_DATA_DIR}")";  : "${d_data:=0}"
  d_master="$(_fs_free_gb /data)";             : "${d_master:=0}"
  d_rootfs="$(_fs_free_gb "${rootfs_dir}")";   : "${d_rootfs:=0}"

  log "disk:"
  log "  ${CUBE_DATA_DIR} (CoW + snapshots): ${d_data} GB free on $(_fs_source "${CUBE_DATA_DIR}")"
  log "  /data (CubeMaster staging):        ${d_master} GB free on $(_fs_source /data)"
  log "  ${rootfs_dir}"
  log "    (converted rootfs, ~${per_template_gb} GB x ${n_templates} templates = ${need_rootfs} GB):"
  log "    ${d_rootfs} GB free on $(_fs_source "${rootfs_dir}")"

  if (( d_data < MIN_FREE_GB )); then
    die "only ${d_data} GB free on the filesystem holding ${CUBE_DATA_DIR};
     Cube needs at least ${MIN_FREE_GB} GB there for CoW volumes and one
     memory snapshot per template."
  fi

  if (( d_rootfs < need_rootfs )); then
    local hint=""
    # The actionable case: the heavy directories are on root while a bigger
    # volume is mounted somewhere else.
    if [[ "$(_fs_source "${rootfs_dir}")" == "$(_fs_source /)" \
       && "$(_fs_source "${CUBE_DATA_DIR}")" != "$(_fs_source /)" ]]; then
      hint="
     Your data volume is mounted at ${CUBE_DATA_DIR} but the converted
     rootfs images go to ${rootfs_dir}, which is on the ROOT volume. Move the
     mount up a level and bind-mount the image directory onto it:

       sudo systemctl stop cube-sandbox-control.target
       # remount the volume at /data (captures /data/cubelet AND
       # /data/CubeMaster), preserving what is already on it
       sudo mkdir -p /data/cubebox_os_image
       echo \"/data/cubebox_os_image ${rootfs_dir} none bind 0 0\" \\
         | sudo tee -a /etc/fstab
       sudo mount ${rootfs_dir}
       sudo systemctl start cube-sandbox-control.target
"
    fi
    warn "${d_rootfs} GB free where converted rootfs images land, but ${n_templates}
     templates need roughly ${need_rootfs} GB (~${per_template_gb} GB each).
     Template builds will fail partway through with
       insufficient disk space: available=N GiB, estimated_required=M GiB
     and the earlier ones will already have succeeded, so it looks like a
     flaky build rather than a full disk.${hint}"
  fi

  local total
  total="$(_fs_total_gb "${CUBE_DATA_DIR}")"; : "${total:=0}"
  if (( total < RECOMMENDED_GB )); then
    warn "data volume is ${total} GB; ${RECOMMENDED_GB} GB+ is recommended for
     high-concurrency sweeps (c=512/1024 create and destroy thousands of
     writable layers, and every snapshot is billed storage until deleted)."
  fi
}

# ------------------------------------------------------------------
# 2. Install
# ------------------------------------------------------------------
cube_installed() {
  # No pipeline here on purpose - see out_matches() in _lib.sh. With
  # `systemctl list-unit-files | grep -q`, pipefail turned a successful match
  # into a failure and this function always said "not installed", so every
  # re-run re-downloaded the whole 400 MB bundle.
  local units
  units="$(systemctl list-unit-files 2>/dev/null || true)"
  out_matches "${units}" '^cube-sandbox-cube-api\.service' \
    && [[ -x "${CUBE_TOOLBOX}/CubeAPI/bin/cube-api" ]]
}

resolve_cube_version() {
  if [[ -n "${CUBE_VERSION:-}" ]]; then
    echo "${CUBE_VERSION}"
    return 0
  fi
  # Newest release that actually ships an asset for this architecture.
  # Pinning by hand is better for reproducibility, hence CUBE_VERSION, but
  # the arm64 asset set has lagged the x86 one before, so discovery beats
  # a hardcoded tag that may not have an arm64 build.
  curl -fsSL "https://api.github.com/repos/${CUBE_REPO}/releases?per_page=30" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    releases = json.load(sys.stdin)
except Exception:
    sys.exit(0)
want = sys.argv[1]
for rel in releases:
    if rel.get("draft") or rel.get("prerelease"):
        continue
    tag = rel.get("tag_name") or ""
    if tag.startswith("kernel-release-"):
        continue
    for asset in rel.get("assets") or []:
        name = asset.get("name") or ""
        if name.startswith("cube-sandbox-one-click-") and name.endswith(f"-{want}.tar.gz"):
            print(tag)
            sys.exit(0)
' "${CUBE_ARCH}"
}

install_from_tarball() {
  local version="$1" tarball="${CUBE_ONE_CLICK_TARBALL:-}"
  local workdir="/opt/cube-sandbox-install"
  mkdir -p "${workdir}"

  if [[ -z "${tarball}" ]]; then
    [[ -n "${version}" ]] || die "could not determine a CubeSandbox release
     with a ${CUBE_ARCH} one-click bundle.
     Pin one explicitly, e.g.  CUBE_VERSION=v0.7.0 sudo bash scripts/ensure-cube.sh
     or download the tarball yourself and pass CUBE_ONE_CLICK_TARBALL=/path/to.tar.gz
     Releases: https://github.com/${CUBE_REPO}/releases"
    local name="cube-sandbox-one-click-${version}-${CUBE_ARCH}.tar.gz"
    tarball="${workdir}/${name}"
    if [[ ! -f "${tarball}" ]]; then
      log "downloading ${name}"
      local url="https://github.com/${CUBE_REPO}/releases/download/${version}/${name}"
      if ! curl -fL --progress-bar "${url}" -o "${tarball}.part"; then
        rm -f "${tarball}.part"
        die "download failed: ${url}
     If you are in mainland China the CNB mirror is usually faster:
       https://cnb.cool/CubeSandbox/CubeSandbox/-/releases"
      fi
      mv "${tarball}.part" "${tarball}"
    else
      log "reusing cached ${tarball}"
    fi
  fi

  log "extracting $(basename "${tarball}")"
  tar -xzf "${tarball}" -C "${workdir}"
  local srcdir
  srcdir="$(find "${workdir}" -maxdepth 1 -type d -name 'cube-sandbox-one-click-*' \
            | sort | tail -1)"
  [[ -n "${srcdir}" ]] || die "no cube-sandbox-one-click-* directory in ${tarball}"

  write_one_click_env "${srcdir}"

  log "running ${srcdir}/install.sh (this pulls container images and builds"
  log "  the guest assets; expect several minutes)"
  chmod +x "${srcdir}/install.sh"

  # Deliberately not `set -e` fatal on the installer's exit code. Its final
  # quickcheck can report a false failure that leaves a perfectly healthy
  # node behind, and dying here would abort setup for no reason.
  #
  # Observed on v0.7.0-arm64: the quickcheck fails with
  #     ERROR: expected socket not ready: /data/cubelet/cubelet.sock
  # because Cubelet runs in its OWN MOUNT NAMESPACE (its start script mounts
  # a tmpfs for /data/cubelet/state), so the socket exists only inside that
  # namespace while the check stats it from the host's. `ss -xlp` shows the
  # listener, `nsenter --mount` shows the file, node registration succeeds
  # and the metrics endpoint serves - the node is fine.
  #
  # So: record the failure, then let verify() be the authority. It tests
  # what actually has to work for this benchmark (CubeAPI health, CubeProxy
  # listening, node registered) rather than an implementation detail of
  # where a socket is visible from.
  # The output is captured as well as shown, so a genuinely fatal failure can
  # be told apart from that known false negative. Blanket-tolerating every
  # non-zero exit made a real abort ("refusing to wipe install root") look
  # benign and then failed 60 seconds later waiting for a CubeAPI that was
  # never installed - with the wrong explanation attached.
  local rc=0 out
  out="$( ( cd "${srcdir}" && ./install.sh ) 2>&1 | tee /dev/stderr )" || rc=$?

  if (( rc != 0 )); then
    CUBE_INSTALLER_RC="${rc}"

    # Refusals that mean nothing was installed. Recognised explicitly,
    # because the remedy is specific and the generic message is misleading.
    if out_matches "${out}" 'refusing to wipe install root'; then
      local root="${CUBE_TOOLBOX:-/usr/local/services/cubetoolbox}"
      die "the installer refused to run: ${root} is not empty but holds none
     of its own markers (.one-click.env / CubeMaster / CubeAPI / Cubelet), so
     it will not touch it. NOTHING WAS INSTALLED.

     The usual cause is something pre-created inside the install root before
     the install - a bind mount for cubebox_os_image, for instance, which has
     to be established AFTER the install, not before.

     Current contents:
$(ls -A "${root}" 2>/dev/null | sed 's/^/       /')
$(findmnt -no SOURCE,TARGET --submounts "${root}" 2>/dev/null | sed 's/^/       mount: /')

     Clear it and re-run. If a bind mount is listed above:
       sudo umount ${root}/cubebox_os_image
       sudo sed -i '\\| ${root}/cubebox_os_image |d' /etc/fstab
       sudo rmdir ${root}/cubebox_os_image ${root}"
    fi

    if out_matches "${out}" 'expected socket not ready'; then
      # Known false negative. Cubelet runs in its OWN MOUNT NAMESPACE (its
      # start script mounts a tmpfs for /data/cubelet/state), so the socket
      # exists only inside that namespace while the check stats it from the
      # host's. `ss -xlp` shows the listener, `nsenter --mount` shows the
      # file, node registration succeeds - the node is fine.
      warn "the installer exited ${rc} on its socket self-check.
     This one is a known false negative: Cubelet runs in its own mount
     namespace, so /data/cubelet/cubelet.sock is invisible from the host.
     Continuing to the health checks, which decide."
    else
      # Unrecognised. Do not pretend to know it is harmless; verify() is
      # still the authority, but say plainly that this was not expected.
      warn "the installer exited ${rc} for a reason this script does not
     recognise (see its output above). Continuing to the health checks, which
     decide - but if they fail, that exit code is where to start looking."
    fi
  fi
}

write_one_click_env() {
  local srcdir="$1"
  local envfile="${srcdir}/.env"
  [[ -f "${srcdir}/env.example" ]] || die "no env.example in ${srcdir}"
  cp "${srcdir}/env.example" "${envfile}"

  # Node IP: the installer auto-detects eth0, which is right on EC2, but
  # be explicit when the operator told us otherwise.
  if [[ -n "${CUBE_SANDBOX_NODE_IP:-}" ]]; then
    printf '\nCUBE_SANDBOX_NODE_IP=%s\n' "${CUBE_SANDBOX_NODE_IP}" >> "${envfile}"
    log "one-click .env: CUBE_SANDBOX_NODE_IP=${CUBE_SANDBOX_NODE_IP}"
  fi
  # Sandbox IP pool. The default 192.168.0.0/18 gives 16382 addresses,
  # which covers c=1024 with room to spare, but it has to not collide with
  # the VPC subnet.
  if [[ -n "${CUBE_SANDBOX_NETWORK_CIDR:-}" ]]; then
    printf 'CUBE_SANDBOX_NETWORK_CIDR=%s\n' "${CUBE_SANDBOX_NETWORK_CIDR}" >> "${envfile}"
    log "one-click .env: CUBE_SANDBOX_NETWORK_CIDR=${CUBE_SANDBOX_NETWORK_CIDR}"
  fi
  # Keep CubeAPI unauthenticated unless asked otherwise: this is a
  # single-tenant benchmark host, and an auth check on the create path is
  # measured overhead we do not want in the numbers.
  if [[ -n "${CUBE_API_KEY:-}" ]]; then
    printf 'CUBE_API_KEY=%s\n' "${CUBE_API_KEY}" >> "${envfile}"
    log "one-click .env: CUBE_API_KEY set (auth enabled)"
  fi
  if [[ -n "${MIRROR:-}" ]]; then
    printf 'MIRROR=%s\n' "${MIRROR}" >> "${envfile}"
  fi
}

install_cube() {
  if cube_installed; then
    local ver="version unknown"
    if [[ -r "${CUBE_TOOLBOX}/VERSION.txt" ]]; then
      # release_version=v0.7.0-arm64
      ver="$(sed -n 's/^release_version=//p' "${CUBE_TOOLBOX}/VERSION.txt" | head -1)"
      : "${ver:=version unknown}"
    fi
    log "CubeSandbox already installed (${ver})"
    return 0
  fi

  if [[ "${CUBE_ARCH}" == "amd64" && -z "${CUBE_ONE_CLICK_TARBALL:-}" \
        && "${CUBE_FORCE_TARBALL:-0}" != "1" ]]; then
    log "installing CubeSandbox via online-install.sh"
    # CUBE_PVM_ENABLE stays 0: we require native KVM on metal, and the PVM
    # host kernel would be an unnecessary variable in a benchmark.
    curl -sL "https://github.com/${CUBE_REPO}/raw/master/deploy/one-click/online-install.sh" \
      | MIRROR="${MIRROR:-}" CUBE_PVM_ENABLE=0 bash
    return 0
  fi

  local version
  version="$(resolve_cube_version || true)"
  [[ -n "${version}" ]] && log "CubeSandbox release: ${version}"
  install_from_tarball "${version}"
}

# ------------------------------------------------------------------
# 3. Tune for a 192-vCPU host running ~1024 sandboxes
# ------------------------------------------------------------------
tune_cubelet() {
  local cfg="${CUBE_TOOLBOX}/Cubelet/config/config.toml"
  if [[ ! -f "${cfg}" ]]; then
    warn "Cubelet config not found at ${cfg}; skipping tuning (defaults apply)"
    return 0
  fi
  local helper="$(cd "$(dirname "$0")" && pwd)/tune_config.py"
  if [[ ! -f "${helper}" ]]; then
    warn "scripts/tune_config.py not found; leaving config at defaults"
    return 0
  fi

  # Key-level, idempotent edits. Do NOT append `[section] key = value`
  # blocks: the shipped config already defines every section touched here,
  # so appending produces duplicate TOML tables and Cubelet refuses to
  # start - which, applied to a live installation, takes out a working
  # node. tune_config.py validates with tomllib and restores on failure.
  #
  # What each override is for:
  #   tap_init_num - Cubelet pre-allocates this many TAP devices. A create
  #     burst that drains the idle pool blocks on TAP allocation, so 500
  #     throttles a c=1024 sweep and does it by degrading create latency,
  #     which is indistinguishable from a genuine result.
  #   cgroup pool_size - pre-created sandbox cgroup slots. 3000 is already
  #     the default and matches CubeMaster's node_max_mvm_num; set
  #     explicitly so a future default change cannot silently cap us.
  #   workflow create/destroy concurrent - how many create and destroy
  #     workflows Cubelet runs at once. 100 is a queue rather than a
  #     failure at c=1024, but it caps achievable churn for the `fresh`
  #     strategy, where every rollout is one create plus one destroy.
  log "applying arl-bench overrides to ${cfg}"
  if ! python3 "${helper}" "${cfg}" \
      "plugins.\"io.cubelet.internal.v1.network\".tap_init_num=${CUBE_TAP_INIT_NUM}" \
      "plugins.\"io.cubelet.internal.v1.cgroup\".pool_size=${CUBE_CGROUP_POOL_SIZE}" \
      "plugins.\"io.cubelet.workflow.v1.workflow\".flows.create.concurrent=${CUBE_WORKFLOW_CONCURRENT}" \
      "plugins.\"io.cubelet.workflow.v1.workflow\".flows.destroy.concurrent=${CUBE_WORKFLOW_CONCURRENT}"; then
    die "Cubelet config tuning failed; ${cfg} was left unchanged.
     Inspect it and re-run, or start the service with upstream defaults."
  fi
  CUBELET_RETUNED=1
}

tune_sysctl() {
  # Each sandbox gets a TAP device, an IP from the CubeVS pool and eBPF
  # policy entries. At ~1024 live sandboxes plus churn, the stock conntrack
  # table and ARP GC thresholds are the first things to break, and they
  # fail as intermittent connection errors rather than as anything obvious.
  log "applying host sysctl / limits tuning"
  cat > /etc/sysctl.d/99-arl-bench.conf <<'CONF'
# arl-bench: thousands of short-lived sandbox network interfaces
net.netfilter.nf_conntrack_max = 1048576
net.ipv4.neigh.default.gc_thresh1 = 8192
net.ipv4.neigh.default.gc_thresh2 = 32768
net.ipv4.neigh.default.gc_thresh3 = 65536
net.core.somaxconn = 65535
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_tw_reuse = 1
fs.file-max = 4194304
fs.inotify.max_user_instances = 65536
vm.max_map_count = 1048576
CONF
  sysctl --system >/dev/null 2>&1 || warn "sysctl --system reported errors"

  # Cubelet holds fds for every sandbox's TAP device, vsock and shim
  # socket; CubeProxy holds two sockets per in-flight request.
  local unit units
  units="$(systemctl list-unit-files 2>/dev/null || true)"
  for unit in cube-sandbox-cubelet cube-sandbox-cube-api cube-sandbox-cubemaster; do
    # Was `systemctl list-unit-files | grep -q`, which under pipefail made
    # this `continue` on every unit and silently applied no limits at all.
    out_matches "${units}" "^${unit}\.service" || continue
    mkdir -p "/etc/systemd/system/${unit}.service.d"
    cat > "/etc/systemd/system/${unit}.service.d/99-arl-bench.conf" <<'CONF'
[Service]
LimitNOFILE=1048576
LimitNPROC=1048576
CONF
  done
  systemctl daemon-reload
}

# ------------------------------------------------------------------
# 4. Restart + verify
# ------------------------------------------------------------------
restart_if_retuned() {
  [[ "${CUBELET_RETUNED:-0}" == "1" ]] || return 0
  log "restarting cube-sandbox-cubelet to pick up the config change"
  systemctl restart cube-sandbox-cubelet || warn "cubelet restart reported an error"
  # Cubelet re-registers with CubeOps on start; give it a moment before the
  # health checks below run.
  sleep 5
}

verify() {
  log "waiting for CubeAPI at ${CUBE_API_URL}/health"
  local ok=0
  for _ in $(seq 1 120); do
    if curl -fsS -o /dev/null "${CUBE_API_URL}/health" 2>/dev/null; then
      ok=1; break
    fi
    sleep 1
  done
  if (( ok != 1 )); then
    echo "--- systemctl status cube-sandbox-cube-api ---" >&2
    systemctl status cube-sandbox-cube-api --no-pager >&2 || true
    echo "--- journalctl -u cube-sandbox-cube-api (last 60) ---" >&2
    journalctl -u cube-sandbox-cube-api --no-pager -n 60 >&2 || true
    if [[ -n "${CUBE_INSTALLER_RC:-}" ]]; then
      echo "NOTE: the installer also exited ${CUBE_INSTALLER_RC}; that failure" >&2
      echo "      is likely the real cause rather than a false negative." >&2
    fi
    die "CubeAPI did not become healthy. See the logs above."
  fi
  log "CubeAPI is healthy at ${CUBE_API_URL}"
  # Note for anyone reading the JSON: /health always reports
  # "sandboxes": 0. It is hardcoded in CubeAPI, not a live count. The real
  # count comes from CubeOps below.

  # CubeProxy carries every in-sandbox request, so a benchmark that cannot
  # reach it will fail on its first data-plane call rather than at startup.
  # It has no health route of its own; any answer proves it is listening
  # (a bare request has no sandbox to route to, so 400/404 is success).
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${CUBE_PROXY_URL}/" 2>/dev/null || echo 000)"
  if [[ "${code}" == "000" ]]; then
    warn "CubeProxy is not answering at ${CUBE_PROXY_URL}.
     Every in-sandbox request goes through it unless CUBE_TRANSPORT=direct.
     Check: docker ps --filter name=cube-proxy   (host networking, port
     ${CUBE_PROXY_URL##*:} by default via CUBE_PROXY_HTTP_PORT)"
  else
    log "CubeProxy is listening at ${CUBE_PROXY_URL} (HTTP ${code} to a bare request)"
  fi

  # Node registration: this is what tells us the sandbox ceiling and
  # whether the node is schedulable at all.
  local nodes
  nodes="$(curl -fsS --max-time 5 "${CUBE_OPS_URL}/internal/v1/nodes" 2>/dev/null || echo "")"
  if [[ -n "${nodes}" ]]; then
    echo "${nodes}" | python3 -c '
import json, sys
try:
    nodes = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for n in nodes or []:
    # Every numeric field is omitempty, so an idle node omits mvm_num
    # entirely rather than reporting 0.
    print("    node=%s ip=%s cpus=%s mem=%sMB sandboxes=%s/%s healthy=%s" % (
        n.get("InstanceID") or n.get("IP"), n.get("IP"),
        n.get("CpuTotal", "?"), n.get("MemMBTotal", "?"),
        n.get("mvm_num", 0), n.get("MaxMvmLimit", "?"), n.get("Healthy")))
' || true
  else
    warn "CubeOps at ${CUBE_OPS_URL} did not answer /internal/v1/nodes.
     Not fatal - only the node-level columns of the report need it - but
     the suite then cannot see the sandbox ceiling or node health."
  fi

  # Sanity-check the node against the sweep that is about to run. MaxMvmLimit
  # is DERIVED from node resources, not the `node_max_mvm_num` default - on a
  # 64-core/125 GiB host it came out at 313, nowhere near 3000 - so a
  # concurrency level above it cannot be served and the point would silently
  # measure the ceiling instead of the workload.
  local ceiling top
  ceiling="$(echo "${nodes}" | python3 -c '
import json, sys
try:
    print(max(int(n.get("MaxMvmLimit") or 0) for n in json.load(sys.stdin)))
except Exception:
    print(0)
' 2>/dev/null || echo 0)"
  top="$(printf '%s\n' "${CONCURRENCIES:-32,128,512,1024}" | tr ',' '\n' \
         | sort -n | tail -1)"
  if [[ "${ceiling}" =~ ^[0-9]+$ ]] && (( ceiling > 0 )) \
     && [[ "${top}" =~ ^[0-9]+$ ]] && (( top > ceiling )); then
    warn "this node reports MaxMvmLimit=${ceiling} live sandboxes, but the
     configured sweep goes up to CONCURRENCIES=${top}. Levels above the
     ceiling cannot be served: creates will fail and the point will measure
     the limit rather than the workload (the suite flags those as
     control_plane_saturated / node_at_sandbox_capacity).
     Either lower CONCURRENCIES or raise scheduler.node_max_mvm_num in
     ${CUBE_TOOLBOX}/CubeMaster/conf.yaml and give the node the CPU/memory
     to back it."
  elif (( ceiling > 0 )); then
    log "node sandbox ceiling: ${ceiling} (top configured concurrency: ${top})"
  fi
}

main() {
  need_root
  check_arch
  check_kvm
  check_glibc
  check_docker
  check_reflink_fs
  check_disk
  install_cube
  # Re-check now that the install has populated $CUBE_DATA_DIR.
  check_disk
  tune_cubelet
  tune_sysctl
  restart_if_retuned
  verify
  log "done."
}

main "$@"
