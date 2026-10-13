#!/usr/bin/env bash
# Runs ON the instance, uploaded by tools/02-prepare-disks.sh.
#
#   setup-disks.sh [<data-volume-id>] [<mount-point>]
#
# Creates the XFS-with-reflink filesystem CubeSandbox requires and mounts
# it, plus the directories that keep template rootfs images off the root
# volume.
#
# Two backings, selected by DATA_BACKING (auto | instance-store | ebs):
#
#   instance-store  All local NVMe SSDs (c8gd / m7id / i4i / ... families)
#                   are striped into one LVM logical volume. Much faster
#                   than the gp3 data volume and free with the instance,
#                   but WIPED by a stop/start - see "ephemeral" below.
#   ebs             The single gp3 data volume created by 01, located by
#                   matching its volume ID against the NVMe serial.
#
# `auto` picks instance-store when local NVMe SSDs are present, else EBS.
#
# Devices are never identified by name. On Nitro the kernel enumerates NVMe
# devices in the order they respond to discovery, which is neither the
# block-device-mapping order nor stable across boots - so "the data volume
# is /dev/nvme1n1" is a guess, and the cost of guessing wrong is mkfs on the
# root filesystem. EBS volumes are matched by serial (the volume ID), local
# SSDs by the controller model string.
#   https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html
#
# Ephemeral storage: instance-store contents survive a reboot but NOT a
# stop/start - the devices come back present and blank. So this script
# rebuilds them without demanding FORCE_MKFS (the data is already gone), and
# the mount is added to fstab with `nofail` so a boot cannot block on a UUID
# that no longer exists. After a stop/start, re-run tools/02-prepare-disks.sh.

set -euo pipefail

VOL_ID="${1:-}"
MOUNT="${2:-/data}"
LABEL="ARLDATA"
VG_NAME="arlvg"
LV_NAME="arldata"
# 256 KiB stripes: large enough that Cube's template/CoW I/O lands as whole
# chunks on one disk, small enough that a single big read still fans out.
STRIPE_KB=256
CUBE_TOOLBOX="/usr/local/services/cubetoolbox"
CUBE_IMG_DIR="${CUBE_TOOLBOX}/cubebox_os_image"

# Model string Nitro reports for local SSDs. EBS reports "Amazon Elastic
# Block Store", so this cannot confuse the two.
INSTANCE_STORE_MODEL='Amazon EC2 NVMe Instance Storage'

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

ensure_pkg() {
  local cmd="$1" pkg="$2"
  command -v "${cmd}" >/dev/null 2>&1 && return 0
  log "installing ${pkg}"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkg}" >/dev/null
}

ensure_pkg xfs_info xfsprogs

# ------------------------------------------------------- find local SSDs
# Whole disks only (-d), and only those whose controller model marks them as
# instance storage.
#
# INSTANCE_STORE_DEVICES overrides the probe with an explicit space-separated
# list. Two uses: hardware whose controller reports a model string we do not
# recognise, and testing this script against loopback devices without a real
# 'd' instance. The safety gates below still apply to whatever is listed.
instance_store_devices() {
  local d name model
  if [[ -n "${INSTANCE_STORE_DEVICES:-}" ]]; then
    # Emit only non-empty words: an all-whitespace override has to read as
    # "no devices", not as one empty device name.
    # shellcheck disable=SC2086
    for d in ${INSTANCE_STORE_DEVICES}; do
      [[ -n "${d}" ]] && echo "${d}"
    done
    return 0
  fi
  while read -r name model; do
    [[ -z "${name}" ]] && continue
    [[ "${model}" == *"NVMe Instance Storage"* ]] && echo "/dev/${name}"
  done < <(lsblk -dno NAME,MODEL)
}

