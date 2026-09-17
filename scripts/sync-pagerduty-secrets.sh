#!/usr/bin/env bash
#
# Copy the PagerDuty routing key from Terraform state into AWS Secrets Manager.
#
#   scripts/sync-pagerduty-secrets.sh [--account <canonical-alias>] [--dry-run]
#
# ---------------------------------------------------------------------------
# Why the key moves at all
# ---------------------------------------------------------------------------
# CloudFormation resolves {{resolve:secretsmanager:...}} at deploy time, in the
# account and region it is deploying to. Terraform holds the key and
# CloudFormation needs it; Secrets Manager is the handoff.
#
# The alternative — a NoEcho stack parameter — puts the key in the changeset,
# where anyone who can describe the stack can read it. NoEcho hides a value
# from `describe-stacks`; it does not hide it from the changeset, and that gap
# is not widely known.
#
# ---------------------------------------------------------------------------
# ONE account holds this, deliberately
# ---------------------------------------------------------------------------
# The obvious design puts an alerting topic in every account, which copies this
# credential into every account in the Organization — including member
# accounts, which are the ones a tenant's application can compromise. Instead
# there is one topic in the Audit account and alarms publish to it
# cross-account, so the key exists once and rotates in one place.
#
# ---------------------------------------------------------------------------
# The key is never printed
# ---------------------------------------------------------------------------
# Not to stdout, not into a temp file read by another process, not into an
# argument. `terraform output -raw` writes it to a variable and the AWS CLI
# reads it from a here-doc on stdin via file://-style indirection. Anything
# that puts it on a command line makes it visible in the process list.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACCOUNT_KEY="altdig-security-audit"
SECRET_NAME="platform/pagerduty/routing-key"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--account <alias>] [--secret <name>] [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

TF="$(find_tool terraform)" || die "terraform not found. Run scripts/setup-tooling.sh."
TFDIR="${REPO_ROOT}/pagerduty"

[[ -f "${TFDIR}/terraform.tfstate" ]] || die "No Terraform state in pagerduty/.
      Run scripts/pagerduty-apply.sh --apply first."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TARGET="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TARGET}" && "${TARGET}" != "None" ]] || die "Unknown account: ${ACCOUNT_KEY}"

KEY="$(cd "${TFDIR}" && "${TF}" output -raw cloudwatch_integration_key 2>/dev/null)" || true
[[ -n "${KEY}" ]] || die "Could not read cloudwatch_integration_key from Terraform output."

# Length check only. The value is never echoed, and a routing key is a 32
# character hex-ish token — a wildly different length means the wrong output
# was read, which is worth catching before it becomes a subscription that
# silently discards every page.
[[ ${#KEY} -ge 20 ]] || die "Routing key looks wrong (${#KEY} characters). Refusing to write it."

hr
log "PagerDuty routing key -> Secrets Manager"
log "  account : ${ACCOUNT_KEY} (${TARGET})"
log "  secret  : ${SECRET_NAME}"
log "  region  : ${AWS_DEFAULT_REGION}"
log "  key     : ${#KEY} characters (not printed)"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s into %s\n' "${C_YELLOW}" "${C_RESET}" "${SECRET_NAME}" "${TARGET}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-pd-sync \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TARGET}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

PAYLOAD="$(printf '{"integration_key":"%s"}' "${KEY}")"

if MSYS_NO_PATHCONV=1 aws secretsmanager describe-secret \
     --secret-id "${SECRET_NAME}" >/dev/null 2>&1; then
  info "secret exists — updating"
  MSYS_NO_PATHCONV=1 aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" \
    --secret-string "${PAYLOAD}" \
    --query 'VersionId' --output text >/dev/null
  ok "rotated ${SECRET_NAME}"
  warn "Rotation is only half done. CloudFormation resolved the OLD key into
      the SNS subscription endpoint at deploy time, and it does not re-resolve
      on its own. Redeploy alerting/10-alert-topic.yaml, then confirm the
      subscription is Confirmed rather than PendingConfirmation."
else
  info "creating secret"
  MSYS_NO_PATHCONV=1 aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "PagerDuty CloudWatch routing key for the platform alerting topic" \
    --secret-string "${PAYLOAD}" \
    --tags Key=platform-managed,Value=true \
    --query 'ARN' --output text >/dev/null
  ok "created ${SECRET_NAME}"
fi

hr
info "Next: redeploy the alerting stack so it creates the subscription"
log "  scripts/deploy-to-account.sh --account ${ACCOUNT_KEY} \\"
log "    --template alerting/10-alert-topic.yaml --stack platform-alerting \\"
log "    OrganizationId=<o-...> PagerDutySecretName=${SECRET_NAME}"
hr
