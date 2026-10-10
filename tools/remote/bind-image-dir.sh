#!/usr/bin/env bash
# Runs ON the instance, AFTER CubeSandbox is installed. Uploaded by
# tools/03-setup-env.sh.
#
#   bind-image-dir.sh <mount-point>
#
# Moves CubeSandbox's converted-rootfs directory onto the data volume and
# bind-mounts it back, so template images do not fill the root volume.
#
# Why this is a separate step rather than part of the disk setup: the target
# lives inside CubeSandbox's install root, and the one-click installer
# refuses to run when that root is non-empty but contains none of its own
# markers -
#
#   ERROR: refusing to wipe install root /usr/local/services/cubetoolbox:
#   directory is not empty and contains no CubeSandbox installation markers
#   (.one-click.env / CubeMaster / CubeAPI / Cubelet)
#
# Creating the directory before the install therefore blocks the install
# outright. So the order has to be: install, then relocate.
#
# Idempotent, and safe to run when the mount is already in place.

set -euo pipefail

MOUNT="${1:-/data}"
CUBE_TOOLBOX="/usr/local/services/cubetoolbox"
CUBE_IMG_DIR="${CUBE_TOOLBOX}/cubebox_os_image"
DEST="${MOUNT}/cubebox_os_image"

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ -d "${CUBE_TOOLBOX}" ]] || die "${CUBE_TOOLBOX} does not exist - is CubeSandbox installed?
     Run: sudo bash scripts/ensure-cube.sh"

findmnt -no TARGET "${MOUNT}" >/dev/null 2>&1 \
  || die "${MOUNT} is not mounted; run tools/02-prepare-disks.sh first"

if findmnt -no TARGET "${CUBE_IMG_DIR}" >/dev/null 2>&1; then
  log "already bind-mounted:"
  findmnt -no SOURCE,TARGET "${CUBE_IMG_DIR}" | sed 's/^/    /'
  exit 0
fi

sudo mkdir -p "${DEST}"

# The installer may already have put content here. Move it rather than
# mounting over it, which would hide it and waste the space it occupies on
# the root volume.
if [[ -d "${CUBE_IMG_DIR}" ]]; then
  if [[ -n "$(sudo ls -A "${CUBE_IMG_DIR}" 2>/dev/null)" ]]; then
    sz="$(sudo du -sh "${CUBE_IMG_DIR}" 2>/dev/null | cut -f1)"
    log "moving existing content (${sz}) from ${CUBE_IMG_DIR} to ${DEST}"
    # -T so the contents land IN ${DEST}, not in ${DEST}/cubebox_os_image.
    sudo cp -a -T "${CUBE_IMG_DIR}/." "${DEST}/" 2>/dev/null \
      || sudo cp -a "${CUBE_IMG_DIR}/." "${DEST}/"
    sudo rm -rf "${CUBE_IMG_DIR:?}"/*
  fi
else
  sudo mkdir -p "${CUBE_IMG_DIR}"
fi

if ! grep -q " ${CUBE_IMG_DIR} " /etc/fstab; then
  echo "${DEST} ${CUBE_IMG_DIR} none bind 0 0" | sudo tee -a /etc/fstab >/dev/null
  log "added the bind mount to /etc/fstab"
fi
sudo mount "${CUBE_IMG_DIR}"

findmnt -no TARGET "${CUBE_IMG_DIR}" >/dev/null 2>&1 \
  || die "the bind mount did not take effect"

log "layout:"
findmnt -no SOURCE,TARGET "${CUBE_IMG_DIR}" | sed 's/^/    /'
df -h / "${MOUNT}" | sed 's/^/    /'
log "template rootfs images will now land on ${MOUNT}, not the root volume"
