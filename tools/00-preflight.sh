#!/usr/bin/env bash
# 00 - preflight. Checks everything that would otherwise fail LATER and
# more confusingly, and changes nothing.
#
#   bash tools/00-preflight.sh
#
# Every check here corresponds to a failure that actually cost time on the
# c7g.metal trial run, or to a launch parameter that cannot be fixed after
# the fact without recreating the instance.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

case "${1:-}" in
  -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
  -*) die "unknown option: $1
     usage: bash tools/00-preflight.sh [instance-type]" ;;
esac

load_config
# Same optional override as 01, so the preflight checks the type you are
# actually about to launch rather than whatever config.env happens to say.
resolve_instance_type "${1:-}"

fail=0
check() { if "$@"; then log "ok: ${CHECK_NAME}"; else warn "FAIL: ${CHECK_NAME}"; fail=1; fi; }

step "local tooling"
for c in aws ssh scp rsync; do
  CHECK_NAME="${c} present"
  check have_cmd "${c}"
done
if have_cmd aws; then
  log "aws-cli: $(aws --version 2>&1)"
fi

step "AWS credentials and region"
if arn="$(aws_ sts get-caller-identity --query Arn --output text 2>/dev/null)"; then
  log "ok: identity ${arn}"
else
  warn "FAIL: aws sts get-caller-identity failed for region ${AWS_REGION}"
  fail=1
fi

step "SSH key"
if [[ -f "${SSH_KEY_PATH}" ]]; then
  ensure_ssh_key
  log "ok: using ${SSH_KEY}"
else
  warn "FAIL: ${SSH_KEY_PATH} does not exist"
  fail=1
fi
if aws_ ec2 describe-key-pairs --key-names "${KEY_NAME}" >/dev/null 2>&1; then
  log "ok: EC2 key pair '${KEY_NAME}' exists in ${AWS_REGION}"
else
  warn "FAIL: no EC2 key pair named '${KEY_NAME}' in ${AWS_REGION}"
  fail=1
fi

step "placement (resolved exactly as 01-create-instance.sh will)"
# Mirror 01's precedence so the preflight reports the placement that will
# actually be used, not a guess.
if discover_self_network; then
  log "this workstation: ${SELF_INSTANCE_ID} (${SELF_AZ}) ${SELF_PRIVATE_IP}"
  log "  vpc=${SELF_VPC_ID} subnet=${SELF_SUBNET_ID} groups=${SELF_SG_IDS} (${SELF_SG_NAMES})"
else
  SELF_VPC_ID=""; SELF_SUBNET_ID=""; SELF_SG_IDS=""; SELF_AZ=""; SELF_PRIVATE_IP=""
  warn "this machine is not an EC2 instance (no IMDS reachable), so its VPC,
     subnet and security groups cannot be inherited. Falling back to the
     default VPC and its default group - which very likely does NOT permit
     SSH from outside AWS. Set SUBNET_ID and SECURITY_GROUP_ID explicitly."
fi

if [[ -n "${SUBNET_ID}" ]]; then
  probe_subnets="${SUBNET_ID}"
  vpc_for_probe="$(aws_ ec2 describe-subnets --subnet-ids "${SUBNET_ID}" \
    --query 'Subnets[0].VpcId' --output text 2>/dev/null || echo "")"
  log "VPC ${vpc_for_probe}, subnet ${probe_subnets} (from config)"
elif [[ -n "${VPC_ID}" ]]; then
  vpc_for_probe="${VPC_ID}"
  probe_subnets="$(aws_ ec2 describe-subnets --filters "Name=vpc-id,Values=${vpc_for_probe}" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null || echo "")"
  log "VPC ${vpc_for_probe} (from config), subnet ${probe_subnets}"
elif [[ -n "${SELF_VPC_ID}" ]]; then
  vpc_for_probe="${SELF_VPC_ID}"
  probe_subnets="${SELF_SUBNET_ID}"
  log "VPC ${vpc_for_probe}, subnet ${probe_subnets} (this workstation's)"
else
  vpc_for_probe="$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "")"
  probe_subnets="$(aws_ ec2 describe-subnets --filters "Name=vpc-id,Values=${vpc_for_probe}" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null || echo "")"
  log "VPC ${vpc_for_probe}, subnet ${probe_subnets} (account default)"
fi

step "SSH reachability (the check that prevents a silent timeout later)"
if [[ -n "${SECURITY_GROUP_ID}" ]]; then
  sgs_for_probe="${SECURITY_GROUP_ID}"; sg_src="config"
elif [[ -n "${SELF_SG_IDS}" && "${SELF_VPC_ID}" == "${vpc_for_probe}" ]]; then
  sgs_for_probe="${SELF_SG_IDS}"; sg_src="inherited from this workstation"
