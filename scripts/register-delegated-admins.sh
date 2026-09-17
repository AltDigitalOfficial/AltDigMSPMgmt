#!/usr/bin/env bash
#
# Delegate administration of the security services to the Audit account.
#
#   scripts/register-delegated-admins.sh --dry-run
#   scripts/register-delegated-admins.sh
#
# ---------------------------------------------------------------------------
# Why delegate at all
# ---------------------------------------------------------------------------
# Design doc 02: "The Organization management account holds nothing but the
# Organization itself. No workloads, no pipelines, minimal access. Delegated
# administration is used for Config, Security Hub, GuardDuty, Backup, IAM
# Identity Center and CloudFormation StackSets."
#
# Without delegation, every day-to-day security operation — triaging a
# GuardDuty finding, tuning a Security Hub control — requires access to the
# management account, which is the one account no SCP can constrain. Delegation
# moves that work into Audit, where it can be governed like anything else.
#
# ---------------------------------------------------------------------------
# Two registration mechanisms, and the regional trap
# ---------------------------------------------------------------------------
# ORGANIZATION-LEVEL services register once, globally, through the
# Organizations API.
#
# REGIONAL services (GuardDuty, Security Hub, Macie, Inspector) each have their
# own API and must be registered SEPARATELY IN EVERY REGION. Registering only
# in the home region leaves the others silently unmonitored — and a member
# account is permitted to operate in any of PLATFORM_ALLOWED_REGIONS, so that
# is a real detection gap, not a theoretical one.
#
# This is the same class of problem as the region-restriction SCP in phase 10.2,
# which the prompts document calls out as "entirely innocent in intent" and
# productive of "a total visibility blind spot". Here the blind spot arrives by
# omission rather than by a developer's choice.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: register-delegated-admins.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

AUDIT_ACCOUNT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT_ACCOUNT}" && "${AUDIT_ACCOUNT}" != "None" ]] \
  || die "Audit account not found in SSM. Run:
      scripts/create-platform-account.sh --ou security --role audit"

IFS=',' read -ra REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"

hr
log "Delegated administration"
log "  delegate to : ${AUDIT_ACCOUNT} (Audit)"
log "  regions     : ${PLATFORM_ALLOWED_REGIONS}"
hr

# --- organization-level ----------------------------------------------------
# Registered once. Region-agnostic.

ORG_SERVICES=(
  access-analyzer.amazonaws.com             # org-wide external access findings
  config.amazonaws.com                      # Config as an organization
  config-multiaccountsetup.amazonaws.com    # Config aggregator in Audit
  backup.amazonaws.com                      # org backup policies and reporting
  reporting.trustedadvisor.amazonaws.com    # org-wide Trusted Advisor
)

info "Organization-level delegation"
CURRENT="$(aws organizations list-delegated-services-for-account \
  --account-id "${AUDIT_ACCOUNT}" --query 'DelegatedServices[].ServicePrincipal' \
  --output text 2>/dev/null | no_cr | tr '\t\n' '  ' || true)"

for sp in "${ORG_SERVICES[@]}"; do
  if [[ " ${CURRENT} " == *" ${sp} "* ]]; then
    skip "${sp}"
  else
    run "delegate ${sp}" \
      aws organizations register-delegated-administrator \
        --account-id "${AUDIT_ACCOUNT}" --service-principal "${sp}"
  fi
done
hr

# --- regional --------------------------------------------------------------
# Each of these has its own admin-registration API, and each is per-region.
#
# All four are idempotent-ish but return errors rather than success when the
# account is already the administrator, so failures are inspected rather than
# trusted: "already" in the message is a skip, anything else is real.

delegate_regional() {
  local label="$1" region="$2"; shift 2
  local out rc
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  %-12s %s\n' "${C_YELLOW}" "${C_RESET}" "${label}" "${region}"
    return 0
  fi
  out="$("$@" 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -eq 0 ]]; then
    printf '%s  ok%s  %-12s %s\n' "${C_GREEN}" "${C_RESET}" "${label}" "${region}"
  elif printf '%s' "${out}" | grep -qiE 'already|ConflictException|exists'; then
    printf '%sskip%s  %-12s %s (already delegated)\n' "${C_DIM}" "${C_RESET}" "${label}" "${region}"
  else
    printf '%swarn%s  %-12s %s — %s\n' "${C_YELLOW}" "${C_RESET}" "${label}" "${region}" \
      "$(printf '%s' "${out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-110)"
    REGIONAL_FAILED=1
  fi
}

REGIONAL_FAILED=0
for region in "${REGIONS[@]}"; do
  info "Regional delegation in ${region}"

  delegate_regional "guardduty" "${region}" \
    aws guardduty enable-organization-admin-account \
      --admin-account-id "${AUDIT_ACCOUNT}" --region "${region}"

  delegate_regional "securityhub" "${region}" \
    aws securityhub enable-organization-admin-account \
      --admin-account-id "${AUDIT_ACCOUNT}" --region "${region}"

  delegate_regional "macie" "${region}" \
    aws macie2 enable-organization-admin-account \
      --admin-account-id "${AUDIT_ACCOUNT}" --region "${region}"

  delegate_regional "inspector" "${region}" \
    aws inspector2 enable-delegated-admin-account \
      --delegated-admin-account-id "${AUDIT_ACCOUNT}" --region "${region}"
done
hr

if [[ ${REGIONAL_FAILED} -eq 1 ]]; then
  warn "One or more regional delegations did not succeed."
  warn "Security Hub and GuardDuty must be enabled in a region before an"
  warn "administrator can be delegated there. Re-run after enabling them."
fi

ok "Delegation pass complete."
log ""
log "NOT delegated here, deliberately:"
log "  * CloudFormation StackSets  -> belongs to Platform Tooling (infra OU),"
log "    which does not exist yet. Delegating it to Audit would put the"
log "    deployment pipeline in the account that audits it."
log "  * IAM Identity Center       -> stays in the management account; moving"
log "    it is disruptive and buys little while the directory holds one user."
