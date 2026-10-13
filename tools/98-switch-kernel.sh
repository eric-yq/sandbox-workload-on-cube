#!/usr/bin/env bash
# 98 - switch the instance's kernel page size (Graviton / arm64 only).
#
#   bash tools/98-switch-kernel.sh            # report only, change nothing
#   bash tools/98-switch-kernel.sh 64k        # switch to 64 KiB pages
#   bash tools/98-switch-kernel.sh 4k         # switch to 4 KiB pages
#   ALLOW_KERNEL_VERSION_CHANGE=1 bash tools/98-switch-kernel.sh 64k
#
# With no argument it prints the running kernel, page size and installed
# kernels, and exits without touching anything: switching reboots the host,
# so that must be asked for explicitly.
#
# With 4k or 64k it installs the kernel if missing, pins GRUB to it, reboots,
# and verifies the page size actually changed. Idempotent: if the instance is
# already on the requested page size nothing happens and there is no reboot.
#
# Why this exists. On arm64 every TLB invalidation is broadcast to all PEs in
# the inner-shareable domain and completed with dsb(ish), so its cost scales
# with core count. Cube gives every sandbox a MAP_PRIVATE mapping of one
# shared template memory snapshot, so each first guest write takes a host
# copy-on-write, and each COW pays two broadcast TLBI sequences. Larger host
# pages mean far fewer of them. Measured on c9g.metal-48xl, B1:
#
#     c=64    1963 -> 2480 exec/s   (+26%, and overtakes c8i.metal-48xl)
#     c=128   1792 -> 2372 exec/s   (+32%)
#     c=256   1556 -> 2115 exec/s   (+36%)
#     create p50 at c=64: 136 ms -> 77 ms
#
# The cost is memory: 64 KiB pages raised peak host memory from ~30 GB to
# ~75-79 GB for the same sandbox counts, so check headroom before using it on
# a smaller instance.
#
# This is numbered 98 because it is a maintenance action rather than a step in
# the provisioning flow: run it any time after 01. It reboots, and Cubelet
# keeps part of its state on tmpfs, so templates can be lost across a boot -
# the script checks them afterwards and tells you to re-run 04 if so.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# No argument means "report, change nothing". Switching reboots the
# instance, which is too destructive to be the default for a bare
# invocation - someone running this to see where things stand should not
# get a reboot.
TARGET=""
for arg in "$@"; do
  case "${arg}" in
    4k|64k)    TARGET="${arg}" ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) die "unknown argument: ${arg}
     usage: bash tools/98-switch-kernel.sh [4k|64k]
     (no argument reports the current state without changing anything)" ;;
  esac
done

load_config
ensure_ssh_key

INSTANCE_ID="$(state_require INSTANCE_ID)"
INSTANCE_TYPE="$(state_get INSTANCE_TYPE || echo '?')"
PRIVATE_IP="$(state_require PRIVATE_IP)"

log "instance ${INSTANCE_ID} (${INSTANCE_TYPE}) at ${PRIVATE_IP}"

# ------------------------------------------------------------ status only
if [[ -z "${TARGET}" ]]; then
  step "current kernel state (nothing will be changed)"

  # Ask EC2 first. wait_for_ssh dies with a security-group/subnet message,
  # which is the wrong diagnosis for an instance that is simply stopped -
  # and "where do things stand" should be answered in seconds.
  ec2_state="$(aws_ ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null \
    || echo "unknown")"
  log "EC2 state   : ${ec2_state}"
  if [[ "${ec2_state}" != "running" ]]; then
    die "the instance is ${ec2_state}, so its kernel state cannot be read.
     Start it with:
       aws --region ${AWS_REGION} ec2 start-instances --instance-ids ${INSTANCE_ID}"
  fi

  # Read-only query: fail fast instead of waiting the 15 minutes the switch
  # path allows for a bare-metal reboot.
  wait_for_ssh 60
  ssh_run 'echo "    arch        : $(uname -m)"
           echo "    running     : $(uname -r)"
           echo "    page size   : $(getconf PAGESIZE)"
           echo "    installed   :"
           ls -1 /boot/vmlinuz-* 2>/dev/null | sed "s|/boot/vmlinuz-|      |" || true
           echo "    GRUB_DEFAULT: $(grep -h "^GRUB_DEFAULT=" /etc/default/grub 2>/dev/null || echo "(unset)")"' \
    || die "could not read the kernel state from the instance"
  echo >&2
  log "to change it (this reboots the instance):"
  log "    bash tools/98-switch-kernel.sh 64k     # 64 KiB pages"
  log "    bash tools/98-switch-kernel.sh 4k      # 4 KiB pages"
  exit 0
fi

log "target page size: ${TARGET}"

step "waiting for the instance"
wait_for_ssh 900
# The remote script may need apt, which is locked during Ubuntu's first boot.
wait_for_cloud_init

