#!/usr/bin/env bash
# 01 - create the metal instance with both EBS volumes attached at launch,
#      then record its private IP.
#
#   bash tools/01-create-instance.sh                    # INSTANCE_TYPE from config
#   bash tools/01-create-instance.sh m8g.metal-48xl     # override it
#
# The optional argument exists so one config.env can drive a sweep across the
# four target machine types without being edited between runs.
#
# Volumes are attached in the block device mapping rather than created and
# attached afterwards, so there is no window where the instance exists
# without its data volume and no second API dance to get wrong.
#
# Both volumes get DeleteOnTermination=true. That is deliberate: the whole
# point of tools/ is a disposable, reproducible box, and a 1 TiB gp3 with
# provisioned IOPS left behind by a terminated instance is an expensive
# thing to forget. Nothing on the data volume is worth preserving anyway -
# see the note about Cubelet's tmpfs state in README.md.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

case "${1:-}" in
  -h|--help)
    sed -n '2,12p' "$0"; exit 0 ;;
  -*) die "unknown option: $1
     usage: bash tools/01-create-instance.sh [instance-type]" ;;
esac

load_config
ensure_ssh_key
need_cmd aws

step "instance type"
resolve_instance_type "${1:-}"
# Validated here as well as in the preflight: a type passed on the command
# line has not been through 00-preflight, so this is the only place it gets
# checked before an hour of bare metal starts billing.
validate_instance_type "${INSTANCE_TYPE}" \
  || die "${INSTANCE_TYPE} cannot be used (see above).
     Valid targets for this benchmark: m7i.metal-48xl, m8i.metal-48xl,
     m8g.metal-48xl, m9g.metal-48xl - or any other bare-metal type."

# Everything after run-instances is recording and waiting, so it is kept in
# a function that can also run against an instance that already exists.
# Without that, any failure between "the instance was created" and "its
# details are in state" left a bare-metal instance billing while a re-run
# offered only to launch a second one.
finalize_instance() {
  local iid="$1"

  step "waiting for the instance to run"
  # Bare metal takes materially longer to boot than a virtualised instance.
  aws_ ec2 wait instance-running --instance-ids "${iid}" || \
    die "instance did not reach running. Check the console for ${iid}."

  local root_dev
  root_dev="$(aws_ ec2 describe-instances --instance-ids "${iid}" \
    --query 'Reservations[0].Instances[0].RootDeviceName' --output text)"

  PRIVATE_IP="$(aws_ ec2 describe-instances --instance-ids "${iid}" \
    --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)"
  state_set PRIVATE_IP "${PRIVATE_IP}"
  log "private IP ${PRIVATE_IP}"

  # Report what actually happened rather than leaving it implicit. With
  # ASSOCIATE_PUBLIC_IP=auto this is inherited from the subnet, and default
  # VPC subnets assign one - so the common path DOES get a public address.
  PUBLIC_IP="$(aws_ ec2 describe-instances --instance-ids "${iid}" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null || echo "None")"
  if [[ -n "${PUBLIC_IP}" && "${PUBLIC_IP}" != "None" ]]; then
    state_set PUBLIC_IP "${PUBLIC_IP}"
    log "public IP  ${PUBLIC_IP}  (ASSOCIATE_PUBLIC_IP=${ASSOCIATE_PUBLIC_IP})"
    warn "this instance is reachable from the internet at the network level.
     These scripts always connect to the PRIVATE address, and the security
     group admits only its own members - but the box runs an
     UNAUTHENTICATED CubeAPI on :3000 and a web console on :12088, so do not
     widen the security group while it has a public IP.
     Set ASSOCIATE_PUBLIC_IP=no in tools/config.env to drop it, but only if
     the subnet has another egress path (NAT or VPC endpoints) -
     03-setup-env.sh downloads Docker, the CubeSandbox bundle and datasets."
  else
    state_set PUBLIC_IP ""
    log "no public IP (ASSOCIATE_PUBLIC_IP=${ASSOCIATE_PUBLIC_IP})"
    warn "with no public IP the instance needs a NAT gateway or VPC endpoints
     for egress. 03-setup-env.sh will hang on the first download otherwise."
  fi

  # Record the volume IDs. The data volume ID in particular is what
  # 02-prepare-disks.sh matches against the NVMe serial to find the device,
  # which is the only safe way: NVMe enumeration order is not the block
  # device mapping order, so a name-based guess can point at the root volume.
  ROOT_VOLUME_ID="$(aws_ ec2 describe-instances --instance-ids "${iid}" \
    --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='${root_dev}'].Ebs.VolumeId | [0]" \
    --output text)"
  state_set ROOT_VOLUME_ID "${ROOT_VOLUME_ID}"
  log "root volume ${ROOT_VOLUME_ID}"

  # Whether there is a gp3 data volume is DETECTED from the instance, not
  # taken from this run's intent. That matters because the branch above
  # adopts an already-running instance and calls this function directly, so
  # any launch-time decision variable would be unset there. It also means
  # .state always describes the machine as it actually is.
  DATA_VOLUME_ID="$(aws_ ec2 describe-instances --instance-ids "${iid}" \
    --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='${DATA_DEVICE_NAME}'].Ebs.VolumeId | [0]" \
    --output text 2>/dev/null || echo "None")"

  if [[ "${DATA_VOLUME_ID}" == vol-* ]]; then
    state_set DATA_VOLUME_ID "${DATA_VOLUME_ID}"
    state_set DATA_DEVICE_NAME "${DATA_DEVICE_NAME}"
    state_set DATA_BACKING "ebs"
    log "data volume ${DATA_VOLUME_ID} (backing: ebs)"
  else
    # No second EBS volume. On a 'd' family type that is intended - the local
    # NVMe disks are the data store. On anything else it means the launch did
    # not attach what was asked for, which is worth saying loudly.
    local it_store
    it_store="$(aws_ ec2 describe-instance-types \
      --instance-types "$(state_get INSTANCE_TYPE || echo "${INSTANCE_TYPE}")" \
      --query 'InstanceTypes[0].InstanceStorageInfo.TotalSizeInGB' \
      --output text 2>/dev/null || echo "None")"
    state_set DATA_VOLUME_ID ""
    state_set DATA_BACKING "instance-store"
    if [[ -n "${it_store}" && "${it_store}" != "None" && "${it_store}" != "0" ]]; then
      log "no gp3 data volume; ${it_store} GB of local NVMe will be used instead"
      log "  02-prepare-disks.sh stripes the local disks into one LVM volume"
    else
      warn "no volume at ${DATA_DEVICE_NAME} on ${iid}, and this type has no
     local NVMe either. 02-prepare-disks.sh will have nothing to prepare.
     Attached devices:
