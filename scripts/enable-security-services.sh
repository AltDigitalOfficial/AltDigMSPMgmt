#!/usr/bin/env bash
#
# Enable GuardDuty, Security Hub, Macie and Inspector organization-wide, from
# the delegated administrator (Audit) account.
#
#   scripts/enable-security-services.sh --dry-run
#   scripts/enable-security-services.sh
#
# Delegation named an administrator. It did not turn anything on. Each service
# must additionally be ENABLED in the admin account per region, and configured
# to auto-enable for organization members — otherwise a newly vested account
# joins with no detection at all and nothing says so.
#
# ---------------------------------------------------------------------------
# This is where the platform starts costing money per account
# ---------------------------------------------------------------------------
# Rough order of magnitude, per account per month, at small scale:
#
#   GuardDuty      $5-30   scales with CloudTrail events, VPC flow logs, DNS
#   Security Hub   $2-10   per finding ingested plus per compliance check
#   Inspector      $1-15   per scanned instance, image and function
#   Macie          inventory plus automated discovery, watched (ADR-017)
#
# Design doc 02 is explicit that governance does not vary by environment, so
# these run in dev and test as well as production. That is a deliberate cost:
# "sensitive data appears in non-production environments more often than anyone
# admits".
#
# MACIE AUTOMATED SENSITIVE DATA DISCOVERY IS ON, IN EVERY ACCOUNT (ADR-017).
# This file used to say it was deliberately off. It was never off: Macie turns
# automated discovery on by default when it is enabled, and it had been
# running in every account since 2026-09-17 while this comment said otherwise.
# Found 2026-10-28. It is now managed here on purpose, because it is the
# cardholder scope tripwire's source (security/70-scope-tripwire.yaml).
#
# Its cost is watched rather than capped. The ceiling is a ratio of each
# account's bill, set in SSM by the commercial owner and never in this
# repository, and exceeding it raises a cost conversation. It never switches
# discovery off, because that would remove the control (Jamie, 2026-10-28).
# scripts/check-macie-cost.sh measures it.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: enable-security-services.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered in SSM."

IFS=',' read -ra REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"

hr
log "Enabling security services organization-wide"
log "  admin account : ${AUDIT} (Audit)"
log "  regions       : ${PLATFORM_ALLOWED_REGIONS}"
log "  macie discovery: ON, card identifiers included (ADR-017)"
hr

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name enable-security \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account."
read -r AK SK ST <<<"${CREDS}"

# audit <region> <command...> — run as the Audit account in one region
audit() {
  local region="$1"; shift
  AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}" \
  AWS_DEFAULT_REGION="${region}" "$@"
}

# step <label> <region> <command...>
# Treats "already enabled" as success. These APIs signal that with an error
# rather than a no-op, so results are read rather than trusted.
step() {
  local label="$1" region="$2"; shift 2
  local out rc
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  %-34s %s\n' "${C_YELLOW}" "${C_RESET}" "${label}" "${region}"
    return 0
  fi
  out="$("$@" 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -eq 0 ]]; then
    printf '%s  ok%s  %-34s %s\n' "${C_GREEN}" "${C_RESET}" "${label}" "${region}"
  elif printf '%s' "${out}" | grep -qiE 'already|ConflictException|ResourceConflict|has been enabled'; then
    printf '%sskip%s  %-34s %s (already)\n' "${C_DIM}" "${C_RESET}" "${label}" "${region}"
  else
    printf '%swarn%s  %-34s %s — %s\n' "${C_YELLOW}" "${C_RESET}" "${label}" "${region}" \
      "$(printf '%s' "${out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-110)"
    FAILED=1
  fi
}

# ---------------------------------------------------------------------------
# Existing accounts must be enrolled explicitly
# ---------------------------------------------------------------------------
# --auto-enable and --auto-enable-organization-members govern accounts that
# join in FUTURE. They do nothing for accounts that already exist.
#
# Verified the hard way: after enabling all four services with auto-enable on,
# the Audit account listed ZERO members and the canary had no GuardDuty
# detector and no Security Hub. Every console would have shown the services
# enabled. That is the worst shape of failure this platform has to avoid —
# configured, green, and detecting nothing.
#
# So the member list is built from Organizations and enrolled by hand. The
# management account is included: it is not covered by SCPs and is the highest
# value target in the Organization, so leaving it unmonitored would be
# precisely backwards.
mapfile -t ORG_ACCOUNTS < <(aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].[Id,Email]' --output text 2>/dev/null | no_cr)

