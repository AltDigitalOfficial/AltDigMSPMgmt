#!/usr/bin/env bash
#
# Assert that every account in the Organization is actually covered by every
# detective service, in every allowed region.
#
#   scripts/verify-security-coverage.sh          # assert, non-zero on any gap
#   scripts/verify-security-coverage.sh --json   # machine-readable
#
# ---------------------------------------------------------------------------
# Why this exists as an assertion rather than a note in a runbook
# ---------------------------------------------------------------------------
# B-008. Enabling GuardDuty, Security Hub, Macie and Inspector with
# --auto-enable set in every region produced an Organization where the Audit
# account had ZERO members and the canary had no detector at all — while every
# console showed the services enabled.
#
# Three separate steps look identical from the console:
#
#   delegation   names an administrator
#   enablement   turns the service on in the admin account
#   enrolment    actually covers an account
#
# The first two produce a system that appears monitored and is not. Only
# enrolment is coverage, and only counting members proves enrolment.
#
# The arithmetic is the whole test:
#
#   members(service, region)  ==  accounts(ACTIVE)  -  1
#
# minus one because the delegated administrator is not its own member. Any
# other number is a gap, and a gap discovered here is free where the same gap
# discovered during an incident is not.
#
# This runs after every account creation (see create-platform-account.sh) and
# belongs in the phase 12.5 verification sweep alongside the evidence-receipt
# and paging checks.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

OUTPUT_JSON=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)    OUTPUT_JSON=1; shift ;;
    -h|--help) log "Usage: verify-security-coverage.sh [--json]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered in SSM."

TOTAL="$(aws organizations list-accounts \
  --query 'length(Accounts[?Status==`ACTIVE`])' --output text | no_cr)"
EXPECTED=$((TOTAL - 1))

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name verify-coverage \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account."
read -r AK SK ST <<<"${CREDS}"

a() { local region="$1"; shift
  AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}" \
  AWS_DEFAULT_REGION="${region}" "$@" 2>/dev/null | no_cr
}

hr
log "Detective service coverage"
log "  active accounts   : ${TOTAL}"
log "  expected members  : ${EXPECTED}  (admin is not its own member)"
log "  regions           : ${PLATFORM_ALLOWED_REGIONS}"
hr

IFS=',' read -ra REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"
GAPS=0; ROWS=()

for region in "${REGIONS[@]}"; do
  DETECTOR="$(a "${region}" aws guardduty list-detectors --query 'DetectorIds[0]' --output text | sed 's/^None$//')"

  GD="0"; [[ -n "${DETECTOR}" ]] && GD="$(a "${region}" aws guardduty list-members \
    --detector-id "${DETECTOR}" --query 'length(Members)' --output text)"
  SH="$(a "${region}" aws securityhub list-members --query 'length(Members)' --output text)"
  MA="$(a "${region}" aws macie2 list-members --query 'length(members)' --output text)"
  IN="$(a "${region}" aws inspector2 list-members --query 'length(members)' --output text)"

  for pair in "guardduty:${GD}" "securityhub:${SH}" "macie:${MA}" "inspector:${IN}"; do
    svc="${pair%%:*}"; n="${pair##*:}"
    [[ "${n}" =~ ^[0-9]+$ ]] || n=0
    if [[ "${n}" -eq "${EXPECTED}" ]]; then
      printf '%s  ok%s  %-12s %-10s %s/%s members\n' \
        "${C_GREEN}" "${C_RESET}" "${svc}" "${region}" "${n}" "${EXPECTED}"
      ROWS+=("PASS|${svc}|${region}|${n}|${EXPECTED}")
    else
      printf '%s GAP%s  %-12s %-10s %s/%s members\n' \
        "${C_RED}" "${C_RESET}" "${svc}" "${region}" "${n}" "${EXPECTED}"
      ROWS+=("GAP|${svc}|${region}|${n}|${EXPECTED}")
      GAPS=$((GAPS+1))
    fi
  done
done
hr

# --- Security Hub: CENTRAL configuration, in every region -----------------
#
# The switch to CENTRAL is an API call, not a CloudFormation resource: the
# organization configuration is a singleton CloudFormation cannot create
# (ADR-011). So drift detection cannot see it reverted to LOCAL, and this
# check does. LOCAL would mean members choose their own standards again.
log "Security Hub configuration"
for region in "${REGIONS[@]}"; do
  t="$(a "${region}" aws securityhub describe-organization-configuration \
         --query 'OrganizationConfiguration.ConfigurationType' --output text)"
  if [[ "${t}" == "CENTRAL" ]]; then
    printf '%s  ok%s  %-12s %-10s CENTRAL\n' "${C_GREEN}" "${C_RESET}" "sh-config" "${region}"
    ROWS+=("PASS|securityhub-central|${region}|1|1")
  else
    printf '%s GAP%s  %-12s %-10s %s (expected CENTRAL)\n' "${C_RED}" "${C_RESET}" "sh-config" "${region}" "${t:-unreadable}"
    ROWS+=("GAP|securityhub-central|${region}|0|1")
    GAPS=$((GAPS+1))
  fi
done
hr