# --------------------------------------------------- locate the EBS volume
ebs_device_for() {
  local vol="$1" want got name serial
  # Serial as reported by the NVMe controller: the volume ID with the dash
  # stripped, e.g. vol-0abc... -> vol0abc...
  want="$(echo "${vol}" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
  log "looking for a block device whose serial is ${want} (${vol})" >&2
  local matches=()
  while read -r name serial; do
    [[ -z "${serial}" ]] && continue
    got="$(echo "${serial}" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
    [[ "${got}" == "${want}" ]] && matches+=("/dev/${name}")
  done < <(lsblk -dno NAME,SERIAL)

  if (( ${#matches[@]} == 0 )); then
    echo "--- lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINTS" >&2
    lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINTS >&2
    die "no block device has serial ${want}.
     The volume may still be attaching; wait a few seconds and re-run."
  fi
  (( ${#matches[@]} == 1 )) || die "more than one device matched ${want}: ${matches[*]}"
  echo "${matches[0]}"
}

# ----------------------------------------------------- decide the backing
BACKING="${DATA_BACKING:-auto}"
mapfile -t LOCAL_SSDS < <(instance_store_devices)

case "${BACKING}" in
  auto)
    if (( ${#LOCAL_SSDS[@]} > 0 )); then
      BACKING="instance-store"
    else
      BACKING="ebs"
    fi
    ;;
  instance-store)
    (( ${#LOCAL_SSDS[@]} > 0 )) || {
      echo "--- lsblk -o NAME,SIZE,TYPE,MODEL" >&2
      lsblk -o NAME,SIZE,TYPE,MODEL >&2
      die "DATA_BACKING=instance-store but no local NVMe SSD was found.
     Only the 'd' instance families have them (c8gd, m7id, i4i, ...).
     Expected a device whose model contains '${INSTANCE_STORE_MODEL}'."
    }
    ;;
  ebs) ;;
  *) die "DATA_BACKING must be auto, instance-store or ebs (got '${BACKING}')" ;;
esac
log "data backing: ${BACKING}"

EPHEMERAL=0

if [[ "${BACKING}" == "instance-store" ]]; then
  EPHEMERAL=1
  ensure_pkg pvs lvm2

  log "local NVMe SSDs: ${LOCAL_SSDS[*]}"
  total_bytes=0
  for d in "${LOCAL_SSDS[@]}"; do
    [[ -b "${d}" ]] || die "${d} is not a block device"
    bytes="$(sudo blockdev --getsize64 "${d}")"
    log "  ${d}  $(numfmt --to=iec --suffix=B "${bytes}" 2>/dev/null || echo "${bytes}B")"
    total_bytes=$(( total_bytes + bytes ))
  done
  log "total raw capacity $(numfmt --to=iec --suffix=B "${total_bytes}" 2>/dev/null \
        || echo "${total_bytes}B") across ${#LOCAL_SSDS[@]} disk(s)"

  # None of these may be the root disk or carry a mounted filesystem. The
  # model check already rules the root volume out (it is EBS), but a wrong
  # match here would be destructive, so assert it rather than assume it.
  root_src="$(findmnt -no SOURCE / || true)"
  root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null | head -1 || true)"
  for d in "${LOCAL_SSDS[@]}"; do
    [[ -n "${root_disk}" && "${d}" == "/dev/${root_disk}" ]] \
      && die "${d} is the disk backing / - refusing to use it as a PV"
    m="$(lsblk -no MOUNTPOINTS "${d}" | tr -d ' ' | grep -v '^$' | grep -v "^${MOUNT}\$" || true)"
    [[ -n "${m}" ]] && die "${d} has mounted filesystems: ${m}
     Refusing to use it as an LVM physical volume."
  done

  DEV="/dev/${VG_NAME}/${LV_NAME}"

  if [[ -b "${DEV}" ]]; then
    log "logical volume ${DEV} already exists - keeping it"
  else
    # A stop/start returns these disks blank, so any LVM metadata from a
    # previous boot is gone and rebuilding is the normal path, not an error.
    log "creating ${VG_NAME}/${LV_NAME} striped across ${#LOCAL_SSDS[@]} disk(s)"
    sudo vgchange -an "${VG_NAME}" >/dev/null 2>&1 || true
    sudo vgremove -f "${VG_NAME}" >/dev/null 2>&1 || true
    for d in "${LOCAL_SSDS[@]}"; do
      # Clears stale LVM/filesystem signatures so pvcreate does not prompt.
      sudo wipefs -a "${d}" >/dev/null 2>&1 || true
      sudo pvcreate -ff -y "${d}" >/dev/null || die "pvcreate failed on ${d}"
    done
    sudo vgcreate "${VG_NAME}" "${LOCAL_SSDS[@]}" >/dev/null \
      || die "vgcreate ${VG_NAME} failed"
    # Striped, not linear: a linear LV would cap throughput at one disk,
    # which defeats the point of using instance storage at all.
    if (( ${#LOCAL_SSDS[@]} > 1 )); then
      sudo lvcreate -y -n "${LV_NAME}" -i "${#LOCAL_SSDS[@]}" -I "${STRIPE_KB}k" \
        -l 100%FREE "${VG_NAME}" >/dev/null || die "lvcreate (striped) failed"
    else
      sudo lvcreate -y -n "${LV_NAME}" -l 100%FREE "${VG_NAME}" >/dev/null \
        || die "lvcreate failed"
    fi
    sudo vgchange -ay "${VG_NAME}" >/dev/null
    [[ -b "${DEV}" ]] || die "${DEV} did not appear after lvcreate"
  fi
  log "data device ${DEV} ($(lsblk -dno SIZE "${DEV}" | tr -d ' '))"
else
  [[ -n "${VOL_ID}" ]] || die "DATA_BACKING=ebs needs a data volume ID.
     usage: setup-disks.sh <data-volume-id> [mount-point]"
  DEV="$(ebs_device_for "${VOL_ID}")"
  log "data volume ${VOL_ID} is ${DEV}"

  # ----------------------------------------------------------- safety gate
  # Refuse to touch anything that is, or contains, a mounted filesystem.
  # This is the guard that makes a wrong match harmless instead of fatal.
  root_src="$(findmnt -no SOURCE / || true)"
  root_disk="$(lsblk -no PKNAME "${root_src}" 2>/dev/null | head -1 || true)"
  if [[ -n "${root_disk}" && "${DEV}" == "/dev/${root_disk}" ]]; then
    die "${DEV} is the disk backing / - refusing to touch it.
     The serial match must be wrong; inspect: lsblk -o NAME,SERIAL,MOUNTPOINTS"
  fi
  mounted="$(lsblk -no MOUNTPOINTS "${DEV}" | tr -d ' ' | grep -v '^$' || true)"
  if [[ -n "${mounted}" && "${mounted}" != "${MOUNT}" ]]; then
    die "${DEV} already has mounted filesystems: ${mounted}
     Refusing to reformat. Unmount them first if this is really the right disk."
  fi
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
  # The FORCE_MKFS gate protects *persistent* data. Instance storage has
  # none to protect: whatever was there is already gone, or we just built
  # the LV a few lines ago.
  if [[ -n "${existing_fs}" && "${FORCE_MKFS:-0}" != "1" && "${EPHEMERAL}" != "1" ]]; then
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
#
# For instance storage the entry additionally needs `nofail` and a short
# device timeout: a stop/start returns the disks blank, so this UUID will
# not exist on the next boot and a default entry would leave systemd
# blocking on the device unit instead of booting.
if (( EPHEMERAL )); then
  fs_opts="defaults,noatime,nofail,x-systemd.device-timeout=10s"
else
  fs_opts="defaults,noatime"
fi
if ! grep -q "UUID=${UUID}" /etc/fstab; then
  # Drop any stale entry for this mount point first, so repeated rebuilds of
  # ephemeral storage cannot accumulate dead UUIDs that break `mount -a`.
  if grep -qE "^[^#]*[[:space:]]${MOUNT}[[:space:]]" /etc/fstab; then
    log "removing a stale ${MOUNT} entry from /etc/fstab"
    sudo sed -i "\|^[^#]*[[:space:]]${MOUNT}[[:space:]]|d" /etc/fstab
  fi
  echo "UUID=${UUID} ${MOUNT} xfs ${fs_opts} 0 2" | sudo tee -a /etc/fstab >/dev/null
  log "added ${MOUNT} to /etc/fstab (by UUID, ${fs_opts})"
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
if (( EPHEMERAL )); then
  sudo lvs --noheadings -o vg_name,lv_name,lv_size,stripes,stripe_size \
    "${VG_NAME}/${LV_NAME}" 2>/dev/null | sed 's/^/    lv:/' || true
fi
df -h / "${MOUNT}" | sed 's/^/    /'
echo

# Reported on stdout so tools/02-prepare-disks.sh can record it in .state.
echo "SETUP_DISKS_BACKING=${BACKING}"
log "disks ready"
log "note: ${CUBE_IMG_DIR} is bind-mounted later, after CubeSandbox installs"
if (( EPHEMERAL )); then
  warn "${MOUNT} is on INSTANCE STORAGE: a stop/start wipes it."
  warn "After any stop/start, re-run: bash tools/02-prepare-disks.sh"
  warn "(a reboot is fine - instance storage survives that)"
fi
