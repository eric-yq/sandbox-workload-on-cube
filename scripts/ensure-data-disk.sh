#!/usr/bin/env bash
# Prepare the extra data volume CubeSandbox needs, on this host.
#
#   sudo bash scripts/ensure-data-disk.sh
#   DRY_RUN=1 bash scripts/ensure-data-disk.sh        # print, change nothing
#   DATA_DEVICE=/dev/nvme1n1 sudo bash ...            # pick the disk yourself
#   ASSUME_YES=1 sudo bash ...                        # no confirmation prompt
#
# For the workflow "launch a metal instance with a root volume plus one
# empty data volume, SSH in, run setup-host.sh". It finds that spare
# volume, makes the filesystem Cube requires, and mounts it.
#
# Why this exists: ensure-cube.sh HARD-FAILS when $CUBE_DATA_DIR is not on
# a reflink-capable filesystem, and Ubuntu/Debian default to ext4 - so a
# fresh instance stops at
#     ==> filesystem for /data/cubelet: ext4
#     ERROR: /data/cubelet is on ext4, which cannot do reflink ...
# before anything is downloaded. This script turns that dead end into an
# automatic fix, and must therefore run BEFORE ensure-cube.sh.
#
# Relationship to the tools/ flow: tools/remote/setup-disks.sh does the same
# job but identifies the disk by matching an EBS volume ID against the NVMe
# serial, because the workstation driving it knows that ID from the AWS API.
# On-box there is no volume ID to match, so selection here is by device
# PROPERTIES (see spare_block_devices in _lib.sh). Everything after
# selection - mkfs flags, fstab by UUID, the directory layout, the FICLONE
# proof - is deliberately identical.
#
# NOT done here: the bind mount for $CUBE_TOOLBOX/cubebox_os_image. Its
# target lives inside CubeSandbox's install root, and the one-click
# installer refuses to run when that root is non-empty but holds none of
# its own markers, so creating it now would block the install. Only the
# destination directory is prepared; setup-host.sh runs
# tools/remote/bind-image-dir.sh after the install instead.
#
# Idempotent: a host that is already prepared is a no-op.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/_lib.sh
load_env

DATA_MOUNT="${DATA_MOUNT:-/data}"
CUBE_DATA_DIR="${CUBE_DATA_DIR:-/data/cubelet}"
CUBE_TOOLBOX="${CUBE_TOOLBOX:-/usr/local/services/cubetoolbox}"
DATA_FS_LABEL="${DATA_FS_LABEL:-ARLDATA}"
DATA_DEVICE="${DATA_DEVICE:-}"
DRY_RUN="${DRY_RUN:-0}"
ASSUME_YES="${ASSUME_YES:-0}"
# Overridable so the test path can exercise the real fstab logic without
# writing into the host's /etc/fstab. Not meant to be set in normal use.
FSTAB="${FSTAB:-/etc/fstab}"

dry() { [[ "${DRY_RUN}" == "1" ]]; }

# Every mutation goes through this, so DRY_RUN=1 is a property of the
# script rather than something each call site has to remember.
run() {
  if dry; then
    echo "    [dry-run] $*"
    return 0
  fi
  "$@"
}

# Appending to /etc/fstab needs a shell redirect, which `run` cannot carry,
# and it must never add a second copy of a line on a re-run.
fstab_add() {
  local line="$1"
  if grep -qF -- "${line}" "${FSTAB}" 2>/dev/null; then
    log "already in ${FSTAB}: ${line}"
    return 0
  fi
  if dry; then
    echo "    [dry-run] append to ${FSTAB}: ${line}"
    return 0
  fi
  printf '%s\n' "${line}" >> "${FSTAB}"
  log "added to ${FSTAB}: ${line}"
}

confirm() {
  local prompt="$1"
  if [[ "${ASSUME_YES}" == "1" ]]; then
    log "${prompt} -> yes (ASSUME_YES=1)"
    return 0
  fi
  # A non-interactive run must not hang on `read`, and must not silently
  # assume yes for something that erases a disk.
  if [[ ! -t 0 ]]; then
    die "${prompt}
     stdin is not a terminal, so this cannot be confirmed interactively.
     Re-run with ASSUME_YES=1 if you are sure, or DRY_RUN=1 to preview."
  fi
  local reply
  read -r -p "==> ${prompt} [y/N] " reply
  [[ "${reply}" =~ ^[Yy]$ ]]
}

