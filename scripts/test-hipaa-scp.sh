#!/usr/bin/env bash
#
# Prove PlatformHipaaEligibleOnly does what it says, on a Sandbox account.
#
#   scripts/test-hipaa-scp.sh --account <sandbox account id> [--region us-east-2]
#
# Proposed V12 (docs/hipaa-eligibility.md §9): "non-eligible service calls are
# denied in enforced accounts, and eligible ones are not."
#
# ---------------------------------------------------------------------------
# Why this is not a section of test-guardrails.sh
# ---------------------------------------------------------------------------
# Every other SCP is attached to the Sandbox OU permanently, so the main harness
# tests what is already there. This one must NOT be attached to Sandbox
# permanently: it denies Synthetics and EventBridge Scheduler (pending, E1),
# which the canary and the HOOP scheduler in Sandbox use. So this script
# attaches it to ONE Sandbox account, tests, and detaches — always, on any exit.
#
# ---------------------------------------------------------------------------
# How a denial is proved to be this SCP
# ---------------------------------------------------------------------------
# test-guardrails.sh differentiates against an EXEMPT probe. Statement A exempts
# nobody, so that is unavailable. Instead each call runs twice as the same
# principal, before the attachment and after it:
#
#   before NOT AccessDenied, after AccessDenied   -> this SCP.        PASS
#   after AccessDenied naming a service control policy             -> PASS
#   after NOT denied                              -> guardrail absent. FAIL
#   before AccessDenied too                       -> proves nothing.   AMBIG
#
# Nothing changes between the two runs except this one attachment, so a flip
# can only be the attachment. Calls are read-only or carry parameters that
# cannot succeed, so nothing is created whichever way they go.
#
# The subject is OrganizationAccountAccessRole: the break-glass administrator,
# exempt from several other SCPs and deliberately exempt from nothing here.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""; TEST_REGION="${PLATFORM_HOME_REGION}"
POLICY_NAME="PlatformHipaaEligibleOnly"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    --region)  TEST_REGION="$2"; shift 2 ;;
    -h|--help) sed -n '3,6p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || die "--account <12-digit id> is required."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

SANDBOX_OU="$(get_param /org/ou/sandbox)"
PARENT="$(aws organizations list-parents --child-id "${TARGET_ACCOUNT}" \
  --query 'Parents[0].Id' --output text | no_cr)"
[[ "${PARENT}" == "${SANDBOX_OU}" ]] || die "${TARGET_ACCOUNT} is not in the Sandbox OU (${PARENT}).
      This script attaches and detaches; it runs on Sandbox only."

PID="$(aws organizations list-policies --filter SERVICE_CONTROL_POLICY \
  --query "Policies[?Name=='${POLICY_NAME}'].Id | [0]" --output text | no_cr)"
[[ "${PID}" =~ ^p- ]] || die "${POLICY_NAME} does not exist. Create it first:
      scripts/deploy-policies.sh --only 09-hipaa-eligible-only.json"

attached() {
  aws organizations list-policies-for-target --target-id "${TARGET_ACCOUNT}" \
    --filter SERVICE_CONTROL_POLICY --query "Policies[?Id=='${PID}'] | length(@)" \
    --output text | no_cr
}
[[ "$(attached)" == "0" ]] || die "${POLICY_NAME} is already attached to ${TARGET_ACCOUNT}.
      The before-run would be meaningless. Detach it first."

read -r S_KEY S_SECRET S_TOKEN <<<"$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name hipaa-scp-test --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text | no_cr)"
[[ -n "${S_TOKEN}" ]] || die "Could not assume into ${TARGET_ACCOUNT}."
# Same shape as test-guardrails.sh as_test: explicit keys outrank the profile,
# and the profile is left alone (an empty AWS_PROFILE= is B-061).
as_subject() { AWS_ACCESS_KEY_ID="${S_KEY}" AWS_SECRET_ACCESS_KEY="${S_SECRET}" \
  AWS_SESSION_TOKEN="${S_TOKEN}" AWS_DEFAULT_REGION="${TEST_REGION}" "$@"; }

denied() { printf '%s' "$1" | grep -qiE 'AccessDenied|UnauthorizedOperation|not authorized|explicit deny'; }

