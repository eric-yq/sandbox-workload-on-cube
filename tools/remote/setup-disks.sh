#!/usr/bin/env bash
# Runs ON the instance, uploaded by tools/02-prepare-disks.sh.
#
#   setup-disks.sh <data-volume-id> <mount-point>
#
# Creates the XFS-with-reflink filesystem CubeSandbox requires and mounts
# it, plus the bind mount that keeps template rootfs images off the root
# volume.
#
# The device is located by matching the EBS volume ID against the NVMe
# serial number, never by device name. On Nitro instances the kernel
# enumerates NVMe devices in the order they respond to discovery, which is
# neither the block-device-mapping order nor stable across boots - so
# "the data volume is /dev/nvme1n1" is a guess, and the cost of guessing
# wrong here is mkfs on the root filesystem.
#   https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html

set -euo pipefail

VOL_ID="${1:?usage: setup-disks.sh <data-volume-id> <mount-point>}"
MOUNT="${2:-/data}"
LABEL="ARLDATA"
CUBE_TOOLBOX="/usr/local/services/cubetoolbox"
CUBE_IMG_DIR="${CUBE_TOOLBOX}/cubebox_os_image"

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

command -v xfs_info >/dev/null 2>&1 || {
  log "installing xfsprogs"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xfsprogs >/dev/null
}

# ---------------------------------------------------------------- locate
# Serial as reported by the NVMe controller: the volume ID with the dash
# stripped, e.g. vol-0abc... -> vol0abc...
want="$(echo "${VOL_ID}" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
log "looking for a block device whose serial is ${want} (${VOL_ID})"

matches=()
while read -r name serial; do
  [[ -z "${serial}" ]] && continue
  got="$(echo "${serial}" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
  [[ "${got}" == "${want}" ]] && matches+=("/dev/${name}")
done < <(lsblk -dno NAME,SERIAL)

