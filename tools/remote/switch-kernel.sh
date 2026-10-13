#!/usr/bin/env bash
# Prepare an arm64 instance to boot a 4 KiB- or 64 KiB-page kernel.
#
#   bash switch-kernel.sh 64k
#   bash switch-kernel.sh 4k
#
# Runs ON the instance. It installs the kernel if needed and pins GRUB, but
# deliberately does NOT reboot: rebooting from here would kill the SSH
# channel that carries our own exit status, so the caller (98-switch-kernel.sh)
# owns the reboot and the post-boot verification.
#
# Communicates back through a single sentinel line on stdout:
#   SWITCH_KERNEL_RESULT=no_change        already booted on the requested size
#   SWITCH_KERNEL_RESULT=reboot_required  GRUB pinned, caller should reboot
#
# Why page size is worth switching at all: on arm64 every TLB invalidation is
# broadcast to every PE in the inner-shareable domain and completed with
# dsb(ish). Larger pages mean fewer page-table entries per mapping and so
# fewer invalidations, which on a 192-core host is a large effect. Measured on
# c9g.metal-48xl, B1: +26% at c=64, +32% at c=128, +36% at c=256.
set -euo pipefail

TARGET="${1:-4k}"
case "${TARGET}" in
  4k|64k) ;;
  *) echo "ERROR: target must be 4k or 64k, got '${TARGET}'" >&2; exit 2 ;;
esac

log()  { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- guards
ARCH="$(uname -m)"
[[ "${ARCH}" == "aarch64" ]] || die "this host is ${ARCH}, not aarch64.
     A 64 KiB-page kernel is an Arm feature: the page size is set by the
     translation granule (TCR_EL1.TG0/TG1 supports 4K/16K/64K). x86-64
     page tables only define 4 KiB base pages, so Ubuntu ships no
     linux-image-*-64k for amd64 at all."

CUR_PAGESIZE="$(getconf PAGESIZE)"
CUR_KERNEL="$(uname -r)"
log "running kernel ${CUR_KERNEL}, page size ${CUR_PAGESIZE}"

if [[ "${TARGET}" == "64k" ]]; then
  WANT_PAGESIZE=65536
else
  WANT_PAGESIZE=4096
fi

# Already there? Do nothing, so this is safe to call unconditionally from
# automation and never reboots a host that does not need it.
if [[ "${CUR_PAGESIZE}" == "${WANT_PAGESIZE}" ]]; then
  log "already running a ${TARGET} kernel; nothing to do"
  echo "SWITCH_KERNEL_RESULT=no_change"
  exit 0
fi

# The running kernel's base version, e.g. 7.0.0-1014 from 7.0.0-1014-aws-64k.
# Used to keep page size the ONLY variable that changes.
BASE_VER="$(sed -E 's/-aws(-64k)?$//' <<<"${CUR_KERNEL}")"
log "base kernel version ${BASE_VER}"

# ------------------------------------------------------- install if needed
export DEBIAN_FRONTEND=noninteractive

apt_retry() {
  # Retry rather than fail fast: on a freshly booted Ubuntu the apt lock is
  # held by cloud-init and unattended-upgrades for a while.
  for _ in $(seq 1 30); do
    if sudo -n apt-get -y -o DPkg::Lock::Timeout=60 "$@" >/tmp/switch-kernel-apt.log 2>&1; then
      return 0
    fi
    sleep 10
  done
  echo "--- last 20 lines of apt output ---" >&2
  tail -20 /tmp/switch-kernel-apt.log >&2 || true
  return 1
}

if [[ "${TARGET}" == "64k" ]]; then
  WANT_SUFFIX="-aws-64k"
  META_PKG="linux-aws-64k"
else
  WANT_SUFFIX="-aws"
  META_PKG="linux-aws"
fi

# An exact-version image is what we want; the meta package tracks "latest",
# which on a stale instance can be a different version than the one running.
EXACT_IMG="linux-image-${BASE_VER}${WANT_SUFFIX}"

installed_kernel_for() {
  # Newest /boot/vmlinuz with exactly this suffix. The glob already excludes
  # the wrong flavour (-aws does not match -aws-64k), and the grep re-asserts
  # it; note the `--`, without which grep would read "-aws$" as options.
  ls -1 /boot/vmlinuz-*"${1}" 2>/dev/null \
    | sed "s|/boot/vmlinuz-||" \
    | grep -E -- "${1}\$" \
    | sort -V | tail -1
}

HAVE="$(installed_kernel_for "${WANT_SUFFIX}" || true)"
if [[ -z "${HAVE}" ]]; then
  log "no ${WANT_SUFFIX} kernel installed; installing"
  sudo -n apt-get -y -o DPkg::Lock::Timeout=60 update >/dev/null 2>&1 || true

  if apt-cache policy "${EXACT_IMG}" 2>/dev/null | grep -q 'Candidate: [0-9]'; then
    log "installing ${EXACT_IMG} (matches the running version)"
    apt_retry install "${EXACT_IMG}" || die "failed to install ${EXACT_IMG}"
  elif apt-cache policy "${META_PKG}" 2>/dev/null | grep -q 'Candidate: [0-9]'; then
    log "exact ${EXACT_IMG} unavailable; falling back to meta package ${META_PKG}"
    apt_retry install "${META_PKG}" || die "failed to install ${META_PKG}"
  else
    die "neither ${EXACT_IMG} nor ${META_PKG} is available from apt.
     Check: apt-cache search linux-image | grep 64k"
  fi
  HAVE="$(installed_kernel_for "${WANT_SUFFIX}" || true)"
  [[ -n "${HAVE}" ]] || die "install reported success but no ${WANT_SUFFIX} kernel is in /boot"
fi
log "target kernel ${HAVE}"

# Changing page size AND kernel version at once would confound any A/B
# measurement, so require an explicit opt-in for that.
HAVE_BASE="$(sed -E 's/-aws(-64k)?$//' <<<"${HAVE}")"
if [[ "${HAVE_BASE}" != "${BASE_VER}" ]]; then
  if [[ "${ALLOW_KERNEL_VERSION_CHANGE:-0}" != "1" ]]; then
    die "the only available ${TARGET} kernel is ${HAVE} (${HAVE_BASE}), but the
     running kernel is ${CUR_KERNEL} (${BASE_VER}).
     Switching would change page size AND kernel version together, so any
     before/after comparison would be confounded.
     Re-run with ALLOW_KERNEL_VERSION_CHANGE=1 if that is acceptable."
  fi
  echo "WARN: page size and kernel version both change (${BASE_VER} -> ${HAVE_BASE})" >&2
fi

# ------------------------------------------------------------ pin GRUB
# Relying on "newest kernel boots by default" is not good enough: -aws and
# -aws-64k usually share a version, so the default entry is ambiguous. Pin
# the exact submenu>entry id instead.
[[ -f /boot/grub/grub.cfg ]] || die "/boot/grub/grub.cfg not found"

SUB_ID="$(grep -oE "^submenu '[^']*' \\\$menuentry_id_option '[^']+'" /boot/grub/grub.cfg \
          | grep -oE "'gnulinux-advanced-[^']+'" | tr -d "'" | head -1)"