$(aws_ ec2 describe-instances --instance-ids "${iid}" \
      --query 'Reservations[0].Instances[0].BlockDeviceMappings[].[DeviceName,Ebs.VolumeId]' \
      --output text 2>/dev/null | sed 's/^/       /')"
    fi
  fi

  step "waiting for SSH"
  wait_for_ssh 900

  echo >&2
  log "instance is up. State:"
  state_show
  echo >&2
  log "Next: bash tools/02-prepare-disks.sh   (filesystem + mounts)"
}

# An instance already recorded in state is adopted rather than duplicated.
# This is the normal path after a partial failure: the instance exists and
# is billing, so finishing it is almost always what you want.
if existing="$(state_get INSTANCE_ID 2>/dev/null)" && [[ -n "${existing}" ]]; then
  st="$(aws_ ec2 describe-instances --instance-ids "${existing}" \
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "gone")"
  case "${st}" in
    running|pending)
      log "${existing} already exists and is ${st}"
      # A requested type that differs from the running one is almost always a
      # mistake worth stopping for: adopting would hand back a machine of the
      # wrong type, and every result would be labelled with what IMDS reports
      # rather than what was asked for.
      existing_type="$(aws_ ec2 describe-instances --instance-ids "${existing}" \
        --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null || echo "?")"
      if [[ "${existing_type}" != "${INSTANCE_TYPE}" ]]; then
        warn "type mismatch: ${existing} is a ${existing_type}, but ${INSTANCE_TYPE}
     was requested (from ${INSTANCE_TYPE_SOURCE}).
     Adopting it would benchmark ${existing_type}, not ${INSTANCE_TYPE}."
        confirm "Adopt the existing ${existing_type} anyway?" || die "aborted.
     To benchmark ${INSTANCE_TYPE} instead, first release the current one:
       bash tools/99-teardown.sh"
        INSTANCE_TYPE="${existing_type}"
      fi
      log "resuming: recording its details instead of launching another"
      state_set INSTANCE_TYPE "${INSTANCE_TYPE}"
      finalize_instance "${existing}"
      exit 0
      ;;
    stopped|stopping)
      warn "${existing} is ${st}. Start it with:
       aws --region ${AWS_REGION} ec2 start-instances --instance-ids ${existing}
     then re-run this script to pick it up."
      die "aborted"
      ;;
    gone|terminated|shutting-down)
      log "${existing} is ${st}; launching a replacement"
      ;;
  esac