# --- AWS Config: a recorder that is RECORDING, per account, per region ------
#
# Config has no member list to count, so the arithmetic above does not apply.
# The organization aggregator in Audit (B-157) reports whether it COLLECTED
# from a source, which is not the same claim. Each account is asked directly:
# is a recorder recording in this region? On 2026-10-20 the answer was yes in
# 4 of 30 account-regions and nothing reported it (15.6a, ADR-010). A recorder
# that exists and is not recording is the green-and-empty state, so recording
# must be TRUE, not merely present.
log "AWS Config recorders"
for acct in $(aws organizations list-accounts \
                --query 'Accounts[?Status==`ACTIVE`].Id' --output text | no_cr); do
  if [[ "${acct}" == "${PLATFORM_MGMT_ACCOUNT_ID}" ]]; then
    cfg_q() { aws configservice describe-configuration-recorder-status --region "$1" \
                --query 'length(ConfigurationRecordersStatus[?recording])' --output text 2>/dev/null | no_cr; }
  else
    cfg_q() { in_account "${acct}" aws configservice describe-configuration-recorder-status --region "$1" \
                --query 'length(ConfigurationRecordersStatus[?recording])' --output text 2>/dev/null | no_cr; }
  fi
  for region in "${REGIONS[@]}"; do
    n="$(cfg_q "${region}" || true)"
    [[ "${n}" =~ ^[0-9]+$ ]] || n=0
    if [[ "${n}" -ge 1 ]]; then
      printf '%s  ok%s  %-12s %-10s %s recording\n' "${C_GREEN}" "${C_RESET}" "config" "${region}" "${acct}"
      ROWS+=("PASS|config:${acct}|${region}|1|1")
    else
      printf '%s GAP%s  %-12s %-10s %s not recording\n' "${C_RED}" "${C_RESET}" "config" "${region}" "${acct}"
      ROWS+=("GAP|config:${acct}|${region}|0|1")
      GAPS=$((GAPS+1))
    fi
  done
done
hr

# --- S3 object access: the data events trail (ADR-013) ----------------------
#
# The conformance pack's Guard rule proves the trail is configured and logging
# from Config's record of it. That record cannot see two things, so they are
# checked here: a DELETED trail (nothing left to evaluate) and the selectors
# themselves (Config records only that custom selectors exist). The selector
# check is exact: the main selector must cover every S3 object and exclude only
# the log archive. A second exclusion would silently stop evidencing a bucket.
log "S3 object access trail"
DE_TRAIL="platform-s3-data-events"
DE_MAIN="S3 object access in every bucket except the log archive"
de_logging="$(aws cloudtrail get-trail-status --region "${PLATFORM_HOME_REGION}" --name "${DE_TRAIL}" \
                --query IsLogging --output text 2>/dev/null | no_cr || true)"
de_type="$(aws cloudtrail get-event-selectors --region "${PLATFORM_HOME_REGION}" --trail-name "${DE_TRAIL}" \
             --query "AdvancedEventSelectors[?Name=='${DE_MAIN}'] | [0].FieldSelectors[?Field=='resources.type'] | [0].Equals" \
             --output text 2>/dev/null | no_cr || true)"
de_excl="$(aws cloudtrail get-event-selectors --region "${PLATFORM_HOME_REGION}" --trail-name "${DE_TRAIL}" \
             --query "AdvancedEventSelectors[?Name=='${DE_MAIN}'] | [0].FieldSelectors[?Field=='resources.ARN'] | [0].NotStartsWith" \
             --output text 2>/dev/null | no_cr || true)"
if [[ "${de_logging}" == "True" && "${de_type}" == "AWS::S3::Object" \
      && "${de_excl}" == "arn:aws:s3:::altdig-log-archive-" ]]; then
  printf '%s  ok%s  %-12s %-10s %s\n' "${C_GREEN}" "${C_RESET}" "s3-access" "org" \
    "${DE_TRAIL} logging; every bucket but the log archive"
  ROWS+=("PASS|s3-data-events|org|1|1")
else
  printf '%s GAP%s  %-12s %-10s %s logging=%s type=%s exclusions=%s\n' "${C_RED}" "${C_RESET}" \
    "s3-access" "org" "${DE_TRAIL}" "${de_logging:-absent}" "${de_type:-none}" "${de_excl:-none}"
  ROWS+=("GAP|s3-data-events|org|0|1")
  GAPS=$((GAPS+1))
fi
hr

if [[ ${OUTPUT_JSON} -eq 1 ]]; then
  printf '{"expected":%s,"gaps":%s,"results":[' "${EXPECTED}" "${GAPS}"
  first=1
  for r in "${ROWS[@]}"; do
    IFS='|' read -r v svc reg n exp <<<"${r}"
    [[ ${first} -eq 1 ]] || printf ','
    printf '{"verdict":"%s","service":"%s","region":"%s","members":%s,"expected":%s}' \
      "${v}" "${svc}" "${reg}" "${n}" "${exp}"
    first=0
  done
  printf ']}\n'
fi

[[ ${GAPS} -eq 0 ]] || die "${GAPS} coverage gap(s).
      Detective services: run scripts/enable-security-services.sh — it is
      idempotent and enrols existing accounts as well as future ones.
      Config: a member account is missing the platform-config stack set (its OU
      is not targeted, or its instance is not CURRENT; settle-baseline.sh
      shows which). The management account needs its own stack (ADR-010).
      S3 object access: org/31-s3-data-events-trail.yaml in the management
      account (ADR-013); an exclusion beyond the log archive is a gap."
ok "Every account is covered by every service in every region."