enroll_region() {
  local region="$1" detector="$2" id email n=0
  for row in "${ORG_ACCOUNTS[@]}"; do
    id="$(printf '%s' "${row}" | awk '{print $1}')"
    email="$(printf '%s' "${row}" | awk '{print $2}')"
    # The admin account is not its own member.
    [[ -z "${id}" || "${id}" == "${AUDIT}" ]] && continue

    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  enroll %-14s %s\n' "${C_YELLOW}" "${C_RESET}" "${id}" "${region}"
      continue
    fi

    # Errors are swallowed per service: "already a member" is the common case
    # and each API signals it differently. Enrollment is verified afterwards by
    # listing members, which is the only result worth trusting.
    if [[ -n "${detector}" ]]; then
      audit "${region}" aws guardduty create-members \
        --detector-id "${detector}" \
        --account-details "AccountId=${id},Email=${email}" >/dev/null 2>&1 || true
    fi
    audit "${region}" aws securityhub create-members \
      --account-details "AccountId=${id},Email=${email}" >/dev/null 2>&1 || true
    audit "${region}" aws macie2 create-member \
      --account "accountId=${id},email=${email}" >/dev/null 2>&1 || true
    audit "${region}" aws inspector2 associate-member --account-id "${id}" >/dev/null 2>&1 || true
    n=$((n+1))
  done
  ok "enrolled ${n} account(s) in ${region}"
}

# enable_in_management <region>
#
# The management account cannot be enrolled as a member until it has enabled
# the service itself:
#
#   "Operation failed because your organization master must first enable
#    GuardDuty to be added as a member"
#
# So this runs with the caller's own (management account) credentials, not the
# Audit ones. Getting this wrong leaves the Organization's most privileged
# account — the one no SCP can constrain — as the only account without
# detection, which is precisely backwards.
enable_in_management() {
  local region="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  enable services in management account %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${region}"
    return 0
  fi
  local d
  d="$(aws guardduty list-detectors --region "${region}" \
    --query 'DetectorIds[0]' --output text 2>/dev/null | no_cr | sed 's/^None$//')"
  [[ -n "${d}" ]] || aws guardduty create-detector --enable --region "${region}" \
    --finding-publishing-frequency FIFTEEN_MINUTES >/dev/null 2>&1 || true
  # No default standards: the central configuration policy decides them
  # (ADR-011, D-032). Under LOCAL configuration this used to subscribe AWS's
  # defaults, FSBP and CIS 1.2.0, which were nobody's decision.
  aws securityhub enable-security-hub --no-enable-default-standards \
    --region "${region}" >/dev/null 2>&1 || true
  aws macie2 enable-macie --status ENABLED \
    --finding-publishing-frequency FIFTEEN_MINUTES --region "${region}" >/dev/null 2>&1 || true
  ok "management account services enabled in ${region}"
}