# The closing quote right after ${HAVE} is what excludes the "(recovery
# mode)" entry, and it also keeps a -aws lookup from matching -aws-64k.
# Note `.*` and not `[^\n]*`: in ERE a bracket expression takes \n
# literally, so [^\n] means "not n, not backslash" and would fail on the
# "--class gnu-linux" text present on every menuentry line.
ENT_ID="$(grep -oE "menuentry '[^']*Linux ${HAVE}' .*\\\$menuentry_id_option '[^']+'" /boot/grub/grub.cfg \
          | grep -oE "'gnulinux-[^']+'" | tr -d "'" | head -1)"

[[ -n "${SUB_ID}" ]] || die "could not find the 'Advanced options' submenu id in grub.cfg"
[[ -n "${ENT_ID}" ]] || die "could not find a grub menuentry for kernel ${HAVE}"
log "grub submenu ${SUB_ID}"
log "grub entry   ${ENT_ID}"

sudo -n cp -n /etc/default/grub /etc/default/grub.arl-backup 2>/dev/null || true
if grep -q '^GRUB_DEFAULT=' /etc/default/grub; then
  sudo -n sed -i "s|^GRUB_DEFAULT=.*|GRUB_DEFAULT=\"${SUB_ID}>${ENT_ID}\"|" /etc/default/grub
else
  echo "GRUB_DEFAULT=\"${SUB_ID}>${ENT_ID}\"" | sudo -n tee -a /etc/default/grub >/dev/null
fi
log "$(grep '^GRUB_DEFAULT=' /etc/default/grub)"

sudo -n update-grub >/tmp/switch-kernel-grub.log 2>&1 \
  || { tail -20 /tmp/switch-kernel-grub.log >&2; die "update-grub failed"; }
log "grub updated"

# Both kernels stay installed, so this is reversible by re-running with the
# other argument.
echo "SWITCH_KERNEL_RESULT=reboot_required"