if (( ${#matches[@]} == 0 )); then
  echo "--- lsblk -o NAME,SIZE,TYPE,SERIAL,MOUNTPOINTS" >&2
  lsblk -o NAME,SIZE,TYPE,SERIAL,MOUNTPOINTS >&2
  die "no block device has serial ${want}.
     The volume may still be attaching; wait a few seconds and re-run."
fi
(( ${#matches[@]} == 1 )) || die "more than one device matched ${want}: ${matches[*]}"
DEV="${matches[0]}"
log "data volume ${VOL_ID} is ${DEV}"

# ----------------------------------------------------------- safety gate
# Refuse to touch anything that is, or contains, a mounted filesystem.
# This is the guard that makes a wrong match harmless instead of fatal.
root_src="$(findmnt -no SOURCE / || true)"
root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null || true)"
if [[ -n "${root_disk}" && "${DEV}" == "/dev/${root_disk}" ]]; then
  die "${DEV} is the disk backing / - refusing to touch it.
     The serial match must be wrong; inspect: lsblk -o NAME,SERIAL,MOUNTPOINTS"
fi
mounted="$(lsblk -no MOUNTPOINTS "${DEV}" | tr -d ' ' | grep -v '^$' || true)"
if [[ -n "${mounted}" && "${mounted}" != "${MOUNT}" ]]; then
  die "${DEV} already has mounted filesystems: ${mounted}
     Refusing to reformat. Unmount them first if this is really the right disk."
fi

# ------------------------------------------------------- already set up?
existing_fs="$(lsblk -no FSTYPE "${DEV}" | tr -d ' ' | grep -v '^$' | head -1 || true)"
needs_mkfs=1
if [[ "${existing_fs}" == "xfs" ]]; then
  # Only skip if it is genuinely reflink-capable; an XFS without reflink is
  # useless to Cube and has to be rebuilt.
  tmpd="$(mktemp -d)"
  if sudo mount "${DEV}" "${tmpd}" 2>/dev/null; then
    if sudo xfs_info "${tmpd}" 2>/dev/null | grep -q 'reflink=1'; then
      log "${DEV} is already XFS with reflink=1 - keeping it (data preserved)"
      needs_mkfs=0
    else
      warn "${DEV} is XFS but WITHOUT reflink; it must be recreated"
    fi
    sudo umount "${tmpd}"
  fi
  rmdir "${tmpd}"
elif [[ -n "${existing_fs}" ]]; then
  warn "${DEV} currently holds a ${existing_fs} filesystem"
fi

if (( needs_mkfs )); then
  if [[ -n "${existing_fs}" && "${FORCE_MKFS:-0}" != "1" ]]; then
    die "${DEV} already has a ${existing_fs} filesystem and would be ERASED.
     If that is intended, re-run with FORCE_MKFS=1:
       FORCE_MKFS=1 bash tools/02-prepare-disks.sh"
  fi
  log "creating XFS with reflink=1 on ${DEV}"
  # reflink=1 is the whole point: Cube's cubecow backend implements
  # snapshot / clone / rollback with the FICLONE ioctl, which ext4 cannot
  # do, and Cubelet refuses to start without it.
  sudo mkfs.xfs -f -m reflink=1 -L "${LABEL}" "${DEV}"
fi

# ----------------------------------------------------------------- mount
UUID="$(sudo blkid -s UUID -o value "${DEV}")"
[[ -n "${UUID}" ]] || die "could not read a UUID from ${DEV}"
log "UUID ${UUID}"

sudo mkdir -p "${MOUNT}"

# fstab by UUID, not by device name: on bare metal the NVMe name can change
# across a stop/start and a name-based entry would fail the next boot.
if ! grep -q "UUID=${UUID}" /etc/fstab; then
  echo "UUID=${UUID} ${MOUNT} xfs defaults,noatime 0 2" | sudo tee -a /etc/fstab >/dev/null
  log "added ${MOUNT} to /etc/fstab (by UUID)"
fi
findmnt -no TARGET "${MOUNT}" >/dev/null 2>&1 || sudo mount "${MOUNT}"

# ------------------------------------------------ keep root volume clear
# Template rootfs images live under $CUBE_TOOLBOX/cubebox_os_image at roughly
# 3 GiB each, and CubeMaster stages builds under $MOUNT/CubeMaster. With the
# volume mounted one level down (at /data/cubelet) both land on the root
# volume instead: that is how the c7g.metal trial filled a 38 GiB root to 89%
# and made the fourth template build fail with a misleading "insufficient
# disk space" while the 200 GiB volume sat empty.
#
# Only the destination is prepared here. The bind mount itself is deliberately
# NOT created yet: its target lives inside CubeSandbox's install root, and the
# one-click installer refuses to proceed when that root is non-empty but holds
# none of its own markers -
#   "refusing to wipe install root ...: directory is not empty and contains
#    no CubeSandbox installation markers"
# so pre-creating the directory here blocks the install outright. The mount is
# established after the install, by tools/remote/bind-image-dir.sh.
sudo mkdir -p "${MOUNT}/cubebox_os_image" "${MOUNT}/cubelet"

# Undo a premature bind mount from an earlier version of this script, which
# is exactly what leaves the installer refusing to run.
if findmnt -no TARGET "${CUBE_IMG_DIR}" >/dev/null 2>&1; then
  if [[ ! -e "${CUBE_TOOLBOX}/.one-click.env" ]]; then
    warn "${CUBE_IMG_DIR} is bind-mounted but CubeSandbox is not installed yet;"
    warn "unmounting it so the installer does not refuse the non-empty root"
    sudo umount "${CUBE_IMG_DIR}" || die "could not unmount ${CUBE_IMG_DIR}"
    sudo rmdir "${CUBE_IMG_DIR}" 2>/dev/null || true
    sudo rmdir "${CUBE_TOOLBOX}" 2>/dev/null || true
  else
    log "${CUBE_IMG_DIR} bind mount already active (Cube is installed)"
  fi
fi
# The fstab entry is written by bind-image-dir.sh, after the install. Remove a
# stale one so `mount -a` cannot fail on a target that does not exist yet.
if grep -q " ${CUBE_IMG_DIR} " /etc/fstab && [[ ! -e "${CUBE_TOOLBOX}/.one-click.env" ]]; then
  log "removing the premature ${CUBE_IMG_DIR} fstab entry"
  sudo sed -i "\| ${CUBE_IMG_DIR} |d" /etc/fstab
fi

# ---------------------------------------------------------------- verify
log "verifying"
sudo xfs_info "${MOUNT}" | grep -q 'reflink=1' \
  || die "${MOUNT} is mounted but xfs_info does not report reflink=1"

# Prove FICLONE actually works rather than trusting the flag.
t="${MOUNT}/.reflink-test"
sudo dd if=/dev/zero of="${t}.src" bs=4k count=1 status=none
if sudo cp --reflink=always "${t}.src" "${t}.dst" 2>/dev/null; then
  log "FICLONE works on ${MOUNT}"
else
  sudo rm -f "${t}.src" "${t}.dst"
  die "cp --reflink=always failed on ${MOUNT}; Cubelet will not start"
fi
sudo rm -f "${t}.src" "${t}.dst"

echo
log "layout:"
findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS "${MOUNT}" | sed 's/^/    /'
df -h / "${MOUNT}" | sed 's/^/    /'
echo
log "disks ready"
log "note: ${CUBE_IMG_DIR} is bind-mounted later, after CubeSandbox installs"