# ------------------------------------------------------------------
# 1. Is there anything to do?
# ------------------------------------------------------------------
# The question is not "is a disk mounted at /data" but "can $CUBE_DATA_DIR
# do FICLONE", because that is the only thing ensure-cube.sh cares about. A
# btrfs root, or a volume someone mounted by hand, is a perfectly good
# answer and must not be reformatted.
already_prepared() {
  local probe
  reflink_probe "${CUBE_DATA_DIR}"; probe=$?
  case ${probe} in
    0) return 0 ;;
    2) # Could not test. On a non-root dry run that is expected, not a
       # reason to go looking for a disk to format.
       if dry || [[ "${EUID}" -ne 0 ]]; then
         warn "cannot write to ${CUBE_DATA_DIR} to test FICLONE (not root?);"
         warn "assuming it is NOT prepared so the rest of the plan is visible"
         return 1
       fi
       return 1 ;;
    *) return 1 ;;
  esac
}

if already_prepared; then
  log "${CUBE_DATA_DIR} already supports reflink copy-on-write - nothing to do"
  findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS --target "${CUBE_DATA_DIR}" 2>/dev/null \
    | sed 's/^/    /' || true
  exit 0
fi

log "${CUBE_DATA_DIR} is not on a reflink-capable filesystem; looking for a data volume"

# ------------------------------------------------------------------
# 2. Choose the disk
# ------------------------------------------------------------------
ROOT_DISK="$(root_disk_device)"
[[ -n "${ROOT_DISK}" ]] && log "root volume is ${ROOT_DISK} (never a candidate)"

CANDIDATES="$(spare_block_devices)"

if [[ -n "${DATA_DEVICE}" ]]; then
  # An explicit choice still gets every safety check below; it only skips
  # the "which one did you mean" question.
  [[ -b "${DATA_DEVICE}" ]] || die "DATA_DEVICE=${DATA_DEVICE} is not a block device"
  DEV="${DATA_DEVICE}"
  log "using DATA_DEVICE=${DEV}"
else
  case "$(wc -w <<< "${CANDIDATES}")" in
    0)
      echo "--- lsblk -o NAME,SIZE,TYPE,FSTYPE,SERIAL,MOUNTPOINTS" >&2
      lsblk -o NAME,SIZE,TYPE,FSTYPE,SERIAL,MOUNTPOINTS >&2 || true
      die "no unused block device on this host, and ${CUBE_DATA_DIR} cannot do
     reflink, so there is nothing safe to format. Reformatting the device
     that currently backs ${CUBE_DATA_DIR} would destroy this machine, so
     no mkfs command is offered.

     Attach an EBS volume (gp3, >= 500 GiB for a full sweep, provisioned
     IOPS) to this instance and re-run. It will be mounted at ${DATA_MOUNT}.

     If the volume IS attached, it may still be settling - wait a few
     seconds and re-run. If it already holds a filesystem this script will
     not touch it; see tools/remote/setup-disks.sh and its FORCE_MKFS=1
     escape hatch, or wipe it yourself first.

     For FUNCTIONAL verification only, where disk numbers do not matter, a
     loopback XFS also satisfies reflink:
       sudo truncate -s 25G /var/lib/cube-xfs.img
       sudo mkfs.xfs -m reflink=1 /var/lib/cube-xfs.img
       sudo mkdir -p ${CUBE_DATA_DIR}
       sudo mount -o loop /var/lib/cube-xfs.img ${CUBE_DATA_DIR}
     Do not benchmark storage on that - it is an extra block layer."
      ;;
    1)
      DEV="${CANDIDATES}"
      log "found one unused block device: ${DEV}"
      ;;
    *)
      # Never pick for the user. Two empty disks is exactly the shape of
      # "an EBS data volume plus an instance-store volume", and choosing
      # the wrong one silently puts the benchmark's storage on ephemeral
      # disk - or throws away the volume they meant to keep.
      echo "--- lsblk -o NAME,SIZE,TYPE,FSTYPE,SERIAL,MOUNTPOINTS" >&2
      lsblk -o NAME,SIZE,TYPE,FSTYPE,SERIAL,MOUNTPOINTS >&2 || true
      die "more than one unused block device: ${CANDIDATES}
     Refusing to choose. Name the one you want explicitly:
       DATA_DEVICE=<device> sudo bash scripts/ensure-data-disk.sh
     (EBS volumes report a SERIAL beginning with 'vol'; instance-store
     volumes do not, and their contents are lost on stop/start.)"
      ;;
  esac
fi

# ------------------------------------------------------------------
# 3. Safety gates - these run for an explicit DATA_DEVICE too
# ------------------------------------------------------------------
# This is the guard that makes a wrong choice harmless instead of fatal.
if [[ -n "${ROOT_DISK}" && "${DEV}" == "${ROOT_DISK}" ]]; then
  die "${DEV} is the disk backing / - refusing to touch it.
     Inspect with: lsblk -o NAME,SIZE,FSTYPE,SERIAL,MOUNTPOINTS"