fi

step "resolving the AMI"
arch="$(aws_ ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" \
         --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' --output text)"
case "${arch}" in
  arm64)  ami_arch=arm64 ;;
  x86_64) ami_arch=amd64 ;;
  *) die "unsupported architecture for ${INSTANCE_TYPE}: ${arch}" ;;
esac
ssm_path="/aws/service/canonical/ubuntu/server/${UBUNTU_RELEASE}/stable/current/${ami_arch}/hvm/ebs-gp3/ami-id"
AMI_ID="$(aws_ ssm get-parameter --name "${ssm_path}" --query 'Parameter.Value' --output text 2>/dev/null || true)"
if [[ -z "${AMI_ID}" || "${AMI_ID}" == "None" ]]; then
  # Canonical's SSM aliases move around between releases; fall back to the
  # image list rather than hardcoding an AMI that rots.
  warn "SSM lookup failed; falling back to describe-images"
  AMI_ID="$(aws_ ec2 describe-images --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd*/ubuntu-*-${UBUNTU_RELEASE}-${ami_arch}-server-*" \
              "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
fi
[[ -n "${AMI_ID}" && "${AMI_ID}" != "None" ]] || die "could not resolve an Ubuntu ${UBUNTU_RELEASE} ${ami_arch} AMI"
log "AMI ${AMI_ID} (Ubuntu ${UBUNTU_RELEASE}, ${ami_arch})"

# The AMI decides the root device name; hardcoding /dev/sda1 breaks on
# images that use /dev/xvda.
ROOT_DEV="$(aws_ ec2 describe-images --image-ids "${AMI_ID}" \
             --query 'Images[0].RootDeviceName' --output text)"
log "root device name: ${ROOT_DEV}"

step "choosing placement"
# Candidate AZs: the ones where this type is actually offered. Bare metal
# 48xl capacity comes and goes, so 01-create-instance.sh walks them instead of dying on the
# first InsufficientInstanceCapacity.
if [[ -n "${AVAILABILITY_ZONE}" ]]; then
  cand_azs="${AVAILABILITY_ZONE}"
else
  cand_azs="$(aws_ ec2 describe-instance-type-offerings \
    --location-type availability-zone \
    --filters "Name=instance-type,Values=${INSTANCE_TYPE}" \
    --query 'InstanceTypeOfferings[].Location' --output text)"
  # Put this workstation's AZ first when the instance type is offered there:
  # same-AZ keeps SSH and the result transfer off the cross-AZ path, and
  # avoids cross-AZ data charges during the sync. The remaining AZs stay in
  # the list because bare metal 48xl capacity is not guaranteed anywhere,
  # and a different subnet in the SAME VPC is still reachable - the VPC's
  # local route plus the shared security group both still apply.
  if [[ -n "${SELF_AZ:-}" ]] && [[ " ${cand_azs} " == *" ${SELF_AZ} "* ]]; then
    cand_azs="${SELF_AZ} $(echo "${cand_azs}" | tr ' ' '\n' | grep -vx "${SELF_AZ}" | tr '\n' ' ')"
    log "preferring this workstation's AZ (${SELF_AZ}) first"
  elif [[ -n "${SELF_AZ:-}" ]]; then
    warn "${INSTANCE_TYPE} is not offered in this workstation's AZ (${SELF_AZ});
     the instance will land in another AZ of the same VPC - still reachable,
     but SSH and the result transfer cross an AZ boundary."
  fi
fi
[[ -n "${cand_azs}" ]] || die "${INSTANCE_TYPE} is not offered in ${AWS_REGION}"
log "candidate AZs (in order): ${cand_azs}"

