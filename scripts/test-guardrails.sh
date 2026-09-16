#!/usr/bin/env bash
#
# Guardrail test harness — prove the SCPs actually deny what they claim to.
#
# Open item V1 is the load-bearing assumption this exists to test:
#   "a member account with full local administrator cannot remove detective
#    controls"
#
# It breaks quietly. An SCP that has been edited, detached, or shadowed by an
# exclusion still appears attached in the console and still reads as
# protective. The only way to know is to attempt the denied action and look at
# what comes back.
#
# Usage:
#   scripts/test-guardrails.sh --account <id> [--region us-east-2] [--json]
#
# Requires credentials in the MANAGEMENT account; it assumes into the target.
#
# ---------------------------------------------------------------------------
# Telling an SCP denial from any other failure
# ---------------------------------------------------------------------------
# This is the whole difficulty, and prompt 1.3 warns about it explicitly:
# "assert denial rather than relying on the action failing for another reason."
#
# Some services say so plainly:
#   "...with an explicit deny in a service control policy"
#
# Others do not. AWS Backup returns "Insufficient privileges to perform this
# action"; Organizations returns "You don't have permissions to access this
# resource". Both are AccessDenied with no indication of what denied them, so a
# message match alone cannot distinguish an SCP from an IAM gap or a
# service-side constraint.
#
# So every test with an exclusion runs TWICE, against two principals:
#
#   GuardrailTestAdmin    AdministratorAccess, ordinary name, matches no
#                         exclusion. Subject to every denial.
#   PlatformHarnessProbe  AdministratorAccess, named Platform*, therefore
#                         excluded by policies 01 and 02.
#
#   test DENIED + probe NOT denied  -> the SCP is what denied it.   CONFIRMED
#   test DENIED + probe DENIED      -> something else denied it, or the
#                                      statement has no exclusion.  AMBIGUOUS
#   test ALLOWED                    -> the guardrail is absent.     FAIL
#
# The probe is created by OrganizationAccountAccessRole, which policy 03
# excludes from the Platform* naming denial precisely so that platform roles
# can be created in a fresh account. That the probe can be created at all is
# itself a check on that exclusion.
#
# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
# Destructive calls target resources that do not exist, so nothing is destroyed
# whether or not the guardrail fires. The one exception is KMS: it resolves the
# key before evaluating authorization and returns NotFoundException for a
# made-up id, which tells us nothing. That test therefore uses a real key
# created for the purpose.
#
# NOTE: that key cannot be cleaned up. Scheduling its deletion is denied by the
# very policy under test, for OrganizationAccountAccessRole as well. It is a
# permanent fixture in the canary account at roughly $1/month. That is the
# correct trade — see policies/scp/README.md.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""
TEST_REGION="${PLATFORM_HOME_REGION}"
OUTPUT_JSON=0
TEST_ROLE="GuardrailTestAdmin"
PROBE_ROLE="PlatformHarnessProbe"
KEY_ALIAS="alias/guardrail-harness-fixture"

usage() {
  cat <<'USAGE'
Usage: test-guardrails.sh --account <id> [--region <region>] [--json]

  --account   Target member account id. Must NOT be the management account:
              SCPs never apply there, so a pass would be meaningless.
  --region    Region to run calls in. Defaults to the platform home region.
  --json      Emit machine-readable results for scheduled runs.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    --region)  TEST_REGION="$2"; shift 2 ;;
    --json)    OUTPUT_JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${TARGET_ACCOUNT}" ]] || { usage; die "--account is required."; }
