#!/usr/bin/env bash
#
# Wait for a new account's baseline StackSets to settle, retry what failed
# once, and refuse to call the account baselined until every instance is
# CURRENT (B-062).
#
#   scripts/settle-baseline.sh --account <id> [--dry-run] [--wait-minutes N]
#
# ---------------------------------------------------------------------------
# Why
# ---------------------------------------------------------------------------
# An account moved into a targeted OU receives every service-managed StackSet
# at once, in no particular order. Two of them depend on others:
#
#   platform-kms-secrets      creates a Config rule, so it needs the recorder
#                             from platform-config first
#   platform-instrumentation  creates log groups under alias/platform-logs,
#                             so it needs platform-log-data-protection first
#
# Sandbox Exercise, 2026-09-19: both failed on first deploy, nothing retried
# them, and they sat at OUTDATED. The account looked baselined and had no
# instrumentation, which is how alarms get created at all.
#
# So, three phases:
#   1. settle  wait until every StackSet that should reach this account has an
#              instance for it, and none is still PENDING or RUNNING
#   2. retry   once, in dependency order, for this account only:
#              update-stack-instances with no overrides, which re-runs the
#              instance from the stack set as it stands. Additive: it creates
#              what the first attempt could not. It deletes nothing.
#   3. verify  every instance CURRENT. Anything else stops here with the list.
#              That is the else-branch: a human looks, and the account is not
#              called baselined.
#
# Runs from Infra Tooling as delegated administrator, like deploy-stackset.sh
# (D-012). Start with management-account credentials.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""; WAIT_MINUTES=30

# Dependency order for the retry. Anything targeting the account but not
# listed here is retried after these, in name order.
ORDER=(platform-config platform-log-data-protection platform-kms-secrets
       platform-network platform-developer-boundary platform-event-forwarding
       platform-instrumentation platform-response platform-security-responder
       platform-canary-artifacts)

usage() {
  cat <<'USAGE'
Usage: settle-baseline.sh --account <id> [--dry-run] [--wait-minutes N]

  --account       The account to settle. Not the management account.
  --wait-minutes  How long phase 1 waits for auto-deployment. Default 30.
  --dry-run       Report what would be retried; change nothing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)      TARGET_ACCOUNT="$2"; shift 2 ;;
    --wait-minutes) WAIT_MINUTES="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || { usage; die "--account <12-digit id> is required."; }