# --- fixture: a Pinpoint application -----------------------------------------
# Pinpoint resolves the application before it evaluates authorization, so a
# made-up id answers NotFoundException with or without the SCP and proves
# nothing (observed 2026-10-04) — the KMS and Backup lesson again. A real,
# empty application is free; it is kept between runs like the harness's other
# fixtures. Its voice channel is only ever set to DISABLED.
PINPOINT_APP="$(as_subject aws pinpoint get-apps \
  --query "ApplicationsResponse.Item[?Name=='hipaa-scp-test-fixture'].Id | [0]" \
  --output text 2>/dev/null | no_cr)"
if [[ -z "${PINPOINT_APP}" || "${PINPOINT_APP}" == "None" ]]; then
  PINPOINT_APP="$(as_subject aws pinpoint create-app --create-application-request \
    '{"Name":"hipaa-scp-test-fixture","tags":{"Owner":"platform","Application":"hipaa-scp-test","DataClassification":"internal"}}' \
    --query 'ApplicationResponse.Id' --output text | no_cr)" \
    || die "Could not create the Pinpoint fixture."
  ok "created Pinpoint fixture ${PINPOINT_APP}"
fi

# --- the calls --------------------------------------------------------------
# label | expectation (deny|allow) | command
CALLS=(
  "apprunner:ListServices (not-allowlisted)|deny|aws apprunner list-services"
  "lightsail:GetInstances (not-allowlisted)|deny|aws lightsail get-instances"
  "codeartifact:ListDomains (not-allowlisted)|deny|aws codeartifact list-domains"
  "synthetics:DescribeCanaries (pending, E1)|deny|aws synthetics describe-canaries"
  "aoss:ListCollections (pending, E1)|deny|aws opensearchserverless list-collections"
  "ds:CreateDirectory, Simple AD (excluded feature)|deny|aws ds create-directory --name hipaa-scp-test.invalid --password Zz1!aaaaaaaa --size Small"
  # Voice messages: the voice CHANNEL on the fixture, set to disabled, not
  # sms-voice:SendVoiceMessage. That service validates input before
  # authorization (ValidationException SENDER_ID_NOT_SUPPORTED with or without
  # the SCP, 2026-10-04), so an inert send proves nothing, and a valid one would
  # send a message. Same statement; Access Analyzer validates the action names.
  "mobiletargeting:UpdateVoiceChannel (excluded feature)|deny|aws pinpoint update-voice-channel --application-id ${PINPOINT_APP} --voice-channel-request Enabled=false"
  "s3:ListAllMyBuckets (eligible)|allow|aws s3api list-buckets"
  "ecs:ListClusters (eligible)|allow|aws ecs list-clusters"
  "lambda:ListFunctions (eligible)|allow|aws lambda list-functions --max-items 1"
  "ds:DescribeDirectories (eligible service, other action)|allow|aws ds describe-directories"
  "iam:ListRoles (carve-out)|allow|aws iam list-roles --max-items 1"
  "servicequotas:ListServices (carve-out)|allow|aws service-quotas list-services --max-results 1"
  "sts:GetCallerIdentity (carve-out)|allow|aws sts get-caller-identity"
)

declare -A BEFORE
info "Before attaching: every call as ${TARGET_ACCOUNT}/OrganizationAccountAccessRole"
for c in "${CALLS[@]}"; do
  IFS='|' read -r label expect cmd <<<"${c}"
  # shellcheck disable=SC2086
  out="$(as_subject ${cmd} 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -ne 0 ]] && denied "${out}"; then BEFORE["${label}"]=denied; else BEFORE["${label}"]=ok; fi
  printf '  %-52s %s\n' "${label}" "${BEFORE[${label}]}"
done

RECORDER_BEFORE="$(as_subject aws configservice describe-configuration-recorder-status \
  --query 'ConfigurationRecordersStatus[0].[lastStatus,lastErrorCode]' --output text 2>&1 | no_cr)"

# --- attach, with a guaranteed detach ----------------------------------------
detach() {
  if [[ "$(attached)" != "0" ]]; then
    aws organizations detach-policy --policy-id "${PID}" --target-id "${TARGET_ACCOUNT}" \
      && ok "detached ${POLICY_NAME} from ${TARGET_ACCOUNT}" \
      || warn "DETACH FAILED. Detach ${PID} from ${TARGET_ACCOUNT} by hand, now."
  fi
}
trap detach EXIT
info "Attaching ${POLICY_NAME} (${PID}) to ${TARGET_ACCOUNT}"
aws organizations attach-policy --policy-id "${PID}" --target-id "${TARGET_ACCOUNT}"

