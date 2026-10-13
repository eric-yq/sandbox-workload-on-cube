#!/usr/bin/env bash
# 02 - filesystem and mounts on the instance.
#
#   bash tools/02-prepare-disks.sh
#   FORCE_MKFS=1 bash tools/02-prepare-disks.sh          # reformat a volume that already has data
#   DATA_BACKING=ebs bash tools/02-prepare-disks.sh      # ignore local SSDs, use the gp3 volume
#   DATA_BACKING=instance-store bash tools/02-prepare-disks.sh
#
# Two backings, chosen by DATA_BACKING (default `auto`):
#
#   instance-store  All local NVMe SSDs are striped into one LVM volume.
#                   Picked automatically when the instance has them - the
#                   'd' families (c8gd, m7id, i4i, ...). Much faster than
#                   the gp3 data volume and free with the instance.
#   ebs             The gp3 data volume created by 01.
#
# Idempotent: re-running against an already-prepared volume keeps the
# filesystem and its contents. The real work is in
# tools/remote/setup-disks.sh, which runs on the instance - read that one
# for the device-identification and safety logic.
#
# Instance storage is EPHEMERAL. It survives a reboot (so switching kernels
# with 98-switch-kernel.sh is safe) but a stop/start returns the disks blank,
# which also destroys the Cube install living under the mount. After any
# stop/start, re-run this script and then 03/04.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config
ensure_ssh_key

INSTANCE_ID="$(state_require INSTANCE_ID)"
PRIVATE_IP="$(state_require PRIVATE_IP)"

# Not state_require: with DATA_BACKING=instance-store there need not be a gp3
# data volume at all, and demanding one would block that path.
DATA_VOLUME_ID="$(state_get DATA_VOLUME_ID || echo '')"

# Inherit the decision 01 made, so a launch-time DATA_BACKING=ebs is not
# silently overridden here by `auto` picking the local disks. An explicit
# DATA_BACKING in the environment still wins.
BACKING="${DATA_BACKING:-$(state_get DATA_BACKING || echo auto)}"

log "instance ${INSTANCE_ID} at ${PRIVATE_IP}"
log "data backing: ${BACKING}"
if [[ -n "${DATA_VOLUME_ID}" ]]; then
  log "gp3 data volume ${DATA_VOLUME_ID} -> ${DATA_MOUNT} (used only in ebs mode)"
else
  log "no gp3 data volume recorded; instance storage is the only option"
fi

step "waiting for the instance to be reachable and settled"
wait_for_ssh 900
# Not strictly needed to run mkfs, but this script installs xfsprogs and
# lvm2 when they are missing, and apt is locked during Ubuntu's first boot.
wait_for_cloud_init

step "preparing ${DATA_MOUNT}"
out_file="$(mktemp)"
trap 'rm -f "${out_file}"' EXIT

# FORCE_MKFS and DATA_BACKING are forwarded rather than baked in: the remote
# script refuses to erase an existing filesystem without the former, and
# needs the latter to decide which disks to touch.
SSH_SCRIPT_ENV="FORCE_MKFS=${FORCE_MKFS:-0} DATA_BACKING=${BACKING}" \
ssh_script "${TOOLS_DIR}/remote/setup-disks.sh" \
  "${DATA_VOLUME_ID}" "${DATA_MOUNT}" \
  | tee "${out_file}" \
  || die "disk setup failed. Nothing was mounted; inspect with:
     ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} 'lsblk -o NAME,SIZE,FSTYPE,MODEL,SERIAL,MOUNTPOINTS'"

backing="$(grep -oE 'SETUP_DISKS_BACKING=[a-z-]+' "${out_file}" | tail -1 | cut -d= -f2 || true)"
[[ -n "${backing}" ]] || die "the remote script did not report which backing it used"

state_set DISKS_READY 1
state_set DATA_BACKING "${backing}"

if [[ "${backing}" == "instance-store" ]]; then
  echo >&2
  warn "${DATA_MOUNT} is on instance storage, which a stop/start WIPES.
     A reboot is fine. After a stop/start, re-run:
       bash tools/02-prepare-disks.sh && bash tools/03-setup-env.sh \\
         && bash tools/04-provision-templates.sh"
  if [[ -n "${DATA_VOLUME_ID}" ]]; then
    warn "the gp3 volume ${DATA_VOLUME_ID} (${DATA_VOLUME_GB} GB) is attached but
     unused, and still billed. To stop creating it, set DATA_VOLUME_GB small
     or drop the second block device from tools/01-create-instance.sh."
  fi
fi

echo >&2
log "Next: bash tools/03-setup-env.sh   (benchmark environment)"
