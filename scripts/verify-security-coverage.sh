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
      shows which). The management account needs its own stack (ADR-010)."
ok "Every account is covered by every service in every region."