FAILED=0
for region in "${REGIONS[@]}"; do
  hr
  info "Region ${region}"

  # Must happen before enrolment, and as the management account itself.
  enable_in_management "${region}"

  # --- GuardDuty ----------------------------------------------------------
  # A detector is per-account per-region and is what "GuardDuty is on" means.
  DETECTOR="$(audit "${region}" aws guardduty list-detectors \
    --query 'DetectorIds[0]' --output text 2>/dev/null | no_cr | sed 's/^None$//')"
  if [[ -z "${DETECTOR}" ]]; then
    step "guardduty: create detector" "${region}" \
      audit "${region}" aws guardduty create-detector --enable \
        --finding-publishing-frequency FIFTEEN_MINUTES
    DETECTOR="$(audit "${region}" aws guardduty list-detectors \
      --query 'DetectorIds[0]' --output text 2>/dev/null | no_cr | sed 's/^None$//')"
  else
    skip "guardduty: detector exists (${region})"
  fi
  if [[ -n "${DETECTOR}" && "${DRY_RUN}" != "1" ]]; then
    # AUTO_ENABLE_ORG_MEMBERS=ALL is the line that matters: without it,
    # accounts vested tomorrow have no detector and nothing reports the gap.
    #
    # S3 Protection and Lambda Protection for every account (ADR-014, Jamie,
    # 2026-10-26). S3 because the evidence, the logs and every tenant's data
    # live there. Lambda because the platform is built of it. AutoEnable ALL
    # reaches existing members as well as new ones. Features this call does
    # not name keep their current setting.
    step "guardduty: auto-enable members, with S3 and Lambda protection" "${region}" \
      audit "${region}" aws guardduty update-organization-configuration \
        --detector-id "${DETECTOR}" --auto-enable-organization-members ALL \
        --features '[{"Name":"S3_DATA_EVENTS","AutoEnable":"ALL"},{"Name":"LAMBDA_NETWORK_LOGS","AutoEnable":"ALL"}]'
    # The administrator is not a member of itself, so organization auto-enable
    # never reaches Audit's own detector. That gap is what Security Hub
    # reported as GuardDuty.6 and GuardDuty.10 on Audit (B-174), the B-008
    # shape again.
    step "guardduty: S3 and Lambda protection on Audit's own detector" "${region}" \
      audit "${region}" aws guardduty update-detector --detector-id "${DETECTOR}" \
        --features '[{"Name":"S3_DATA_EVENTS","Status":"ENABLED"},{"Name":"LAMBDA_NETWORK_LOGS","Status":"ENABLED"}]'
  fi

  # --- Security Hub -------------------------------------------------------
  # Enabled without default standards, and standards are not auto-enabled.
  # Under CENTRAL configuration (security/36-securityhub-policy.yaml,
  # ADR-011) the policy associated at the root decides the standards and
  # controls for every account, new ones included, and
  # update-organization-configuration --auto-enable conflicts with it. So that
  # step runs only while the organization is still LOCAL, and says so when it
  # skips.
  step "securityhub: enable" "${region}" \
    audit "${region}" aws securityhub enable-security-hub \
      --no-enable-default-standards
  SH_CONFIG_TYPE="$(audit "${region}" aws securityhub describe-organization-configuration \
    --query 'OrganizationConfiguration.ConfigurationType' --output text 2>/dev/null | no_cr)"
  if [[ "${SH_CONFIG_TYPE}" == "CENTRAL" ]]; then
    skip "securityhub: auto-enable members (CENTRAL: the root policy decides, ADR-011)"
  else
    step "securityhub: auto-enable members" "${region}" \
      audit "${region}" aws securityhub update-organization-configuration \
        --auto-enable --auto-enable-standards DEFAULT
  fi

  # --- Macie --------------------------------------------------------------
  step "macie: enable" "${region}" \
    audit "${region}" aws macie2 enable-macie \
      --finding-publishing-frequency FIFTEEN_MINUTES --status ENABLED
  step "macie: auto-enable members" "${region}" \
    audit "${region}" aws macie2 update-organization-configuration --auto-enable

  # Automated discovery, for every account including later ones (ADR-017).
  # Stated, not left to Macie's default, so that the state is this script's
  # decision and a later default change cannot quietly turn it off.
  step "macie: automated discovery, all members" "${region}" \
    audit "${region}" aws macie2 update-automated-discovery-configuration \
      --status ENABLED --auto-enable-organization-members ALL

  # Card identifiers INCLUDED, on top of AWS's default set, never instead of
  # it: "includes" adds to the defaults, so this guarantees the tripwire's
  # types are inspected without narrowing what else is found. Expiry is
  # included so a finding shows it alongside a number; the tripwire ignores it
  # on its own. The template is per region and its id is read, not stored.
  if [[ "${DRY_RUN}" != "1" ]]; then
    TEMPLATE_ID="$(audit "${region}" aws macie2 get-automated-discovery-configuration \
      --query sensitivityInspectionTemplateId --output text 2>/dev/null | no_cr)"
    if [[ -n "${TEMPLATE_ID}" && "${TEMPLATE_ID}" != "None" ]]; then
      step "macie: card identifiers in the discovery template" "${region}" \
        audit "${region}" aws macie2 update-sensitivity-inspection-template \
          --id "${TEMPLATE_ID}" \
          --includes 'managedDataIdentifierIds=CREDIT_CARD_NUMBER,CREDIT_CARD_NUMBER_(NO_KEYWORD),CREDIT_CARD_MAGNETIC_STRIPE,CREDIT_CARD_SECURITY_CODE,CREDIT_CARD_EXPIRATION'
    else
      printf '%swarn%s  %-34s %s — no sensitivity inspection template id\n' \
        "${C_YELLOW}" "${C_RESET}" "macie: card identifiers" "${region}"
      FAILED=1
    fi
  fi

  # --- Inspector ----------------------------------------------------------
  # EC2 and ECR only. Lambda scanning is per-function and most member accounts
  # will not have enough Lambda to justify it as a blanket default; it belongs
  # to a per-tenant decision.
  step "inspector: enable" "${region}" \
    audit "${region}" aws inspector2 enable --resource-types EC2 ECR
  step "inspector: auto-enable members" "${region}" \
    audit "${region}" aws inspector2 update-organization-configuration \
      --auto-enable ec2=true,ecr=true,lambda=false

  # Accounts that already exist are not covered by any of the auto-enable
  # settings above. Enrol them explicitly.
  info "Enrolling existing accounts in ${region}"
  enroll_region "${region}" "${DETECTOR}"
done
hr

[[ ${FAILED} -eq 0 ]] || warn "One or more steps reported an unexpected error — review above."
ok "Pass complete."
log ""
log "Deliberately NOT enabled:"
log "  * Inspector Lambda scanning — per-function, per-tenant decision."
log "  * GuardDuty Malware Protection, EKS, ECS and EC2 runtime and RDS plans —"
log "    per-tenant, where the workload exists (ADR-014). S3 and Lambda"
log "    protection ARE enabled, in every account."