# SCP changes propagate in seconds to a couple of minutes. Wait until the first
# deny-expected call flips, up to three minutes, rather than sleeping blind.
info "Waiting for the attachment to take effect"
for i in $(seq 1 36); do
  out="$(as_subject aws apprunner list-services 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -ne 0 ]] && denied "${out}"; then ok "in effect after ~$((i*5))s"; break; fi
  sleep 5
done

# --- after ----------------------------------------------------------------------
PASS=0; FAIL=0; AMBIG=0
hr
for c in "${CALLS[@]}"; do
  IFS='|' read -r label expect cmd <<<"${c}"
  # An attachment does not reach every service at the same moment. Seen
  # 2026-10-04: App Runner denied within five seconds while Lightsail, on the
  # same run, still answered. So a deny-expected call is retried for up to two
  # minutes before it counts as absent; an allow-expected call is not, because
  # retrying a permit only gives an over-broad policy time to look correct.
  for attempt in $(seq 1 12); do
    # shellcheck disable=SC2086
    out="$(as_subject ${cmd} 2>&1)" && rc=0 || rc=$?
    is_denied=0; [[ ${rc} -ne 0 ]] && denied "${out}" && is_denied=1
    [[ "${expect}" == "allow" || ${is_denied} -eq 1 ]] && break
    sleep 10
  done
  if [[ ${is_denied} -eq 0 && "${expect}" == "deny" ]]; then
    printf '        last response: %s\n' "$(printf '%s' "${out}" | tr -d '\n' | cut -c1-160)"
  fi
  if [[ "${expect}" == "allow" ]]; then
    if [[ ${is_denied} -eq 0 ]]; then
      printf '%s  PASS%s  %-52s permitted\n' "${C_GREEN}" "${C_RESET}" "${label}"; PASS=$((PASS+1))
    else
      printf '%s  FAIL%s  %-52s DENIED — the policy is over-broad\n' "${C_RED}" "${C_RESET}" "${label}"; FAIL=$((FAIL+1))
    fi
    continue
  fi
  if [[ ${is_denied} -eq 0 ]]; then
    printf '%s  FAIL%s  %-52s NOT denied — guardrail absent\n' "${C_RED}" "${C_RESET}" "${label}"; FAIL=$((FAIL+1))
  elif printf '%s' "${out}" | grep -qi 'service control policy'; then
    printf '%s  PASS%s  %-52s denied, SCP named\n' "${C_GREEN}" "${C_RESET}" "${label}"; PASS=$((PASS+1))
  elif [[ "${BEFORE[${label}]}" == "ok" ]]; then
    printf '%s  PASS%s  %-52s denied after, not before — the attachment\n' "${C_GREEN}" "${C_RESET}" "${label}"; PASS=$((PASS+1))
  else
    printf '%s AMBIG%s  %-52s denied both before and after\n' "${C_YELLOW}" "${C_RESET}" "${label}"; AMBIG=$((AMBIG+1))
  fi
done
hr

RECORDER_AFTER="$(as_subject aws configservice describe-configuration-recorder-status \
  --query 'ConfigurationRecordersStatus[0].[lastStatus,lastErrorCode]' --output text 2>&1 | no_cr)"
log "  Config recorder status before: ${RECORDER_BEFORE}"
log "  Config recorder status after : ${RECORDER_AFTER}"
log "  (B-160: whether the recorder errors under Statement A. A few minutes of"
log "   attachment may not provoke a recording pass; this is an observation, not a test.)"
hr
log "  ${PASS} passed, ${AMBIG} ambiguous, ${FAIL} failed"
hr
[[ ${FAIL} -eq 0 ]] || die "${FAIL} assertion(s) failed."
[[ ${AMBIG} -eq 0 ]] || { warn "${AMBIG} ambiguous. Read them before relying on the policy."; exit 0; }
ok "PlatformHipaaEligibleOnly holds: non-eligible denied, eligible and carve-outs permitted."
