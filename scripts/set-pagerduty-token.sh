#!/usr/bin/env bash
#
# Store the PagerDuty API token in Secrets Manager.
#
#   scripts/set-pagerduty-token.sh          # prompts, input hidden
#   ... | scripts/set-pagerduty-token.sh --stdin
#
# ---------------------------------------------------------------------------
# This is the most dangerous PagerDuty credential the platform holds
# ---------------------------------------------------------------------------
# A ROUTING key can only raise an incident on one service. This token can
# create, modify and delete services, escalation policies and schedules — it
# can silently redirect or switch off every page the platform sends, and the
# absence of alerts is the hardest failure to notice.
#
# Which is the argument FOR putting it here rather than against. In an
# environment variable it exists on one laptop, nothing records its use, and
# rotating it means remembering where it was typed. In Secrets Manager every
# retrieval is a CloudTrail event in an archive nobody can alter.
#
# Be clear about what this does NOT buy: use-time secrecy. Terraform reads the
# token from its environment, so fetching it puts it in a process environment
# either way. The gain is at rest, in rotation, and in audit.
#
# ---------------------------------------------------------------------------
# Never on a command line
# ---------------------------------------------------------------------------
# No --token flag, deliberately. An argument is visible in the process list to
# every user on the machine and lands in shell history. Input comes from a
# hidden prompt or from stdin.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

FROM_STDIN=0
SECRET_NAME="platform/pagerduty/api-token"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stdin)  FROM_STDIN=1; shift ;;
    --secret) SECRET_NAME="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--stdin] [--secret <name>]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

if [[ ${FROM_STDIN} -eq 1 ]]; then
  IFS= read -r TOKEN
else
  # -s suppresses echo. The trailing newline is printed separately because -s
  # swallows the one the user typed, and without it the next output line
  # overwrites the prompt.
  printf 'PagerDuty API token (input hidden): '
  IFS= read -rs TOKEN
  printf '\n'
fi

[[ -n "${TOKEN}" ]] || die "No token supplied."

# Length check only; the value is never echoed. PagerDuty general access keys
# are 20 or 32 characters depending on vintage, so this catches a paste that
# picked up nothing rather than validating the token.
[[ ${#TOKEN} -ge 16 ]] || die "Token looks wrong (${#TOKEN} characters). Refusing to store it."

hr
log "PagerDuty API token -> Secrets Manager"
log "  account : altdig-infra-tooling (${TOOLING})"
log "  secret  : ${SECRET_NAME}"
log "  token   : ${#TOKEN} characters (not printed)"
hr

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-pd-token \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

PAYLOAD="$(printf '{"api_token":"%s"}' "${TOKEN}")"

if MSYS_NO_PATHCONV=1 aws secretsmanager describe-secret \
     --secret-id "${SECRET_NAME}" >/dev/null 2>&1; then
  MSYS_NO_PATHCONV=1 aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" --secret-string "${PAYLOAD}" \
    --query 'VersionId' --output text >/dev/null
  ok "rotated ${SECRET_NAME}"
  log ""
  log "Nothing needs redeploying. Unlike the ROUTING keys — which CloudFormation"
  log "resolves into an SNS subscription endpoint at deploy time and does not"
  log "re-resolve — this token is read fresh on every terraform run."
else
  MSYS_NO_PATHCONV=1 aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "PagerDuty REST API token used by terraform in pagerduty/. General Access key, not a user token (open item B10)." \
    --secret-string "${PAYLOAD}" \
    --tags Key=platform-managed,Value=true \
    --query 'ARN' --output text >/dev/null
  ok "created ${SECRET_NAME}"
fi

hr
info "scripts/pagerduty-apply.sh now finds it automatically."
log "  PAGERDUTY_TOKEN in the environment still wins, for the case where you"
log "  need to run as a different identity without rewriting the secret."
hr
