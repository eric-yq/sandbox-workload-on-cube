#!/usr/bin/env bash
# 99 - terminate the instance and confirm both volumes are gone.
#
#   bash tools/99-teardown.sh            # asks first
#   ASSUME_YES=1 bash tools/99-teardown.sh
#   bash tools/99-teardown.sh --stop     # stop instead of terminate (keeps the disks)
#
# Exists because this is the highest-consequence thing to forget: at
# m8g.metal-48xl on-demand rates an instance left running over a weekend
# costs more than the entire benchmark campaign, and a 1 TiB gp3 with
# 40,000 provisioned IOPS keeps billing after the instance is gone if
# DeleteOnTermination was ever turned off.
#
# --stop is the cheaper pause: it keeps the volumes (so ~$0.6/hour instead
# of ~$9/hour) and the private IP. Note that after a start you MUST re-run
# tools/04-provision-templates.sh - Cubelet's local image registry is on tmpfs, so templates do
# not survive the reboot even though they still report READY.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config

MODE="terminate"
[[ "${1:-}" == "--stop" ]] && MODE="stop"

INSTANCE_ID="$(state_get INSTANCE_ID || true)"
if [[ -z "${INSTANCE_ID}" ]]; then
  log "no instance recorded in ${STATE_FILE}"
  # Still worth looking: state can be lost while an instance keeps running.
  log "checking for tagged instances that state may have lost track of"
  aws_ ec2 describe-instances \
    --filters "Name=tag:Name,Values=${INSTANCE_NAME}" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,PrivateIpAddress,LaunchTime]' \
    --output table >&2 || true
  exit 0
fi

state="$(aws_ ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
  --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "gone")"
if [[ "${state}" == "gone" || "${state}" == "terminated" ]]; then
  log "${INSTANCE_ID} is already ${state}; clearing state"
  rm -f "${STATE_FILE}"
  exit 0
fi

DATA_VOLUME_ID="$(state_get DATA_VOLUME_ID || echo "")"
ROOT_VOLUME_ID="$(state_get ROOT_VOLUME_ID || echo "")"

echo >&2
log "instance    ${INSTANCE_ID} (${state}) $(state_get INSTANCE_TYPE || echo '')"
log "private IP  $(state_get PRIVATE_IP || echo '?')"
log "root vol    ${ROOT_VOLUME_ID:-?}"
log "data vol    ${DATA_VOLUME_ID:-?}"
echo >&2

if [[ "${MODE}" == "stop" ]]; then
  warn "STOP keeps both volumes (still billed) and the private IP.
     After starting again you must re-run tools/04-provision-templates.sh to rebuild templates."
  confirm "Stop ${INSTANCE_ID}?" || die "aborted"
  aws_ ec2 stop-instances --instance-ids "${INSTANCE_ID}" >/dev/null
  log "stopping; waiting"
  aws_ ec2 wait instance-stopped --instance-ids "${INSTANCE_ID}"
  log "stopped. Start again with:
     aws --region ${AWS_REGION} ec2 start-instances --instance-ids ${INSTANCE_ID}
     then re-read the private IP (it is retained across stop/start) and re-run tools/04-provision-templates.sh"
  exit 0
fi

# Check for unpulled results before destroying them.
if [[ -n "$(state_get PRIVATE_IP || true)" ]] && ensure_ssh_key 2>/dev/null; then
  if ssh_run "ls ${REMOTE_REPO}/results/*/summary_*.json >/dev/null 2>&1" 2>/dev/null; then
    local_count="$(ls -1d "${REPO_DIR}"/results/*/ 2>/dev/null | wc -l)"
    warn "the instance has results. Local ./results/ currently has ${local_count} directories."
    if confirm "Pull results down before terminating?"; then
      rsync_results_down
    fi
  fi
fi

warn "TERMINATE destroys the instance and both volumes, including every
     template, snapshot and any result not already pulled."
confirm "Terminate ${INSTANCE_ID}?" || die "aborted"

aws_ ec2 terminate-instances --instance-ids "${INSTANCE_ID}" >/dev/null
log "terminating; waiting (bare metal takes a few minutes)"
aws_ ec2 wait instance-terminated --instance-ids "${INSTANCE_ID}"
log "instance terminated"

# DeleteOnTermination should have handled both, but verify rather than
# assume: a leftover provisioned-IOPS volume is silent and expensive.
step "verifying the volumes are gone"
leaked=()
for vol in "${ROOT_VOLUME_ID}" "${DATA_VOLUME_ID}"; do
  [[ -z "${vol}" || "${vol}" == "None" ]] && continue
  vstate="$(aws_ ec2 describe-volumes --volume-ids "${vol}" \
    --query 'Volumes[0].State' --output text 2>/dev/null || echo "deleted")"
  if [[ "${vstate}" == "deleted" ]]; then
    log "ok: ${vol} deleted"
  else
    warn "${vol} still exists (state: ${vstate})"
    leaked+=("${vol}")
  fi
done

if (( ${#leaked[@]} )); then
  warn "${#leaked[@]} volume(s) survived termination and are still billing."
  if confirm "Delete them now?"; then
    for vol in "${leaked[@]}"; do
      aws_ ec2 delete-volume --volume-id "${vol}" && log "deleted ${vol}" \
        || warn "could not delete ${vol}; delete it in the console"
    done
  else
    warn "left behind: ${leaked[*]}
     Delete with: aws --region ${AWS_REGION} ec2 delete-volume --volume-id <id>"
  fi
fi

rm -f "${STATE_FILE}"
log "state cleared. Nothing left running."
