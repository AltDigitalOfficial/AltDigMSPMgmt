#!/usr/bin/env bash
#
# Deploy a baseline template as a service-managed StackSet targeting OUs.
#
#   scripts/deploy-stackset.sh --name platform-config \
#     --template baseline/10-config-recorder.yaml \
#     --ou sandbox --regions us-east-2 \
#     LogArchiveBucketName=... [--dry-run]
#
# ---------------------------------------------------------------------------
# Service-managed, and why that matters
# ---------------------------------------------------------------------------
# A SERVICE-MANAGED StackSet targets organizational units rather than a list of
# account ids, and AWS creates the execution role in each target account. Two
# consequences the design depends on:
#
#   * Auto-deployment. An account moved into a targeted OU receives the stack
#     without anyone running anything. That is what makes the baseline a
#     property of OU placement rather than of remembering to deploy — design
#     doc 02: "Places it in the correct OU (which triggers SCP and StackSet
#     inheritance)".
#
#   * No execution roles to create or protect. Self-managed StackSets need
#     AWSCloudFormationStackSetExecutionRole in every target account, which is
#     another Platform-adjacent role to provision and defend.
#
# The execution principal is 'stacksets-exec-<hash>', which SCPs 01, 02 and 03
# exclude. That exclusion was written from AWS documentation and has never been
# exercised — this script is where it gets tested. If it is wrong, stack
# instances fail with AccessDenied on role creation rather than anything
# clearer.
#
# ---------------------------------------------------------------------------
# Staged rollout
# ---------------------------------------------------------------------------
# Design doc 08 and prompt 2.4 want Sandbox canary, then non-production
# Members, then production. This script deploys to ONE OU at a time by design:
# --ou members requires --confirm-production, and there is no flag that targets
# everything at once. The wave discipline is enforced by having to type it
# again.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

NAME=""; TEMPLATE=""; OU=""; REGIONS="${PLATFORM_HOME_REGION}"; PARAMS=()
MAX_CONCURRENT=1; FAILURE_TOLERANCE=0

usage() {
  cat <<'USAGE'
Usage: deploy-stackset.sh --name <stackset> --template <path> --ou <target>
                          [--regions r1,r2] [Key=Value ...]
                          [--max-concurrent N] [--dry-run] [--confirm-production]

  --ou              sandbox | members | security | infra
  --regions         Comma-separated. Defaults to the home region.
  --max-concurrent  Accounts updated in parallel. Default 1 — a bad baseline
                    reaching one account is recoverable; reaching forty at once
                    is an incident.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)               NAME="$2"; shift 2 ;;
    --template)           TEMPLATE="$2"; shift 2 ;;
    --ou)                 OU="$2"; shift 2 ;;
    --regions)            REGIONS="$2"; shift 2 ;;
    --max-concurrent)     MAX_CONCURRENT="$2"; shift 2 ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --confirm-production) CONFIRM_PRODUCTION=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *=*)                  PARAMS+=("$1"); shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${NAME}" ]]     || { usage; die "--name is required."; }
[[ -n "${TEMPLATE}" ]] || { usage; die "--template is required."; }
[[ -n "${OU}" ]]       || { usage; die "--ou is required."; }

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

case "${OU}" in
  sandbox)  TARGET_OU="$(get_param /org/ou/sandbox)" ;;
  members)  confirm_production; TARGET_OU="$(get_param /org/ou/members)" ;;
  security) TARGET_OU="$(get_param /org/ou/security)" ;;
  infra)    TARGET_OU="$(get_param /org/ou/infrastructure)" ;;
  *) die "Unknown --ou '${OU}'. Expected sandbox, members, security or infra." ;;
esac
[[ -n "${TARGET_OU}" && "${TARGET_OU}" != "None" ]] || die "Could not resolve the ${OU} OU."

hr
log "StackSet deploy"
log "  stackset : ${NAME}"
log "  template : ${TEMPLATE}"
log "  target   : ${OU} OU (${TARGET_OU})"
log "  regions  : ${REGIONS}"
log "  parallel : ${MAX_CONCURRENT} account(s), tolerance ${FAILURE_TOLERANCE}"
hr