step "placement"
# Default to "wherever this workstation is": same VPC, same subnet, same
# security groups. Reachability is then a property of the construction
# rather than a hope - and nothing has to be created, which matters because
# any network this script invented could easily be somewhere the workstation
# cannot reach.
#
# Precedence, most specific first:
#   VPC     SUBNET_ID's VPC > VPC_ID > this workstation's VPC > default VPC
#   subnet  SUBNET_ID > AVAILABILITY_ZONE > this workstation's subnet,
#           then any other subnet in the same VPC (capacity fallback)
#   groups  SECURITY_GROUP_ID > this workstation's groups > the VPC's default
if discover_self_network; then
  log "this workstation: ${SELF_INSTANCE_ID} in ${SELF_AZ}"
  log "  vpc=${SELF_VPC_ID} subnet=${SELF_SUBNET_ID}"
  log "  groups=${SELF_SG_IDS} (${SELF_SG_NAMES})"
else
  SELF_VPC_ID=""; SELF_SUBNET_ID=""; SELF_SG_IDS=""; SELF_AZ=""; SELF_PRIVATE_IP=""
  warn "this machine is not an EC2 instance (no IMDS), so its network cannot
     be reused. Falling back to the default VPC and its default security
     group - set SUBNET_ID / SECURITY_GROUP_ID in tools/config.env if the
     instance then turns out to be unreachable."
fi

if [[ -n "${SUBNET_ID}" ]]; then
  VPC_ID="$(aws_ ec2 describe-subnets --subnet-ids "${SUBNET_ID}" \
             --query 'Subnets[0].VpcId' --output text)"
  log "VPC ${VPC_ID} (from SUBNET_ID)"
elif [[ -n "${VPC_ID:-}" ]]; then
  aws_ ec2 describe-vpcs --vpc-ids "${VPC_ID}" >/dev/null 2>&1 \
    || die "VPC_ID=${VPC_ID} does not exist in ${AWS_REGION}"
  log "VPC ${VPC_ID} (from config)"
elif [[ -n "${SELF_VPC_ID}" ]]; then
  VPC_ID="${SELF_VPC_ID}"
  log "VPC ${VPC_ID} (this workstation's)"
else
  VPC_ID="$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
             --query 'Vpcs[0].VpcId' --output text)"
  [[ -n "${VPC_ID}" && "${VPC_ID}" != "None" ]] \
    || die "no default VPC in ${AWS_REGION}.
     Set VPC_ID or SUBNET_ID in tools/config.env."
  log "VPC ${VPC_ID} (account default)"
fi
state_set VPC_ID "${VPC_ID}"

step "security groups"
if [[ -n "${SECURITY_GROUP_ID}" ]]; then
  SG_IDS="${SECURITY_GROUP_ID}"
  log "groups ${SG_IDS} (from config)"
elif [[ -n "${SELF_SG_IDS}" && "${SELF_VPC_ID}" == "${VPC_ID}" ]]; then
  # Same groups as this workstation. A self-referencing inbound rule - which
  # is the VPC default group's only rule - then permits us in, because both
  # ends are members.
  SG_IDS="${SELF_SG_IDS}"
  log "groups ${SG_IDS} (same as this workstation)"
else
  [[ -n "${SELF_SG_IDS}" ]] && warn "this workstation is in ${SELF_VPC_ID}, but the
     instance is going into ${VPC_ID}; its groups cannot be reused."
  SG_IDS="$(aws_ ec2 describe-security-groups \
            --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=default" \
            --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")"
  [[ -n "${SG_IDS}" && "${SG_IDS}" != "None" ]] \
    || die "no security group named 'default' in ${VPC_ID}.
     Set SECURITY_GROUP_ID in tools/config.env."
  log "groups ${SG_IDS} (the VPC's default group)"
fi
state_set SECURITY_GROUPS_USED "${SG_IDS}"

# Confirm the choice actually permits SSH from here, instead of discovering
# it as a timeout after the instance is already billing.
if [[ -n "${SELF_PRIVATE_IP}" ]]; then
  if verify_ssh_reachable "${SG_IDS}" "${SELF_PRIVATE_IP}" "${SELF_SG_IDS}"; then
    log "SSH should work: ${SSH_REACH_WHY}"
  else
    warn "no security group rule appears to permit SSH from this host:
       ${SSH_REACH_WHY}
     The instance will launch, but tools/02-prepare-disks.sh will not be able
     to reach it. Either add an inbound rule for port 22, or set
     SECURITY_GROUP_ID in tools/config.env to a group that has one."
    confirm "Launch anyway?" || die "aborted before launching"
  fi
