#!/usr/bin/env bash
#
# Create one partner OU beneath Members.
#
# This is the AWS half of partner onboarding (design doc 15). It does NOT do
# the rest: partner-level SCPs, Cost Category dimension, Truveon tenant, Jira
# project, Entra commercial access group, or the registry entry in 'partner'
# state. Those are later phases. A partner is not 'active' because this script
# ran.
#
# Usage:
#   scripts/create-partner-ou.sh --slug oeight --legal-name "OEight Ltd" \
#     --domain oeight.io [--dry-run]

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SLUG=""; LEGAL_NAME=""; DOMAIN=""

usage() {
  cat <<'USAGE'
Usage: create-partner-ou.sh --slug <slug> --legal-name <name> [--domain <domain>] [--dry-run]

  --slug         Machine-safe partner id. Lowercase alphanumeric and hyphens,
                 1-20 chars, no dots. Becomes the OU name and the <partner>
                 segment of every account alias beneath it.
  --legal-name   Partner legal or trading name (display and audit trail).
  --domain       Partner primary domain, e.g. oeight.io. Optional.
  --dry-run      Produce a changeset without executing it.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)       SLUG="$2"; shift 2 ;;
    --legal-name) LEGAL_NAME="$2"; shift 2 ;;
    --domain)     DOMAIN="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${SLUG}" ]]       || { usage; die "--slug is required."; }
[[ -n "${LEGAL_NAME}" ]] || { usage; die "--legal-name is required."; }

validate_slug partner "${SLUG}"

# 'direct' is the reserved partner slot for AltDigital's own clients and is
# created by the org-structure stack. Creating it again here would produce a
# duplicate OU with the same name under the same parent, which AWS permits and
# which would silently split the billing roll-up.
[[ "${SLUG}" != "direct" ]] \
  || die "'direct' is reserved and already exists — created by 00-org-structure.yaml."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

MEMBERS_OU="$(get_param /org/ou/members)"
[[ -n "${MEMBERS_OU}" && "${MEMBERS_OU}" != "None" ]] \
  || die "Members OU id not found at ${PLATFORM_SSM_PREFIX}/org/ou/members.
      Run scripts/deploy-org-structure.sh first."

# Duplicate-name detection. AWS allows two sibling OUs with the same name; that
# would be a silent billing-roll-up split, so refuse rather than create.
EXISTING="$(find_ou "${MEMBERS_OU}" "${SLUG}")"
STACK_NAME="platform-partner-${SLUG}"
if [[ -n "${EXISTING}" ]]; then
  STACK_EXISTS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].StackName' --output text 2>/dev/null || true)"
  if [[ -z "${STACK_EXISTS}" || "${STACK_EXISTS}" == "None" ]]; then
    die "An OU named '${SLUG}' already exists under Members (${EXISTING}) but is
      not managed by stack ${STACK_NAME}. Adopt or remove it by hand before
      continuing — deploying now would create a second OU with the same name."
  fi
  skip "OU '${SLUG}' exists (${EXISTING}) and is managed by ${STACK_NAME}; updating."
fi

hr
log "Partner OU"
log "  slug       : ${SLUG}"
log "  legal name : ${LEGAL_NAME}"
log "  domain     : ${DOMAIN:-(none)}"
log "  parent     : Members (${MEMBERS_OU})"
log "  alias stem : ${PLATFORM_ACCOUNT_PREFIX}-${SLUG}-<client>-<env>"
hr

cfn_deploy "${STACK_NAME}" "${REPO_ROOT}/org/10-partner-ou.yaml" \
  "PartnerSlug=${SLUG}" \
  "PartnerLegalName=${LEGAL_NAME}" \
  "PartnerDomain=${DOMAIN}" \
  "MembersOuId=${MEMBERS_OU}" \
  "SsmPrefix=${PLATFORM_SSM_PREFIX}"

if [[ "${DRY_RUN}" != "1" ]]; then
  hr
  cfn_outputs "${STACK_NAME}" | sed 's/^/  /'
  hr
  warn "Partner OU created. The partner is NOT yet active:"
  warn "  contract and BAA, notification window, downstream SLA commitments,"
  warn "  Cost Category dimension, Truveon tenant and registry entry are all"
  warn "  still outstanding. Client onboarding must block until they exist."
fi