fi

mounted="$(lsblk -no MOUNTPOINTS "${DEV}" 2>/dev/null | tr -d ' ' | grep -v '^$' || true)"
if [[ -n "${mounted}" ]]; then
  die "${DEV} has mounted filesystems: ${mounted}
     Refusing to reformat. Unmount them first if this is really the disk."
fi

# Root is needed from here on: inspecting an existing filesystem means
# mounting it, and everything after that writes.
if ! dry && [[ "${EUID}" -ne 0 ]]; then
  die "must run as root (mkfs, mount, ${FSTAB})
     try: sudo bash scripts/ensure-data-disk.sh
     or preview without root: DRY_RUN=1 bash scripts/ensure-data-disk.sh"
fi

# xfsprogs is needed before the inspection below, not just for mkfs.
if ! have_cmd mkfs.xfs || ! have_cmd xfs_info; then
  log "installing xfsprogs"
  if have_cmd apt-get; then
    run apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xfsprogs
  elif have_cmd dnf; then
    run dnf install -y xfsprogs
  else
    die "xfsprogs is missing and no supported package manager was found"
  fi
fi

# Does the disk already carry a filesystem?
#
# Adopting a reflink-capable XFS matters more than it looks. Without it, a
# run that formats the disk and then fails at any later step (a bad fstab
# path, a mount that needs a retry, an interrupted shell) strands the host:
# the disk is now correct but unmounted, and every re-run refuses it for
# being non-empty. The user is then stuck with a "wipe it yourself" message
# about a disk that is already exactly what they wanted.
# tools/remote/setup-disks.sh makes the same choice for the same reason.
existing_fs="$(lsblk -rno FSTYPE "${DEV}" 2>/dev/null | tr -d ' ' | grep -v '^$' | head -1 || true)"
NEEDS_MKFS=1
if [[ "${existing_fs}" == "xfs" ]]; then
  if dry; then
    echo "    [dry-run] inspect the existing XFS on ${DEV} for reflink=1"
    NEEDS_MKFS=0
  else
    probe_mnt="$(mktemp -d)"
    if mount "${DEV}" "${probe_mnt}" 2>/dev/null; then
      xinfo="$(xfs_info "${probe_mnt}" 2>/dev/null || true)"
      umount "${probe_mnt}"
      rmdir "${probe_mnt}" 2>/dev/null || true
      if out_matches "${xinfo}" 'reflink=1'; then
        log "${DEV} is already XFS with reflink=1 - adopting it, data preserved"
        NEEDS_MKFS=0
      else
        # Only reachable with reflink off, which Cubelet cannot use at all.
        # Recreating is destructive, so it stays a manual decision.
        die "${DEV} is XFS but WITHOUT reflink, which Cubelet cannot use.
     It has to be recreated, which ERASES it. If that is what you want:
       sudo wipefs -a ${DEV}
     then re-run."
      fi
    else
      rmdir "${probe_mnt}" 2>/dev/null || true
      die "${DEV} reports an XFS filesystem but could not be mounted for
     inspection. Check it by hand: sudo mount ${DEV} /mnt"
    fi
  fi
elif [[ -n "${existing_fs}" ]]; then
  die "${DEV} already holds a ${existing_fs} filesystem and would be ERASED.
     This script only formats empty disks, or adopts an existing XFS that
     already has reflink. If you are certain this disk is free:
       sudo wipefs -a ${DEV}
     then re-run. (The tools/ flow has a FORCE_MKFS=1 escape for this.)"
fi

# EBS NVMe devices report a serial starting with the volume ID ("vol...").
# Instance-store volumes do not. Formatting one works, but everything on it
# disappears on stop/start, which is a nasty surprise for a 4-hour sweep.
serial="$(lsblk -dno SERIAL "${DEV}" 2>/dev/null | tr -d ' ' || true)"
if [[ -n "${serial}" && "${serial}" != vol* ]]; then
  warn "${DEV} has serial '${serial}', which does not look like an EBS volume."
  warn "If this is instance-store, its contents are LOST on stop/start."
fi

size="$(lsblk -dno SIZE "${DEV}" 2>/dev/null | tr -d ' ' || echo '?')"