else
  warn "cannot verify SSH reachability without knowing this host's address"
fi

log "inbound rules in effect:"
# shellcheck disable=SC2086
aws_ ec2 describe-security-groups --group-ids ${SG_IDS} \
  --query 'SecurityGroups[].IpPermissions[].[IpProtocol,FromPort,ToPort,
           join(`,`,IpRanges[].CidrIp), join(`,`,UserIdGroupPairs[].GroupId)]' \
  --output text 2>/dev/null | sed 's/^/      /' >&2 || true

# --- security group ids -> the JSON/CLI forms used at launch --------------
sg_json="$(printf '%s' "${SG_IDS}" | tr ' ' '\n' | grep -v '^$' \
           | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin]))')"

# --- data volume: skip the gp3 when the instance has local NVMe SSDs -----
# Asked of the API rather than inferred from a 'd' in the type name: the API
# is authoritative, and the naming is not a contract worth parsing. A 'd'
# instance striping its local SSDs (see tools/02-prepare-disks.sh) gets far
# more throughput than the gp3 for free, so creating the volume as well just
# bills a 1000 GB provisioned-IOPS disk that nothing ever mounts.
inst_store_gb="$(aws_ ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" \
  --query 'InstanceTypes[0].InstanceStorageInfo.TotalSizeInGB' \
  --output text 2>/dev/null || echo "None")"

HAS_INSTANCE_STORE=0
if [[ -n "${inst_store_gb}" && "${inst_store_gb}" != "None" && "${inst_store_gb}" != "0" ]]; then
  HAS_INSTANCE_STORE=1
fi

# Same vocabulary as 02-prepare-disks.sh, so one setting drives both.
CREATE_DATA_VOLUME=1
case "${DATA_BACKING:-auto}" in
  auto)
    if (( HAS_INSTANCE_STORE )); then CREATE_DATA_VOLUME=0; fi
    ;;
  instance-store)
    (( HAS_INSTANCE_STORE )) || die "DATA_BACKING=instance-store but ${INSTANCE_TYPE}
     has no local NVMe storage (InstanceStorageInfo.TotalSizeInGB=${inst_store_gb}).
     Use a 'd' family type (c8gd, m7id, i4i, ...) or DATA_BACKING=ebs."
    CREATE_DATA_VOLUME=0
    ;;
  ebs) CREATE_DATA_VOLUME=1 ;;
  *) die "DATA_BACKING must be auto, instance-store or ebs (got '${DATA_BACKING}')" ;;
esac

if (( CREATE_DATA_VOLUME )); then
  if (( HAS_INSTANCE_STORE )); then
    log "${INSTANCE_TYPE} has ${inst_store_gb} GB of local NVMe, but DATA_BACKING=ebs"
    log "  -> creating the ${DATA_VOLUME_GB} GB gp3 anyway; the local disks stay unused"
  else
    log "no local NVMe on ${INSTANCE_TYPE}; creating a ${DATA_VOLUME_GB} GB gp3 data volume"
  fi
else
  log "${INSTANCE_TYPE} has ${inst_store_gb} GB of local NVMe"
  log "  -> NOT creating a gp3 data volume; 02-prepare-disks.sh will stripe the local disks"
fi

step "launching ${INSTANCE_TYPE}"
if (( CREATE_DATA_VOLUME )); then
  bdm="$(cat <<JSON
[
  {"DeviceName":"${ROOT_DEV}",
   "Ebs":{"VolumeSize":${ROOT_VOLUME_GB},"VolumeType":"gp3",
          "DeleteOnTermination":true,"Encrypted":true}},
  {"DeviceName":"${DATA_DEVICE_NAME}",
   "Ebs":{"VolumeSize":${DATA_VOLUME_GB},"VolumeType":"gp3",
          "Iops":${DATA_VOLUME_IOPS},"Throughput":${DATA_VOLUME_THROUGHPUT},
          "DeleteOnTermination":true,"Encrypted":true}}
]
JSON
)"
else
  bdm="$(cat <<JSON
