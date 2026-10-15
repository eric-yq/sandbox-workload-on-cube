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

# `cmd | grep -q pat` is unsafe under `set -o pipefail` - see the long note on
# out_matches() in scripts/_lib.sh. The remote scripts are shipped standalone
# by scp and cannot source that file, so the helper is repeated here.
#
# That footgun bit this script specifically. The apt check below used
#   apt-cache policy <pkg> | grep -q 'Candidate: [0-9]'
# and measured PIPESTATUS=(141 0): grep matched, apt-cache then died of SIGPIPE
# writing into the closed pipe, pipefail promoted the 141. So the script
# reported "neither ... is available from apt" while the package was available
# the whole time, and a page-size A/B measurement went down a dead end.
out_matches() {
  local haystack="$1" pattern="$2"
  grep -qE "${pattern}" <<< "${haystack}"
}

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
  WANT_SUFFIX="-aws-64k"
  META_PKG="linux-aws-64k"
else
  WANT_PAGESIZE=4096
  WANT_SUFFIX="-aws"
  META_PKG="linux-aws"
fi

# Already there? Do nothing, so this is safe to call unconditionally from
# automation and never reboots a host that does not need it.
# Running the right page size is necessary but NOT sufficient: GRUB_DEFAULT
# must point at the same flavour, or the next unrelated reboot silently
# changes the page size under the operator. That state is reachable whenever a
# previous run pinned GRUB and the reboot was skipped or failed, so check both
# and fall through to the pinning logic when only GRUB is wrong.
grub_pinned_to() {  # $1 = -aws | -aws-64k
  local d
  d="$(grep '^GRUB_DEFAULT=' /etc/default/grub 2>/dev/null || true)"
  # Matches the menuentry-id form this script writes, e.g.
  #   ...>gnulinux-7.0.0-1014-aws-64k-advanced-<uuid>
  # The literal "-advanced-" immediately after the suffix is what stops a
  # "-aws" lookup from matching a "-aws-64k" entry. Anything else (GRUB's
  # default "0"/"saved", or a human-written title form) simply fails to match
  # and we re-pin, which is the safe direction.
  out_matches "${d}" "gnulinux-[0-9][0-9.-]*${1}-advanced-"
}

if [[ "${CUR_PAGESIZE}" == "${WANT_PAGESIZE}" ]]; then
  if grub_pinned_to "${WANT_SUFFIX}"; then
    log "already running a ${TARGET} kernel and GRUB agrees; nothing to do"
    echo "SWITCH_KERNEL_RESULT=no_change"
    exit 0
  fi
  log "already running a ${TARGET} kernel, but GRUB_DEFAULT does not point at"
  log "a ${WANT_SUFFIX} entry - re-pinning so the next reboot stays on ${TARGET}"
  REPIN_ONLY=1
fi
REPIN_ONLY="${REPIN_ONLY:-0}"

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

# An exact-version image is what we want; the meta package tracks "latest",
# which on a stale instance can be a different version than the one running.
EXACT_IMG="linux-image-${BASE_VER}${WANT_SUFFIX}"

# Does apt report a real Candidate version for this package?
apt_installable() {
  out_matches "$(apt-cache policy "$1" 2>/dev/null)" 'Candidate:[[:space:]]+[0-9]'
}

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

  if apt_installable "${EXACT_IMG}"; then
    log "installing ${EXACT_IMG} (matches the running version)"
    apt_retry install "${EXACT_IMG}" || die "failed to install ${EXACT_IMG}"
  elif apt_installable "${META_PKG}"; then
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

# The `|| true` on these two is not cosmetic: `head -1` exits after the first
# line, so under pipefail the upstream greps can be killed by SIGPIPE and the
# assignment would abort the script. The emptiness checks below are the real
# guard, so swallowing the pipeline status here is both safe and necessary.
SUB_ID="$(grep -oE "^submenu '[^']*' \\\$menuentry_id_option '[^']+'" /boot/grub/grub.cfg \
          | grep -oE "'gnulinux-advanced-[^']+'" | tr -d "'" | head -1 || true)"
# The closing quote right after ${HAVE} is what excludes the "(recovery
# mode)" entry, and it also keeps a -aws lookup from matching -aws-64k.
# Note `.*` and not `[^\n]*`: in ERE a bracket expression takes \n
# literally, so [^\n] means "not n, not backslash" and would fail on the
# "--class gnu-linux" text present on every menuentry line.
ENT_ID="$(grep -oE "menuentry '[^']*Linux ${HAVE}' .*\\\$menuentry_id_option '[^']+'" /boot/grub/grub.cfg \
          | grep -oE "'gnulinux-[^']+'" | tr -d "'" | head -1 || true)"

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
if [[ "${REPIN_ONLY}" == "1" ]]; then
  # We were already running the requested page size and only GRUB disagreed,
  # so the host is now consistent without a reboot. Reporting no_change keeps
  # 98-switch-kernel.sh from rebooting a host that does not need it.
  log "GRUB re-pinned; already running ${TARGET}, no reboot needed"
  echo "SWITCH_KERNEL_RESULT=no_change"
else
  echo "SWITCH_KERNEL_RESULT=reboot_required"
fi