[[ "${WAIT_MINUTES}" =~ ^[0-9]+$ ]] || die "--wait-minutes must be a whole number."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
[[ "${TARGET_ACCOUNT}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "StackSets never deploy to the management account."

# --- what should reach this account: OUs from it up to the root ----------
# Read while still in the management account. A stack set targeting any OU on
# this path reaches the account, because OU targets include child OUs.
ANCESTORS=()
child="${TARGET_ACCOUNT}"
for _ in 1 2 3 4 5 6; do
  parent="$(aws organizations list-parents --child-id "${child}" \
    --query 'Parents[0].[Id,Type]' --output text | no_cr)"
  read -r pid ptype <<<"${parent}"
  ANCESTORS+=("${pid}")
  [[ "${ptype}" == "ROOT" ]] && break
  child="${pid}"
done

TOOLING_ACCOUNT="$(get_param /org/account/altdig-infra-tooling)"
read -r T_KEY T_SECRET T_TOKEN <<<"$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-settle-baseline \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume OrganizationAccountAccessRole in Infra Tooling."
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" AWS_SESSION_TOKEN="${T_TOKEN}"
[[ "$(aws sts get-caller-identity --query Account --output text | no_cr)" == "${TOOLING_ACCOUNT}" ]] \
  || die "Expected to be operating from Infra Tooling."
cfn_ss() { aws cloudformation "$@" --call-as DELEGATED_ADMIN; }

# Stack sets whose targets include an OU on the account's path.
EXPECTED=()
for ss in $(cfn_ss list-stack-sets --status ACTIVE --query 'Summaries[?PermissionModel==`SERVICE_MANAGED`].StackSetName' \
              --output text | no_cr); do
  targets=" $(cfn_ss describe-stack-set --stack-set-name "${ss}" \
    --query 'StackSet.OrganizationalUnitIds' --output text | no_cr | tr '\t\n' '  ') "
  for a in "${ANCESTORS[@]}"; do
    if [[ "${targets}" == *" ${a} "* ]]; then EXPECTED+=("${ss}"); break; fi
  done
done
[[ ${#EXPECTED[@]} -gt 0 ]] || die "No stack set targets this account's OUs. Is it in the right OU?"

# Ordered: ORDER first, then the rest by name.
ORDERED=()
for ss in "${ORDER[@]}"; do
  for e in "${EXPECTED[@]}"; do [[ "${e}" == "${ss}" ]] && ORDERED+=("${ss}"); done
done
for e in $(printf '%s\n' "${EXPECTED[@]}" | sort); do
  [[ " ${ORDER[*]} " == *" ${e} "* ]] || ORDERED+=("${e}")
done

# instances_of <stackset> -> lines "region status detailed-status" for this account
instances_of() {
  cfn_ss list-stack-instances --stack-set-name "$1" --stack-instance-account "${TARGET_ACCOUNT}" \
    --query 'Summaries[].[Region,Status,StackInstanceStatus.DetailedStatus]' \
    --output text | no_cr
}

hr
log "Settle baseline"
log "  account  : ${TARGET_ACCOUNT}"
log "  expected : ${#ORDERED[@]} stack set(s): ${ORDERED[*]}"
log "  mode     : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo APPLY)"
hr

# --- 1. settle --------------------------------------------------------------
info "1. Waiting for every expected stack set to reach the account and finish"
deadline=$(( $(date +%s) + WAIT_MINUTES * 60 ))
while :; do
  pending=""
  for ss in "${ORDERED[@]}"; do
    rows="$(instances_of "${ss}")"
    if [[ -z "${rows}" ]]; then pending+=" ${ss}(no instance yet)"; continue; fi
    while read -r region status detail; do
      [[ -z "${region}" ]] && continue
      case "${detail}" in PENDING|RUNNING) pending+=" ${ss}/${region}(${detail})" ;; esac
    done <<<"${rows}"
  done
  [[ -z "${pending}" ]] && { ok "settled"; break; }
  if (( $(date +%s) >= deadline )); then
    warn "Still not settled after ${WAIT_MINUTES} minute(s):${pending}"
    break
  fi
  printf '  waiting on:%s\n' "${pending}"
  sleep 30
done

# --- 2. retry, once, in order -----------------------------------------------
info "2. Retrying anything that did not come up CURRENT"
RETRIED=0
for ss in "${ORDERED[@]}"; do
  while read -r region status detail; do
    [[ -z "${region}" ]] && continue
    [[ "${status}" == "CURRENT" ]] && continue
    # INOPERABLE means the stack set lost track of the stack. A re-run cannot
    # repair that, and pretending otherwise would hide it. Leave it for phase 3.
    [[ "${detail}" == "INOPERABLE" ]] && continue
    case "${detail}" in PENDING|RUNNING) continue ;; esac
    RETRIED=$((RETRIED+1))
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  retry %s in %s (was %s/%s)\n' "${C_YELLOW}" "${C_RESET}" "${ss}" "${region}" "${status}" "${detail}"
      continue
    fi
    info "retry ${ss} in ${region} (was ${status}/${detail})"
    op="$(cfn_ss update-stack-instances --stack-set-name "${ss}" \
      --deployment-targets "OrganizationalUnitIds=${ANCESTORS[0]},AccountFilterType=INTERSECTION,Accounts=${TARGET_ACCOUNT}" \
      --regions "${region}" --query OperationId --output text 2>&1 | no_cr)" || {
        warn "could not start the retry of ${ss}: ${op}"; continue; }
    for _ in $(seq 1 80); do
      st="$(cfn_ss describe-stack-set-operation --stack-set-name "${ss}" --operation-id "${op}" \
        --query 'StackSetOperation.Status' --output text | no_cr)"
      case "${st}" in RUNNING|QUEUED|STOPPING) sleep 15 ;; *) break ;; esac
    done
    log "  ${ss}: retry ${st}"
  done <<<"$(instances_of "${ss}")"
done
[[ ${RETRIED} -gt 0 ]] || ok "nothing to retry"

# --- 3. verify --------------------------------------------------------------
info "3. Verifying"
BAD=""
for ss in "${ORDERED[@]}"; do
  rows="$(instances_of "${ss}")"
  [[ -n "${rows}" ]] || { BAD+=$'\n'"        ${ss}: no instance for this account"; continue; }
  while read -r region status detail; do
    [[ -z "${region}" ]] && continue
    [[ "${status}" == "CURRENT" ]] || BAD+=$'\n'"        ${ss} ${region}: ${status}/${detail}"
  done <<<"${rows}"
done
hr
if [[ -n "${BAD}" ]]; then
  [[ "${DRY_RUN}" == "1" ]] && { warn "Not baselined (dry run, nothing was retried):${BAD}"; exit 0; }
  die "Account ${TARGET_ACCOUNT} is NOT baselined. After one retry:${BAD}
      Read each instance's StatusReason (list-stack-instances --call-as DELEGATED_ADMIN
      from Infra Tooling) before retrying again. A second identical failure is a
      real fault, not ordering."
fi
ok "Account ${TARGET_ACCOUNT}: all ${#ORDERED[@]} baseline stack set(s) CURRENT."
