#!/usr/bin/env bash
# 04 - build the 7 CubeSandbox templates, then prove they are usable.
#
#   bash tools/04-provision-templates.sh
#
# Its own step, and the one you will re-run most often, because template
# materialisation does NOT survive a reboot: Cubelet mounts its state
# directory on tmpfs, and the node-local image registry lives there. After a
# reboot the template DEFINITIONS are still in MySQL and GET /templates
# still reports READY, so the platform looks perfectly healthy - and only
# sandbox create fails, with "template ... is not available locally".
#
# Which is why this script does not stop at "status: READY". It runs
# smoke_test.py --all, which creates a real sandbox from every template.
# That is the only check that distinguishes "registered" from "usable".
#
# Takes about 4 minutes for all seven (measured: 3m44s on c7g.metal).

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_config
ensure_ssh_key

PRIVATE_IP="$(state_require PRIVATE_IP)"
[[ "$(state_get ENV_READY || echo 0)" == "1" ]] || die "the environment is not installed.
     Run: bash tools/03-setup-env.sh"

wait_for_ssh 300

step "platform health"
# ensure-cube.sh is idempotent and prints the derived node sandbox ceiling,
# which is the number that decides whether the configured concurrency
# levels are even reachable. Worth seeing before spending 4 minutes on
# templates.
# shellcheck disable=SC2046
ssh $(_ssh_opts) -t "$(ssh_host)" \
  "cd ${REMOTE_REPO} && sudo bash scripts/ensure-cube.sh" \
  || die "the platform is unhealthy; ensure-cube.sh reported the reason above."

step "building templates"
# provision-templates.sh deletes and recreates each alias, so this is safe
# to re-run and is the documented recovery after a reboot. It fetches its
# own ECR credentials.
# shellcheck disable=SC2046
ssh $(_ssh_opts) -t "$(ssh_host)" \
  "cd ${REMOTE_REPO} && bash scripts/provision-templates.sh" \
  || die "template provisioning failed.
     If the error mentions 'authorization token has expired', the instance
     lost its ECR credentials - check the IAM instance profile.
     If a template build timed out, the usual cause is a service inside the
     image not answering its readiness probe within Cube's fixed ~30s
     budget."

step "proving the templates are usable"
# The distinction that matters: status READY is a database fact, while this
# actually boots a sandbox from each template and runs one step through it.
# shellcheck disable=SC2046
ssh $(_ssh_opts) -t "$(ssh_host)" \
  "cd ${REMOTE_REPO} && .venv/bin/python orchestrator/smoke_test.py --all" \
  || die "smoke test failed. Templates may be registered but not materialised
     on the node (see the header of this script). Re-run tools/04-provision-templates.sh."

state_set TEMPLATES_READY 1
echo >&2
log "Next: bash tools/05-run-benchmark.sh            (full suite)"
log "  or: bash tools/05-run-benchmark.sh B1         (one benchmark)"
