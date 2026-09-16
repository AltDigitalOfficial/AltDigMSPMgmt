#!/usr/bin/env bash
#
# Deploy the fixed OU skeleton: Security, Infrastructure, Members,
# Members/direct, Sandbox.
#
# Runs in the Organization management account, after
# bootstrap-management-account.sh has created the Organization and published
# the root id to SSM.
#
# Usage:
#   scripts/deploy-org-structure.sh --dry-run
#   scripts/deploy-org-structure.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

STACK_NAME=platform-org-structure
TEMPLATE="${REPO_ROOT}/org/00-org-structure.yaml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: deploy-org-structure.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

ROOT_ID="$(get_param /org/root-id)"
if [[ -z "${ROOT_ID}" || "${ROOT_ID}" == "None" ]]; then
  warn "/platform/org/root-id not set in SSM; falling back to a live lookup."
  ROOT_ID="$(org_root_id || true)"
fi
[[ -n "${ROOT_ID}" && "${ROOT_ID}" != "None" ]] \
  || die "Could not determine the Organization root id.
      Run scripts/bootstrap-management-account.sh first."

hr
log "Org structure deploy"
log "  stack   : ${STACK_NAME}"
log "  root    : ${ROOT_ID}"
log "  region  : ${AWS_DEFAULT_REGION}"
hr

cfn_deploy "${STACK_NAME}" "${TEMPLATE}" \
  "RootId=${ROOT_ID}" \
  "SsmPrefix=${PLATFORM_SSM_PREFIX}"

if [[ "${DRY_RUN}" != "1" ]]; then
  hr
  log "OU ids:"
  cfn_outputs "${STACK_NAME}" | sed 's/^/  /'
  hr
  log "Next: scripts/create-partner-ou.sh --slug oeight --legal-name '<name>' --domain oeight.io --dry-run"
fi