else
  sgs_for_probe="$(aws_ ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${vpc_for_probe}" "Name=group-name,Values=default" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")"
  sg_src="the VPC's default group"
fi
if [[ -z "${sgs_for_probe}" || "${sgs_for_probe}" == "None" ]]; then
  warn "FAIL: could not determine a security group for ${vpc_for_probe}"
  fail=1
else
  log "groups: ${sgs_for_probe} (${sg_src})"
  if [[ -n "${SELF_PRIVATE_IP}" ]]; then
    if verify_ssh_reachable "${sgs_for_probe}" "${SELF_PRIVATE_IP}" "${SELF_SG_IDS}"; then
      log "ok: ${SSH_REACH_WHY}"
    else
      warn "FAIL: ${SSH_REACH_WHY}
     02-prepare-disks.sh would not be able to reach the instance. Add an
     inbound rule for port 22, or point SECURITY_GROUP_ID at a group that
     has one."
      fail=1
    fi
  else
    warn "cannot verify: this host's address is unknown (not on EC2)"
  fi
fi

step "egress path (needed by 03-setup-env.sh for Docker, the Cube bundle and datasets)"
# Whether the instance ends up internet-reachable is inherited from the
# subnet unless ASSOCIATE_PUBLIC_IP forces it, so report what will actually
# happen before anything is launched.
if [[ -n "${probe_subnets}" && "${probe_subnets}" != "None" ]]; then
  automap="$(aws_ ec2 describe-subnets --subnet-ids ${probe_subnets} \
    --query 'Subnets[0].MapPublicIpOnLaunch' --output text 2>/dev/null || echo "?")"
  log "subnet ${probe_subnets}: MapPublicIpOnLaunch=${automap}"
  case "${ASSOCIATE_PUBLIC_IP}" in
    auto) effective="${automap}" ;;
    yes)  effective="True" ;;
    no)   effective="False" ;;
  esac
  log "ASSOCIATE_PUBLIC_IP=${ASSOCIATE_PUBLIC_IP} -> public IP: ${effective}"
  igw="$(aws_ ec2 describe-internet-gateways \
    --filters "Name=attachment.vpc-id,Values=${vpc_for_probe}" \
    --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || echo "None")"
  nat="$(aws_ ec2 describe-nat-gateways \
    --filter "Name=vpc-id,Values=${vpc_for_probe}" "Name=state,Values=available" \
    --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null || echo "None")"
  log "VPC ${vpc_for_probe}: igw=${igw} nat=${nat}"
  if [[ "${effective}" == "True" ]]; then
    if [[ "${igw}" == "None" ]]; then
      warn "the instance will get a public IP but the VPC has no internet gateway,
     so it still has no egress and 03-setup-env.sh will hang on its first download."
      fail=1
    else
      log "ok: egress via the public IP + ${igw}"
      warn "internet-reachable. Keep the security group at 22-from-VPC:
     this host runs an UNAUTHENTICATED CubeAPI on :3000 and a console on :12088."
    fi
  else
    if [[ "${nat}" == "None" ]]; then
      warn "FAIL: no public IP and no NAT gateway in ${vpc_for_probe}.
     03-setup-env.sh needs egress for Docker packages, the ~400 MB CubeSandbox bundle
     and the datasets. Either set ASSOCIATE_PUBLIC_IP=auto/yes, or add a NAT
     gateway, or provide VPC endpoints for S3/ECR."
      fail=1
    else
      log "ok: egress via ${nat}"
    fi
  fi
fi

step "instance type availability"
# Same helper 01 uses, so both paths judge the type identically: bare metal,
# offered in this region, priced for the cost columns, and big enough for the
# configured concurrency.
validate_instance_type "${INSTANCE_TYPE}" || fail=1
azs="${TYPE_AZS}"

step "architecture and AMI"
arch="$(aws_ ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" \
         --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' \
         --output text 2>/dev/null || true)"
case "${arch}" in
  arm64)  ami_arch=arm64 ;;
  x86_64) ami_arch=amd64 ;;
  *) warn "FAIL: could not determine architecture for ${INSTANCE_TYPE} (got '${arch}')"; fail=1; ami_arch="" ;;
esac
if [[ -n "${ami_arch}" ]]; then
  log "ok: architecture ${arch} -> Ubuntu ${UBUNTU_RELEASE} ${ami_arch}"
  ssm_path="/aws/service/canonical/ubuntu/server/${UBUNTU_RELEASE}/stable/current/${ami_arch}/hvm/ebs-gp3/ami-id"
  if ami="$(aws_ ssm get-parameter --name "${ssm_path}" --query 'Parameter.Value' --output text 2>/dev/null)"; then
    log "ok: AMI ${ami}"
  else
    warn "SSM lookup failed for ${ssm_path}; 01-create-instance.sh will fall back to describe-images"
  fi
