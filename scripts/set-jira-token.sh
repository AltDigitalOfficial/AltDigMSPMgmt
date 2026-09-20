#!/usr/bin/env bash
#
# Store the Jira Cloud service account credential in Secrets Manager.
#
#   scripts/set-jira-token.sh                  # prompts, input hidden
#   ... | scripts/set-jira-token.sh --stdin    # token on stdin
#
# ---------------------------------------------------------------------------
# Why the email lives in the secret and not in config/platform.env
# ---------------------------------------------------------------------------
# Jira Cloud's REST API authenticates with HTTP Basic using
# `<account-email>:<api-token>`. Both halves are required, and they rotate
# together — a token reissued for a different service account is useless
# against the old email.
#
# Keeping them in one secret has a second benefit: the email is a direct
# address for a person or a platform identity, and CLAUDE.md keeps those out of
# the repository. Secrets Manager is where it belongs anyway.
#
# ---------------------------------------------------------------------------
# What this credential can do, which is a lot
# ---------------------------------------------------------------------------
# This credential both reads and WRITES, and the write half is not optional.
#
#   reconciliation (8.2)   reads issues
#   quorum (8.3)           reads issues, changelogs and approvals
#   the filer (8.2)        raises change requests and edits them
#
# An earlier version of this header said to scope the account read-only. That
# was true when only reconciliation used it and is now wrong: the filer raises
# a JSM request and then edits it, because Labels and Approver groups are not
# on the request type's form and both are load-bearing. In JSM terms that means
# AGENT access to the change project, not customer access - a customer can
# raise a request and cannot edit one.
#
# So the circularity the old text worried about cannot be handled by scoping.
# It is handled in code and in Jira instead:
#
#   code   jira.py has no write method and publish-change.sh refuses to
#          package it if one appears. filer.py may write but may not call an
#          approval or transition endpoint.
#   Jira   this account MUST NOT be a member of any approver group. A
#          credential that can edit issues and also approve them can
#          manufacture the approval the quorum pipeline then verifies, and the
#          control is circular however the code is arranged.
#
# ---------------------------------------------------------------------------
# Never on a command line
# ---------------------------------------------------------------------------
# No --token flag, deliberately. An argument is visible in the process list to
# every user on the machine and lands in shell history. Input comes from a
# hidden prompt or from stdin.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

FROM_STDIN=0
SECRET_NAME="platform/jira/service-account"
EMAIL=""
# AUDIT, not Tooling.
#
# A secret is only useful in the account that reads it. The reconciliation
# engine (8.2) and the quorum pipeline (8.3) both run in Audit, alongside the
# router and containment, because that is where deployment events land and
# where Security Hub is aggregated. Storing this in Tooling — the first version
# of this script did — would have put the credential somewhere nothing can
# reach it, and the failure would have surfaced as an AccessDenied on
# GetSecretValue naming a secret that does exist, just not there.
#
# Same arrangement as the PagerDuty routing key, which
# scripts/sync-pagerduty-secrets.sh places into the consuming account for
# exactly this reason.
ACCOUNT_KEY="altdig-security-audit"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stdin)   FROM_STDIN=1; shift ;;
    --email)   EMAIL="$2"; shift 2 ;;
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--email <addr>] [--stdin] [--secret <name>] [--account <alias>]\n' "$0"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TOOLING="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Account ${ACCOUNT_KEY} is not registered in SSM."

if [[ -z "${EMAIL}" ]]; then
  # The email is not secret in the way the token is, so it is prompted
  # normally. It is still not accepted as a positional argument, because a
  # mistyped positional would be stored silently.
  printf 'Jira service account email: '
  read -r EMAIL
fi
[[ -n "${EMAIL}" ]] || die "No email supplied."
[[ "${EMAIL}" == *"@"* ]] || die "That does not look like an email address."

if [[ "${FROM_STDIN}" == "1" ]]; then
  read -r TOKEN
else
  printf 'Jira API token (input hidden): '
  # -s so it is never echoed to the terminal or captured by a screen recorder.
  read -rs TOKEN
  printf '\n'
fi
[[ -n "${TOKEN}" ]] || die "No token supplied."

# Length check only. The value is never printed, never logged, and never
# interpolated into a message — an error that quotes a credential back is how
# credentials end up in terminal scrollback and support tickets.
if [[ ${#TOKEN} -lt 20 ]]; then
  warn "That token is shorter than any Jira Cloud API token observed so far."
  warn "Storing it anyway; verify with scripts/jira-check.sh."
fi

PAYLOAD="$(EMAIL="${EMAIL}" TOKEN="${TOKEN}" python -c '
import json, os
print(json.dumps({"email": os.environ["EMAIL"],
                  "api_token": os.environ["TOKEN"]}))
')"
unset TOKEN

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would store %s in %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${SECRET_NAME}" "${TOOLING}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name set-jira-token \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

if MSYS_NO_PATHCONV=1 aws secretsmanager describe-secret \
     --secret-id "${SECRET_NAME}" >/dev/null 2>&1; then
  MSYS_NO_PATHCONV=1 aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" --secret-string "${PAYLOAD}" \
    --query VersionId --output text >/dev/null
  ok "rotated ${SECRET_NAME} in ${TOOLING}"
else
  MSYS_NO_PATHCONV=1 aws secretsmanager create-secret \
    --name "${SECRET_NAME}" --secret-string "${PAYLOAD}" \
    --description "Jira Cloud service account for change reconciliation (8.2) and quorum verification (8.3). Written by scripts/set-jira-token.sh." \
    --query ARN --output text >/dev/null
  ok "stored ${SECRET_NAME} in ${TOOLING}"
fi
unset PAYLOAD

hr
log "Stored as JSON: {\"email\": ..., \"api_token\": ...}"
log "Rotating: re-run this script. The old version is retained by Secrets"
log "Manager, so a bad rotation is recoverable."
hr
