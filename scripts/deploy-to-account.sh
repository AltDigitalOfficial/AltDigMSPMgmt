#!/usr/bin/env bash
#
# Deploy a CloudFormation stack into a platform account by assuming into it.
#
#   scripts/deploy-to-account.sh --account altdig-security-logarchive \
#     --template security/10-log-archive.yaml --stack platform-log-archive \
#     RetentionDays=2190 [--dry-run]
#
# For PLATFORM accounts only. Member accounts get their configuration from the
# baseline StackSet, not from one-off stacks — that is the whole point of a
# baseline, and a member account carrying a hand-deployed stack is drift by
# design principle P9.
#
# Platform accounts are different: there are seven of them, each distinct, each
# deployed once. A StackSet for a population of one is ceremony.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACCOUNT_KEY=""; TEMPLATE=""; STACK=""; PARAMS=(); REGION=""

usage() {
  cat <<'USAGE'
Usage: deploy-to-account.sh --account <canonical-alias> --template <path>
                            --stack <name> [Key=Value ...] [--dry-run]

  --account   Canonical platform account name, e.g. altdig-security-logarchive.
              Resolved via /platform/org/account/<name>.
  --template  Template path, relative to the repository root.
  --stack     Stack name to create or update.
  --region    Target region. Defaults to the platform home region. Needed for
              replica buckets, which must live in a different region than the
              sources they protect — a CloudFormation stack is regional, so a
              cross-region pair is two stacks.
  --dry-run   Produce a changeset without executing it.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)  ACCOUNT_KEY="$2"; shift 2 ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    --stack)    STACK="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *=*)        PARAMS+=("$1"); shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${ACCOUNT_KEY}" ]] || { usage; die "--account is required."; }
[[ -n "${TEMPLATE}" ]]    || { usage; die "--template is required."; }
[[ -n "${STACK}" ]]       || { usage; die "--stack is required."; }

# SSM Parameter Store is REGIONAL. The platform registry lives in the home
# region, so the account lookup must happen there regardless of where the stack
# is being deployed — otherwise deploying to us-west-2 searches for a parameter
# that only exists in us-east-2 and reports the account as unregistered.
export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TARGET="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TARGET}" && "${TARGET}" != "None" ]] \
  || die "No account registered at ${PLATFORM_SSM_PREFIX}/org/account/${ACCOUNT_KEY}."

# Only now switch to the deployment region.
export AWS_DEFAULT_REGION="${REGION:-${PLATFORM_HOME_REGION}}"

[[ "${TARGET}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "Refusing to deploy a workload stack into the management account.
      Design doc 02: it holds the Organization and nothing else."

hr
log "Cross-account deploy"
log "  account  : ${ACCOUNT_KEY} (${TARGET})"
log "  template : ${TEMPLATE}"
log "  stack    : ${STACK}"
log "  region   : ${AWS_DEFAULT_REGION}"
hr

# Validate from the management account before assuming — a template that fails
# validation should not cost an assume-role round trip, and the error is
# clearer here.
cfn_validate "${REPO_ROOT}/${TEMPLATE}"

info "Assuming OrganizationAccountAccessRole in ${TARGET}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-deploy \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume OrganizationAccountAccessRole in ${TARGET}."
read -r AK SK ST <<<"${CREDS}"

args=(
  cloudformation deploy
  --stack-name "${STACK}"
  --template-file "$(win_path "${REPO_ROOT}/${TEMPLATE}")"
  --capabilities CAPABILITY_NAMED_IAM
  --no-fail-on-empty-changeset
  --tags platform-managed=true
)
[[ ${#PARAMS[@]} -gt 0 ]] && args+=(--parameter-overrides "${PARAMS[@]}")

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  changeset only\n' "${C_YELLOW}" "${C_RESET}"
  AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
    aws "${args[@]}" --no-execute-changeset || true
  exit 0
fi

info "Deploying ${STACK}"
AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST aws "${args[@]}"
ok "stack ${STACK} deployed"

hr
AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
  aws cloudformation describe-stacks --stack-name "${STACK}" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text 2>/dev/null \
  | no_cr | sed 's/^/  /'
hr