# Gate on what the instance actually reports rather than on the instance-type
# string: that is authoritative, and it still catches the case where .state is
# stale or hand-edited.
REMOTE_ARCH="$(ssh_run 'uname -m' | tr -d '\r\n')"
if [[ "${REMOTE_ARCH}" != "aarch64" ]]; then
  die "instance ${INSTANCE_TYPE} reports ${REMOTE_ARCH}, not aarch64.
     Page size is selectable only on Arm: the translation granule
     (TCR_EL1.TG0/TG1) supports 4K/16K/64K, whereas x86-64 page tables
     define 4 KiB base pages only. Ubuntu ships no linux-image-*-64k for
     amd64 - 'apt-cache search linux-image | grep 64k' is empty there."
fi

step "preparing the ${TARGET} kernel"
out_file="$(mktemp)"
trap 'rm -f "${out_file}"' EXIT

# The remote script prepares and pins but never reboots: a self-reboot would
# destroy the SSH channel carrying its exit status, leaving us unable to tell
# a successful switch from a kernel that failed to boot.
SSH_SCRIPT_ENV="ALLOW_KERNEL_VERSION_CHANGE=${ALLOW_KERNEL_VERSION_CHANGE:-0}" \
ssh_script "${TOOLS_DIR}/remote/switch-kernel.sh" "${TARGET}" \
  | tee "${out_file}" \
  || die "kernel preparation failed; nothing was rebooted. Inspect with:
     ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} 'uname -r; getconf PAGESIZE'"

result="$(grep -oE 'SWITCH_KERNEL_RESULT=[a-z_]+' "${out_file}" | tail -1 | cut -d= -f2 || true)"
[[ -n "${result}" ]] || die "the remote script did not report a result; treating as failure"

if [[ "${result}" == "no_change" ]]; then
  log "already on a ${TARGET} kernel - no reboot needed"
  state_set KERNEL_PAGESIZE "${TARGET}"
  exit 0
fi

step "rebooting into the ${TARGET} kernel"
# A bare-metal reboot takes several minutes, far longer than a virtualised
# instance, so the wait below is generous on purpose.
ssh_run 'sudo -n systemctl reboot' >/dev/null 2>&1 || true
log "reboot issued; bare-metal instances take ~4-6 minutes to come back"
sleep 60
wait_for_ssh 900

step "verifying"
got_kernel="$(ssh_run 'uname -r' | tr -d '\r\n')"
got_pagesize="$(ssh_run 'getconf PAGESIZE' | tr -d '\r\n')"
log "kernel ${got_kernel}, page size ${got_pagesize}"

# Plain if/else, not `[[ ... ]] && assign`: lib.sh enables `set -e`, under
# which a false test as the last command of a list aborts the script - which
# would break the default 4k path.
if [[ "${TARGET}" == "64k" ]]; then
  want_pagesize=65536
  other="4k"
else
  want_pagesize=4096
  other="64k"
fi

if [[ "${got_pagesize}" != "${want_pagesize}" ]]; then
  die "expected page size ${want_pagesize} but the instance booted with ${got_pagesize}
     (kernel ${got_kernel}). The other kernel is still installed, so recover with:
       bash tools/98-switch-kernel.sh ${other}
     or inspect the pinned entry:
       ssh -i ${SSH_KEY} ${SSH_USER}@${PRIVATE_IP} 'grep GRUB_DEFAULT /etc/default/grub'"
fi

state_set KERNEL_PAGESIZE "${TARGET}"

# Cube's components live on /usr/local and the data volume, so they survive a
# reboot; report their health rather than assuming it, because a benchmark run
# against a half-started control plane is worse than a clear failure here.
step "Cube health after reboot"
if ssh_run 'curl -sf --max-time 5 http://127.0.0.1:3000/health' 2>/dev/null; then
  echo >&2
  ready="$(ssh_run "curl -sf --max-time 20 http://127.0.0.1:3000/templates \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for t in d if (t.get(\"status\") or t.get(\"state\"))==\"READY\"), len(d))'" 2>/dev/null || echo '? ?')"
  log "templates READY: ${ready% *}/${ready#* }"
  [[ "${ready% *}" == "${ready#* }" ]] || warn "not every template is READY.
     Re-run: bash tools/04-provision-templates.sh"
else
  warn "CubeAPI did not answer on 127.0.0.1:3000.
     If Cube is installed, give it a minute and re-check; otherwise run
     tools/03-setup-env.sh and tools/04-provision-templates.sh."
fi

if [[ "${TARGET}" == "64k" ]]; then
  echo >&2
  warn "64 KiB pages cost memory: peak host usage measured ~2.5x higher
     (~30 GB -> ~75-79 GB at c=64..256 on a 384 GiB host). Check headroom
     before raising concurrency."
fi

echo >&2
log "Next: bash tools/05-run-benchmark.sh"