# ------------------------------------------------------------------
# 4. Confirm, then do it
# ------------------------------------------------------------------
echo
log "plan:"
if (( NEEDS_MKFS )); then
  echo "    device        ${DEV}  (${size}, serial ${serial:-none}, empty)"
  echo "    filesystem    CREATE xfs, reflink=1, label ${DATA_FS_LABEL}"
else
  echo "    device        ${DEV}  (${size}, serial ${serial:-none})"
  echo "    filesystem    keep the existing XFS (reflink=1 already present)"
fi
echo "    mount         ${DATA_MOUNT}        (persisted in ${FSTAB} by UUID)"
echo "    directories   ${CUBE_DATA_DIR}, ${DATA_MOUNT}/cubebox_os_image"
echo

# Only ask when something is actually destroyed. Prompting to "confirm" a
# no-op trains people to say yes without reading, which is the opposite of
# what the prompt is for.
if ! dry && (( NEEDS_MKFS )); then
  confirm "Create a new XFS filesystem on ${DEV}? ALL DATA ON IT IS LOST." \
    || die "aborted - nothing was changed"
fi

if (( NEEDS_MKFS )); then
  # reflink=1 is the whole point: cubecow implements snapshot / clone /
  # rollback with the FICLONE ioctl, which ext4 cannot do, and Cubelet
  # refuses to start without it.
  #
  # No -f on purpose. The checks above already established the disk is
  # empty, so -f would only serve to override a signature they missed -
  # i.e. it would turn "we were wrong about this disk" from an error into
  # data loss.
  log "creating XFS with reflink=1 on ${DEV}"
  run mkfs.xfs -m reflink=1 -L "${DATA_FS_LABEL}" "${DEV}"
fi

# fstab by UUID, not by device name: on bare metal the NVMe name can change
# across a stop/start and a name-based entry would fail the next boot.
if dry; then
  echo "    [dry-run] read UUID from ${DEV} and add an fstab entry for ${DATA_MOUNT}"
else
  UUID="$(blkid -s UUID -o value "${DEV}")"
  [[ -n "${UUID}" ]] || die "could not read a UUID from ${DEV} after mkfs"
  log "UUID ${UUID}"
  run mkdir -p "${DATA_MOUNT}"
  fstab_add "UUID=${UUID} ${DATA_MOUNT} xfs defaults,noatime 0 2"
  # Mount the device explicitly rather than `mount ${DATA_MOUNT}`, which
  # would depend on the fstab entry having been parsed correctly this
  # instant. The fstab line is for the NEXT boot; this is for now.
  findmnt -no TARGET "${DATA_MOUNT}" >/dev/null 2>&1 \
    || run mount "${DEV}" "${DATA_MOUNT}"
fi

# Mount at ${DATA_MOUNT}, not at ${CUBE_DATA_DIR}: CubeMaster stages rootfs
# artifacts under ${DATA_MOUNT}/CubeMaster, and the converted per-template
# rootfs images (~3 GiB each, 7 templates) go to
# ${CUBE_TOOLBOX}/cubebox_os_image. Mounting one level down leaves both on
# the root volume - that is how the c7g.metal trial filled a 38 GiB root to
# 89% and failed a template build with a misleading "insufficient disk
# space" while the data volume sat empty.
run mkdir -p "${CUBE_DATA_DIR}" "${DATA_MOUNT}/cubebox_os_image"

# ------------------------------------------------------------------
# 5. Prove it
# ------------------------------------------------------------------
if dry; then
  echo "    [dry-run] verify xfs_info reports reflink=1 and FICLONE works"
  log "dry run complete - nothing was changed"
  exit 0
fi

log "verifying"
xinfo="$(xfs_info "${DATA_MOUNT}" 2>/dev/null || true)"
out_matches "${xinfo}" 'reflink=1' \
  || die "${DATA_MOUNT} is mounted but xfs_info does not report reflink=1"
log "xfs_info(${DATA_MOUNT}) reports reflink=1"

# The flag is an indicator; the ioctl is the requirement.
reflink_probe "${CUBE_DATA_DIR}"; probe=$?
case ${probe} in
  0) log "FICLONE works on ${CUBE_DATA_DIR}" ;;
  2) die "could not test FICLONE in ${CUBE_DATA_DIR}" ;;
  *) die "cp --reflink=always failed on ${CUBE_DATA_DIR}; Cubelet will not start" ;;
esac

echo
log "layout:"
findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS "${DATA_MOUNT}" | sed 's/^/    /'
df -h / "${DATA_MOUNT}" | sed 's/^/    /'
echo
log "data disk ready"
log "note: ${CUBE_TOOLBOX}/cubebox_os_image is bind-mounted later, after"
log "      CubeSandbox installs (the installer refuses a non-empty root)"
