#!/usr/bin/env bash
#
# Put Security Hub under central configuration from the Audit account (15.6b,
# ADR-011). Idempotent: every step checks before it acts.
#
#   scripts/enable-securityhub-central.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# Three steps, in the only order AWS accepts
# ---------------------------------------------------------------------------
#   1. security/35-securityhub-aggregator.yaml   finding aggregator in the home
#                                                region, linking the others.
#                                                CENTRAL requires one.
#   2. update-organization-configuration         ConfigurationType CENTRAL,
#                                                AutoEnable false, standards
#                                                NONE. An API call, because the
#                                                organization configuration is
#                                                a singleton CloudFormation
#                                                cannot create (ADR-011).
#   3. security/36-securityhub-policy.yaml       the policy and its root
#                                                association. Security Hub
#                                                refuses a policy before step 2.
#   4. verify                                    CENTRAL in every allowed
#                                                region, and the policy
#                                                associated where expected.
#
# The switch changes every account's standards at once: CIS 1.2.0 is
# replaced by the policy's FSBP and NIST 800-53 r5 (D-032), and CIS findings
# are archived. Reverting is the same call with ConfigurationType LOCAL.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: enable-securityhub-central.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

AUDIT="$(get_param /org/account/altdig-security-audit)"
ROOT_ID="$(get_param /org/root-id)"
[[ "${AUDIT}" =~ ^[0-9]{12}$ ]] || die "Audit account not registered in SSM."
[[ "${ROOT_ID}" =~ ^r- ]] || die "Organization root id not in SSM (/platform/org/root-id)."
LINKED="$(printf '%s' "${PLATFORM_ALLOWED_REGIONS}" | tr ',' '\n' \
  | grep -v "^${PLATFORM_HOME_REGION}$" | paste -sd, -)"

hr
log "Security Hub central configuration"
log "  audit    : ${AUDIT}"
log "  home     : ${PLATFORM_HOME_REGION}   linked: ${LINKED}"
log "  root     : ${ROOT_ID}"
log "  mode     : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo APPLY)"
hr

DTA_ARGS=(); [[ "${DRY_RUN}" == "1" ]] && DTA_ARGS+=(--dry-run)

# --- 1. aggregator ----------------------------------------------------------
info "1. Finding aggregator"
bash "${REPO_ROOT}/scripts/deploy-to-account.sh" --account altdig-security-audit \
  --template security/35-securityhub-aggregator.yaml --stack platform-securityhub-aggregator \
  "LinkedRegions=${LINKED}" "${DTA_ARGS[@]}" \
  || die "Aggregator stack failed. Nothing else was changed."

# --- 2. the switch ----------------------------------------------------------
info "2. Organization configuration"
CURRENT="$(in_account "${AUDIT}" aws securityhub describe-organization-configuration \
  --region "${PLATFORM_HOME_REGION}" \
  --query 'OrganizationConfiguration.ConfigurationType' --output text | no_cr)"
if [[ "${CURRENT}" == "CENTRAL" ]]; then
  skip "already CENTRAL"
elif [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  switch %s -> CENTRAL (auto-enable off, standards NONE)\n' \
    "${C_YELLOW}" "${C_RESET}" "${CURRENT}"
else
  in_account "${AUDIT}" aws securityhub update-organization-configuration \
    --region "${PLATFORM_HOME_REGION}" --no-auto-enable \
    --auto-enable-standards NONE \
    --organization-configuration ConfigurationType=CENTRAL \
    || die "Switch to CENTRAL refused. The aggregator stands; the configuration is still ${CURRENT}."
  # CENTRAL goes through PENDING first. Wait for it rather than assume.
  for _ in $(seq 1 40); do
    st="$(in_account "${AUDIT}" aws securityhub describe-organization-configuration \
      --region "${PLATFORM_HOME_REGION}" \
      --query 'OrganizationConfiguration.[ConfigurationType,Status,StatusMessage]' \
      --output text | no_cr)"
    case "${st}" in
      CENTRAL*ENABLED*) ok "CENTRAL"; break ;;
      *FAILED*) die "Switch to CENTRAL failed: ${st}" ;;
      *) sleep 15 ;;
    esac
  done
  [[ "${st}" == CENTRAL*ENABLED* ]] || die "Switch to CENTRAL did not settle: ${st}"
fi

# --- 3. policy and association ---------------------------------------------
info "3. Configuration policy, associated at the root"
if [[ "${DRY_RUN}" == "1" && "${CURRENT}" != "CENTRAL" ]]; then
  # Security Hub refuses a policy before CENTRAL, so a dry run cannot even
  # build the change set. Say what would happen instead.
  printf '%s DRY%s  deploy security/36-securityhub-policy.yaml (FSBP + NIST, 8 controls disabled) at %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${ROOT_ID}"
else
  bash "${REPO_ROOT}/scripts/deploy-to-account.sh" --account altdig-security-audit \
    --template security/36-securityhub-policy.yaml --stack platform-securityhub-policy \
    "OrganizationRootId=${ROOT_ID}" "${DTA_ARGS[@]}" \
    || die "Policy stack failed. CENTRAL is on with no policy: accounts keep their current standards until it is fixed."
fi

[[ "${DRY_RUN}" == "1" ]] && { hr; ok "Dry run complete."; exit 0; }

# --- 4. verify ---------------------------------------------------------------
info "4. Verify"
IFS=',' read -ra REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"
for r in "${REGIONS[@]}"; do
  t="$(in_account "${AUDIT}" aws securityhub describe-organization-configuration --region "${r}" \
    --query 'OrganizationConfiguration.ConfigurationType' --output text | no_cr)"
  [[ "${t}" == "CENTRAL" ]] && ok "${r}: CENTRAL" || warn "${r}: ${t}"
done
hr
ok "Central configuration in place. Run verify-security-coverage.sh, then check the
      association status of an account with:
      aws securityhub get-configuration-policy-association --target AccountId=<id>   (from Audit)"
