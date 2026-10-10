#!/usr/bin/env bash
# 02 - filesystem and mounts on the instance.
#
#   bash tools/02-prepare-disks.sh
#   FORCE_MKFS=1 bash tools/02-prepare-disks.sh    # reformat a volume that already has data
#
# Idempotent: re-running against an already-prepared volume keeps the
# filesystem and its contents. The real work is in
# tools/remote/setup-disks.sh, which runs on the instance - read that one
# for the device-identification and safety logic.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config
ensure_ssh_key

INSTANCE_ID="$(state_require INSTANCE_ID)"
PRIVATE_IP="$(state_require PRIVATE_IP)"
DATA_VOLUME_ID="$(state_require DATA_VOLUME_ID)"

log "instance ${INSTANCE_ID} at ${PRIVATE_IP}"
log "data volume ${DATA_VOLUME_ID} -> ${DATA_MOUNT}"

step "waiting for the instance to be reachable and settled"
wait_for_ssh 900
# Not strictly needed to run mkfs, but this script installs xfsprogs when
# it is missing, and apt is locked during Ubuntu's first boot.
wait_for_cloud_init

step "preparing ${DATA_MOUNT}"
# FORCE_MKFS is forwarded rather than baked in: the remote script refuses
# to erase an existing filesystem without it.
SSH_SCRIPT_ENV="FORCE_MKFS=${FORCE_MKFS:-0}" \
ssh_script "${TOOLS_DIR}/remote/setup-disks.sh" \
  "${DATA_VOLUME_ID}" "${DATA_MOUNT}" \
  || die "disk setup failed. Nothing was mounted; inspect with:
     ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} 'lsblk -o NAME,SIZE,FSTYPE,SERIAL,MOUNTPOINTS'"

state_set DISKS_READY 1
echo >&2
log "Next: bash tools/03-setup-env.sh   (benchmark environment)"