[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || die "Account id must be 12 digits."
[[ "${TARGET_ACCOUNT}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "Refusing to test the management account. SCPs do not apply to it,
      so the result would be meaningless. Target a member account."

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
export AWS_DEFAULT_REGION="${TEST_REGION}"

assume() {
  aws sts assume-role --role-arn "$1" --role-session-name "$2" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr
}

info "Assuming OrganizationAccountAccessRole in ${TARGET_ACCOUNT}"
read -r O_KEY O_SECRET O_TOKEN <<<"$(assume \
  "arn:aws:iam::${TARGET_ACCOUNT}:role/OrganizationAccountAccessRole" guardrail-setup)"
[[ -n "${O_KEY:-}" ]] \
  || die "Could not assume OrganizationAccountAccessRole in ${TARGET_ACCOUNT}.
      That role exists only in accounts created by Organizations."

as_org() { AWS_ACCESS_KEY_ID="${O_KEY}" AWS_SECRET_ACCESS_KEY="${O_SECRET}" \
           AWS_SESSION_TOKEN="${O_TOKEN}" "$@"; }

ensure_role() {
  local role="$1"
  local trust="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${PLATFORM_MGMT_ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"
  as_org aws iam create-role --role-name "${role}" \
    --assume-role-policy-document "${trust}" \
    --description "Guardrail harness principal" >/dev/null 2>&1 || true
  as_org aws iam attach-role-policy --role-name "${role}" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess >/dev/null 2>&1 || true
}

info "Ensuring harness principals exist"
ensure_role "${TEST_ROLE}"
ensure_role "${PROBE_ROLE}"

# The probe must exist for the differential to mean anything. If policy 03's
# exclusion for OrganizationAccountAccessRole is wrong, creation fails here and
# every differential result silently degrades to AMBIGUOUS — so check.
PROBE_OK=1
as_org aws iam get-role --role-name "${PROBE_ROLE}" >/dev/null 2>&1 || PROBE_OK=0
[[ ${PROBE_OK} -eq 1 ]] || warn "Probe role ${PROBE_ROLE} could not be created — \
differential checks will report AMBIGUOUS. Policy 03's OrganizationAccountAccessRole \
exclusion may be wrong."

sleep 10   # sts:AssumeRole is eventually consistent after role creation

read -r T_KEY T_SECRET T_TOKEN <<<"$(assume "arn:aws:iam::${TARGET_ACCOUNT}:role/${TEST_ROLE}" gr-test)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume ${TEST_ROLE}. IAM propagation delay?"
read -r P_KEY P_SECRET P_TOKEN <<<"$(assume "arn:aws:iam::${TARGET_ACCOUNT}:role/${PROBE_ROLE}" gr-probe)"
[[ -n "${P_KEY:-}" ]] || PROBE_OK=0

as_test()  { AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" \
             AWS_SESSION_TOKEN="${T_TOKEN}" "$@"; }
as_probe() { AWS_ACCESS_KEY_ID="${P_KEY}" AWS_SECRET_ACCESS_KEY="${P_SECRET}" \
             AWS_SESSION_TOKEN="${P_TOKEN}" "$@"; }

# --- KMS fixture -----------------------------------------------------------
# Created via the probe role, which is excluded from the KMS denial. Reused
# across runs; see the note at the top about why it cannot be cleaned up.
info "Ensuring KMS test fixture"
KEY_ID="$(as_org aws kms describe-key --key-id "${KEY_ALIAS}" \
  --query 'KeyMetadata.KeyId' --output text 2>/dev/null | no_cr | sed 's/^None$//' || true)"
if [[ -z "${KEY_ID}" ]]; then
  # Deliberately NOT tagged platform-managed. kms:CreateKey --tags requires
  # kms:TagResource, which DenyPlatformManagedTagTampering denies for any
  # principal outside its exclusion list — OrganizationAccountAccessRole
  # included. The SCP doing its job would otherwise block its own test fixture.
  # ProtectKmsKeys carries no tag condition, so the tag is not needed here.
  KEY_ID="$(as_org aws kms create-key \
    --description "Guardrail harness fixture - deletion denied by the SCP under test" \
    --tags TagKey=Purpose,TagValue=guardrail-harness \
    --query 'KeyMetadata.KeyId' --output text 2>/dev/null | no_cr || true)"
  if [[ -n "${KEY_ID}" ]]; then
    as_org aws kms create-alias --alias-name "${KEY_ALIAS}" \
      --target-key-id "${KEY_ID}" >/dev/null 2>&1 || true
  fi
fi
if [[ -n "${KEY_ID}" ]]; then ok "KMS fixture ${KEY_ID}"; else warn "No KMS fixture — that test is skipped"; fi

# --- Backup vault fixture --------------------------------------------------
# AWS Backup returns AccessDenied for a vault that does not exist, regardless
# of whether the caller is permitted — information hiding, verified against an
# excluded principal. A made-up name therefore proves nothing.
#
# Against a REAL vault the inference is sound: the subject holds
# AdministratorAccess, so if the call is denied, the denial can only come from
# an SCP. No differential probe is needed, and none is used.
#
# Unlike the KMS key this vault IS deletable by the platform, so it costs
# nothing and can be cleaned up. If the guardrail were absent the subject would
# delete it — that is the failure path, it is reported, and the vault is simply
# recreated on the next run.
VAULT_NAME="guardrail-harness-fixture"
info "Ensuring Backup vault fixture"
as_org aws backup create-backup-vault --backup-vault-name "${VAULT_NAME}" \
  >/dev/null 2>&1 || true
VAULT_OK=0
as_org aws backup describe-backup-vault --backup-vault-name "${VAULT_NAME}" \
  >/dev/null 2>&1 && VAULT_OK=1
if [[ ${VAULT_OK} -eq 1 ]]; then ok "Backup fixture ${VAULT_NAME}"; else warn "No Backup fixture — that test is skipped"; fi

# --- classification --------------------------------------------------------

denied()     { printf '%s' "$1" | grep -qiE 'AccessDenied|UnauthorizedOperation|not authorized|Insufficient privileges'; }
scp_denied() { printf '%s' "$1" | grep -qi 'service control policy'; }

PASS=0; FAIL=0; AMBIG=0; RESULTS=()

emit() {
  local verdict="$1" label="$2" ref="$3" detail="$4" colour
  case "${verdict}" in
    PASS)  colour="${C_GREEN}"; PASS=$((PASS+1)) ;;
    AMBIG) colour="${C_YELLOW}"; AMBIG=$((AMBIG+1)) ;;
    *)     colour="${C_RED}"; FAIL=$((FAIL+1)) ;;
  esac
  printf '%s%6s%s  %-34s %s\n' "${colour}" "${verdict}" "${C_RESET}" "${label}" "${ref}"
  [[ "${verdict}" == PASS ]] || printf '        %s\n' "${detail}"
  RESULTS+=("${verdict}|${ref}|${label}|${detail}")
}

# check <policy> <sid> <label> <differential:yes|no> -- <cmd...>
check() {
  local policy="$1" sid="$2" label="$3" diff_mode="$4"; shift 4
  [[ "$1" == "--" ]] && shift
  local ref="${policy}/${sid}" t_out t_rc p_out p_rc

  t_out="$(as_test "$@" 2>&1)" && t_rc=0 || t_rc=$?

  if [[ ${t_rc} -eq 0 ]]; then
    emit FAIL "${label}" "${ref}" "action SUCCEEDED — guardrail absent"; return
  fi
  if ! denied "${t_out}"; then
    emit FAIL "${label}" "${ref}" \
      "got past authorization — $(printf '%s' "${t_out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-100)"
    return
  fi
  if scp_denied "${t_out}"; then
    emit PASS "${label}" "${ref}" "denied, SCP named explicitly"; return
  fi

  # Denied, but the service did not say by what. How we resolve that depends on
  # what the test had available:
  #
  #   yes   an exclusion exists -> run the same call as the excluded probe
  #   real  the target resource genuinely exists, and the subject holds
  #         AdministratorAccess, so an AccessDenied can only be an SCP
  #   no    neither -> honestly ambiguous, say so
  if [[ "${diff_mode}" == "real" ]]; then
    emit PASS "${label}" "${ref}" "denied on a real resource with AdministratorAccess — only an SCP can do that"
    return
  fi
  if [[ "${diff_mode}" != "yes" ]]; then
    emit AMBIG "${label}" "${ref}" \
      "denied, but service does not name the SCP and there is no exclusion to differentiate against"
    return
  fi
  if [[ ${PROBE_OK} -ne 1 ]]; then
    emit AMBIG "${label}" "${ref}" "denied, but probe role unavailable — cannot differentiate"; return
  fi

  p_out="$(as_probe "$@" 2>&1)" && p_rc=0 || p_rc=$?
  if [[ ${p_rc} -eq 0 ]] || ! denied "${p_out}"; then
    # Excluded principal got through, non-excluded did not. That difference can
    # only be the SCP.
    emit PASS "${label}" "${ref}" "denied; excluded principal permitted — SCP confirmed by differential"
  else
    emit AMBIG "${label}" "${ref}" \
      "denied for BOTH principals — exclusion not working, or denied by something other than the SCP"
  fi
}

# expect_allowed — the negative control. Without it, a harness that cannot
# authenticate reports a clean sweep.
expect_allowed() {
  local label="$1"; shift
  [[ "$1" == "--" ]] && shift
  if as_test "$@" >/dev/null 2>&1; then
    emit PASS "${label}" "control" "permitted as expected"
  else
    emit FAIL "${label}" "control" "control action BLOCKED — harness broken or SCP over-broad"
  fi
}

NOPE="guardrail-harness-does-not-exist"

hr
log "Guardrail harness"
log "  target account : ${TARGET_ACCOUNT}"
log "  region         : ${TEST_REGION}"
log "  subject        : ${TEST_ROLE}  (admin, matches no exclusion)"
log "  probe          : ${PROBE_ROLE} (admin, matches Platform* exclusion)"
hr

check 01 ProtectCloudTrail "cloudtrail:StopLogging" yes -- \
  aws cloudtrail stop-logging --name "${NOPE}"

check 01 ProtectConfigRecorder "config:DeleteConfigurationRecorder" yes -- \
  aws configservice delete-configuration-recorder --configuration-recorder-name "${NOPE}"

check 01 ProtectThreatDetection "guardduty:DeleteDetector" yes -- \
  aws guardduty delete-detector --detector-id "00000000000000000000000000000000"

check 01 ProtectFlowAndDnsLogging "ec2:DeleteFlowLogs" yes -- \
  aws ec2 delete-flow-logs --flow-log-ids "fl-00000000000000000"

if [[ -n "${KEY_ID}" ]]; then
  check 02 ProtectKmsKeys "kms:ScheduleKeyDeletion" yes -- \
    aws kms schedule-key-deletion --key-id "${KEY_ID}" --pending-window-in-days 30
fi

if [[ ${VAULT_OK} -eq 1 ]]; then
  check 02 ProtectBackupVaults "backup:DeleteBackupVault" real -- \
    aws backup delete-backup-vault --backup-vault-name "${VAULT_NAME}"
fi

# The critical one. If this fails, a member administrator can name their way
# out of every wildcard exclusion in policies 01 and 02, and the whole set is
# decorative. No differential: the probe is itself excluded here.
check 03 DenyPlatformRoleNameSquatting "iam:CreateRole Platform*" no -- \
  aws iam create-role --role-name "PlatformEscapeHatch" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

check 03 DenyLeavingTheOrganization "organizations:LeaveOrganization" no -- \
  aws organizations leave-organization

expect_allowed "sts:GetCallerIdentity" -- aws sts get-caller-identity

hr
log "  ${PASS} passed, ${AMBIG} ambiguous, ${FAIL} failed"
hr

if [[ ${OUTPUT_JSON} -eq 1 ]]; then
  printf '{"account":"%s","region":"%s","passed":%d,"ambiguous":%d,"failed":%d,"results":[' \
    "${TARGET_ACCOUNT}" "${TEST_REGION}" "${PASS}" "${AMBIG}" "${FAIL}"
  first=1
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r v ref l d <<<"${r}"
    [[ ${first} -eq 1 ]] || printf ','
    printf '{"verdict":"%s","ref":"%s","label":"%s","detail":"%s"}' "${v}" "${ref}" "${l}" "${d//\"/}"
    first=0
  done
  printf ']}\n'
fi

[[ ${FAIL} -eq 0 ]] || die "${FAIL} guardrail assertion(s) FAILED. Do not vest accounts until clean —
      a guardrail that does not hold is worse than one known to be absent,
      because it is trusted."
[[ ${AMBIG} -eq 0 ]] || { warn "${AMBIG} assertion(s) AMBIGUOUS: denied, but not provably by the SCP.
      Acceptable only if you have read the detail and understand why."; exit 0; }
ok "All guardrail assertions passed, each provably by service control policy."