[
  {"DeviceName":"${ROOT_DEV}",
   "Ebs":{"VolumeSize":${ROOT_VOLUME_GB},"VolumeType":"gp3",
          "DeleteOnTermination":true,"Encrypted":true}}
]
JSON
)"
fi

tag_spec="ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}}"
tag_spec_v="ResourceType=volume,Tags=[{Key=Name,Value=${INSTANCE_NAME}}"
if [[ -n "${EXTRA_TAGS:-}" ]]; then
  for kv in ${EXTRA_TAGS}; do
    tag_spec+=",{Key=${kv%%=*},Value=${kv#*=}}"
    tag_spec_v+=",{Key=${kv%%=*},Value=${kv#*=}}"
  done
fi
tag_spec+="]"; tag_spec_v+="]"

profile_arg=()
[[ -n "${IAM_INSTANCE_PROFILE}" ]] && profile_arg=(--iam-instance-profile "Name=${IAM_INSTANCE_PROFILE}")

INSTANCE_ID=""
for az in ${cand_azs}; do
  if [[ -n "${SUBNET_ID}" ]]; then
    subnet="${SUBNET_ID}"
  elif [[ -n "${SELF_SUBNET_ID:-}" && "${az}" == "${SELF_AZ:-}" ]]; then
    # In this workstation's own AZ, use its exact subnet rather than
    # whichever one describe-subnets happens to return first.
    subnet="${SELF_SUBNET_ID}"
  else
    subnet="$(aws_ ec2 describe-subnets \
      --filters "Name=vpc-id,Values=${VPC_ID}" "Name=availability-zone,Values=${az}" \
      --query 'Subnets[0].SubnetId' --output text 2>/dev/null || echo "None")"
    [[ "${subnet}" == "None" || -z "${subnet}" ]] && { warn "no subnet in ${az}; skipping"; continue; }
  fi
  log "trying ${az} (subnet ${subnet})"

  # Placement is expressed one of two ways, and they are mutually
  # exclusive in the API: either top-level --subnet-id/--security-group-ids
  # (which inherits the subnet's MapPublicIpOnLaunch), or a full
  # --network-interfaces spec, which is the only place
  # AssociatePublicIpAddress can be set explicitly.
  net_args=()
  if [[ "${ASSOCIATE_PUBLIC_IP}" == "auto" ]]; then
    # shellcheck disable=SC2086
    net_args=(--subnet-id "${subnet}" --security-group-ids ${SG_IDS})
  else
    want_pub=false
    [[ "${ASSOCIATE_PUBLIC_IP}" == "yes" ]] && want_pub=true
    net_args=(--network-interfaces "$(cat <<JSON
[{"DeviceIndex":0,"SubnetId":"${subnet}","Groups":${sg_json},
  "AssociatePublicIpAddress":${want_pub},"DeleteOnTermination":true}]
JSON
)")
  fi

  set +e
  out="$(aws_ ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" \
    "${net_args[@]}" \
    --block-device-mappings "${bdm}" \
    --tag-specifications "${tag_spec}" "${tag_spec_v}" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    "${profile_arg[@]}" \
    --query 'Instances[0].InstanceId' --output text 2>&1)"
  rc=$?
  set -e
  if (( rc == 0 )) && [[ "${out}" == i-* ]]; then
    INSTANCE_ID="${out}"
    state_set AVAILABILITY_ZONE "${az}"
    state_set SUBNET_ID "${subnet}"
    break
  fi
  if echo "${out}" | grep -qiE 'InsufficientInstanceCapacity|Unsupported'; then
    warn "${az}: no capacity for ${INSTANCE_TYPE}, trying the next AZ"
    continue
  fi
  die "run-instances failed in ${az}:
${out}"
done
[[ -n "${INSTANCE_ID}" ]] || die "no AZ had capacity for ${INSTANCE_TYPE} in ${AWS_REGION}.
     Try a different region, or request an On-Demand Capacity Reservation."

log "instance ${INSTANCE_ID}"
state_set INSTANCE_ID "${INSTANCE_ID}"
state_set INSTANCE_TYPE "${INSTANCE_TYPE}"
state_set AWS_REGION "${AWS_REGION}"
state_set AMI_ID "${AMI_ID}"

finalize_instance "${INSTANCE_ID}"