fi

step "IAM instance profile"
if [[ -z "${IAM_INSTANCE_PROFILE}" ]]; then
  warn "IAM_INSTANCE_PROFILE is empty.
     The instance builds and pushes a container image to ECR, and
     CubeSandbox mints its own ECR token to pull the template image.
     Without a profile, 03-setup-env.sh fails at the build step with a 401 and
     04-provision-templates.sh cannot pull. Set one, or use a non-ECR REGISTRY."
  # Path-independent: REGISTRY may be a bare ECR hostname or carry a
  # namespace. (scripts/_lib.sh has registry_is_ecr for this, but the
  # tools/ scripts source tools/lib.sh instead.)
  [[ "${REGISTRY%%/*}" == *.dkr.ecr.*.amazonaws.com ]] && fail=1
else
  if aws_ iam get-instance-profile --instance-profile-name "${IAM_INSTANCE_PROFILE}" >/dev/null 2>&1; then
    log "ok: instance profile ${IAM_INSTANCE_PROFILE}"
  else
    warn "FAIL: instance profile '${IAM_INSTANCE_PROFILE}' not found"
    fail=1
  fi
fi

step "EBS parameters"
# gp3 caps: 80,000 IOPS, 2,000 MiB/s, and max IOPS needs >=160 GiB.
if (( DATA_VOLUME_IOPS > 80000 )); then
  warn "FAIL: DATA_VOLUME_IOPS=${DATA_VOLUME_IOPS} exceeds the gp3 maximum of 80000"; fail=1
elif (( DATA_VOLUME_IOPS > DATA_VOLUME_GB * 500 )); then
  warn "FAIL: gp3 allows at most 500 IOPS per GiB: ${DATA_VOLUME_GB} GiB caps at $(( DATA_VOLUME_GB * 500 ))"; fail=1
else
  log "ok: ${DATA_VOLUME_GB} GiB / ${DATA_VOLUME_IOPS} IOPS"
fi
if (( DATA_VOLUME_THROUGHPUT > 2000 )); then
  warn "FAIL: DATA_VOLUME_THROUGHPUT=${DATA_VOLUME_THROUGHPUT} exceeds the gp3 maximum of 2000 MiB/s"; fail=1
elif (( DATA_VOLUME_THROUGHPUT * 4 > DATA_VOLUME_IOPS )); then
  warn "FAIL: gp3 gives 0.25 MiB/s per provisioned IOPS, so ${DATA_VOLUME_IOPS} IOPS caps
     throughput at $(( DATA_VOLUME_IOPS / 4 )) MiB/s"; fail=1
else
  log "ok: ${DATA_VOLUME_THROUGHPUT} MiB/s"
fi

# The instance's own EBS ceiling matters too: provisioning more than the
# instance can use is wasted money.
inst_iops="$(aws_ ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" \
  --query 'InstanceTypes[0].EbsInfo.EbsOptimizedInfo.MaximumIops' --output text 2>/dev/null || echo "")"
inst_mbps="$(aws_ ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" \
  --query 'InstanceTypes[0].EbsInfo.EbsOptimizedInfo.MaximumThroughputInMBps' --output text 2>/dev/null || echo "")"
if [[ -n "${inst_iops}" && "${inst_iops}" != "None" ]]; then
  log "instance EBS ceiling: ${inst_iops} IOPS / ${inst_mbps} MB/s"
  (( DATA_VOLUME_IOPS > inst_iops )) && warn "provisioned IOPS exceed what ${INSTANCE_TYPE} can drive"
fi

step "cost"
price="$(hourly_price "${INSTANCE_TYPE}")"
if [[ -n "${price}" ]]; then
  warn "${INSTANCE_TYPE} is \$${price}/hour on-demand.
     Plus roughly \$0.5-0.7/hour for the ${DATA_VOLUME_GB} GiB /
     ${DATA_VOLUME_IOPS} IOPS gp3 volume.
     tools/99-teardown.sh terminates the instance AND deletes both volumes."
else
  warn "no price known for ${INSTANCE_TYPE}; add it to INSTANCE_PRICES in env.example
     so the benchmark can compute cost per unit"
fi

step "repo sanity"
CHECK_NAME="scripts/setup-host.sh present"; check test -f "${REPO_DIR}/scripts/setup-host.sh"
CHECK_NAME="scripts/provision-templates.sh present"; check test -f "${REPO_DIR}/scripts/provision-templates.sh"
CHECK_NAME="scripts/run-suite.sh present"; check test -f "${REPO_DIR}/scripts/run-suite.sh"

echo >&2
if (( fail )); then
  die "preflight found blocking problems (above). Fix them before tools/01-create-instance.sh."
fi
log "preflight passed. Next: bash tools/01-create-instance.sh"