cfn_validate "${REPO_ROOT}/${TEMPLATE}"

PARAM_ARGS=()
for p in "${PARAMS[@]}"; do
  PARAM_ARGS+=("ParameterKey=${p%%=*},ParameterValue=${p#*=}")
done

EXISTS="$(aws cloudformation describe-stack-set --stack-set-name "${NAME}" \
  --call-as SELF --query 'StackSet.StackSetId' --output text 2>/dev/null | no_cr || true)"

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  %s stack set %s\n' "${C_YELLOW}" "${C_RESET}" \
    "$([[ -n "${EXISTS}" ]] && echo update || echo create)" "${NAME}"
  printf '%s DRY%s  create stack instances in OU %s across %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${TARGET_OU}" "${REGIONS}"
  hr; ok "Dry run complete."; exit 0
fi

if [[ -z "${EXISTS}" ]]; then
  info "Creating stack set ${NAME}"
  aws cloudformation create-stack-set \
    --stack-set-name "${NAME}" \
    --template-body "file://$(win_path "${REPO_ROOT}/${TEMPLATE}")" \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
    --capabilities CAPABILITY_NAMED_IAM \
    --description "Platform baseline from ${TEMPLATE}" \
    ${PARAM_ARGS:+--parameters "${PARAM_ARGS[@]}"} \
    --query 'StackSetId' --output text | no_cr
  ok "stack set created"
else
  info "Updating stack set ${NAME}"
  aws cloudformation update-stack-set \
    --stack-set-name "${NAME}" \
    --template-body "file://$(win_path "${REPO_ROOT}/${TEMPLATE}")" \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
    --capabilities CAPABILITY_NAMED_IAM \
    ${PARAM_ARGS:+--parameters "${PARAM_ARGS[@]}"} \
    --operation-preferences "MaxConcurrentCount=${MAX_CONCURRENT},FailureToleranceCount=${FAILURE_TOLERANCE},RegionConcurrencyType=SEQUENTIAL" \
    --query 'OperationId' --output text | no_cr
  ok "stack set updated"
fi

# Instances are separate from the set. Creating the set defines the template;
# creating instances is what actually deploys anything.
info "Creating stack instances in ${OU}"
OP_ID="$(aws cloudformation create-stack-instances \
  --stack-set-name "${NAME}" \
  --deployment-targets "OrganizationalUnitIds=${TARGET_OU}" \
  --regions "$(printf '%s' "${REGIONS}" | tr ',' ' ')" \
  --operation-preferences "MaxConcurrentCount=${MAX_CONCURRENT},FailureToleranceCount=${FAILURE_TOLERANCE},RegionConcurrencyType=SEQUENTIAL" \
  --query 'OperationId' --output text 2>&1 | no_cr)" || true

if printf '%s' "${OP_ID}" | grep -qi 'error'; then
  printf '%s' "${OP_ID}" | sed 's/^/  /'
  warn "If this reports instances already exist, the set is already deployed there."
else
  log "  operation: ${OP_ID}"
  info "Waiting for the operation to complete"
  for _ in $(seq 1 60); do
    STATUS="$(aws cloudformation describe-stack-set-operation \
      --stack-set-name "${NAME}" --operation-id "${OP_ID}" \
      --query 'StackSetOperation.Status' --output text 2>/dev/null | no_cr)"
    case "${STATUS}" in
      SUCCEEDED) ok "operation SUCCEEDED"; break ;;
      FAILED|STOPPED) warn "operation ${STATUS}"; break ;;
      *) printf '  %s ...\n' "${STATUS}"; sleep 15 ;;
    esac
  done
fi

hr
log "Stack instances:"
aws cloudformation list-stack-instances --stack-set-name "${NAME}" \
  --query 'Summaries[].[Account,Region,Status,StatusReason]' --output text 2>/dev/null \
  | no_cr | cut -c1-140 | sed 's/^/  /'
hr
