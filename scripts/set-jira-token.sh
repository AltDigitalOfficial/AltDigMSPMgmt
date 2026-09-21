#!/usr/bin/env bash
#
# Store the Jira Cloud service account credential in Secrets Manager, in every
# account that reads it, in one run.
#
#   scripts/set-jira-token.sh                  # prompts, input hidden
#   ... | scripts/set-jira-token.sh --stdin    # token on stdin
#   scripts/set-jira-token.sh --propagate      # fan an existing value out
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
#   provisioning (12.3)    creates onboarding issues, obligations and the
#                          per-tenant JSM organization
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
#          approval or transition endpoint. jira_tenant.py checks every write
#          path against an allow-list of issue and organization endpoints.
#   Jira   this account MUST NOT be a member of any approver group. A
#          credential that can edit issues and also approve them can
#          manufacture the approval the quorum pipeline then verifies, and the
#          control is circular however the code is arranged.
#
# ---------------------------------------------------------------------------
# Every consuming account, in one run — B-110
# ---------------------------------------------------------------------------
# A secret is only useful in the account that reads it, and there are now two
# such accounts:
#
#   audit     reconciliation (8.2) and quorum (8.3), alongside the router and
#             containment, because that is where deployment events land and
#             where Security Hub is aggregated.
#   tooling   provisioning (12.3), because that is where `platform-registry`
#             lives and the saga reads the registry on every step.
#
# The obvious alternative — one copy in audit, read cross-account from tooling —
# is NOT available. The secret is encrypted under the AWS-managed
# `aws/secretsmanager` key, and AWS-managed keys cannot be shared across
# accounts. It would have to be re-created under a customer-managed key first.
#
# So there are two copies, and two copies of a credential is a rotation
# hazard: update one, miss the other, and the missed account keeps working on
# the old value until the old value is revoked — at which point it fails in a
# way that looks like an outage rather than like a half-finished rotation.
#
# This script removes that hazard by construction. One invocation writes ALL
# consuming accounts, then reads each one back and compares fingerprints. There
# is no supported way to rotate one and not the other, and if a write fails
# part way the script says exactly which accounts hold which value rather than
# reporting a generic failure.
#
# ---------------------------------------------------------------------------
# Never on a command line
# ---------------------------------------------------------------------------
# No --token flag, deliberately. An argument is visible in the process list to
# every user on the machine and lands in shell history. Input comes from a
# hidden prompt or from stdin.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

FROM_STDIN=0
PROPAGATE=0
SECRET_NAME="platform/jira/service-account"
EMAIL=""

# The consuming accounts, by SSM alias. Order matters only for --propagate,
# which sources the existing value from the first one that has it.
CONSUMER_KEYS="altdig-security-audit altdig-infra-tooling"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stdin)     FROM_STDIN=1; shift ;;
    --propagate) PROPAGATE=1; shift ;;
    --email)     EMAIL="$2"; shift 2 ;;
    --secret)    SECRET_NAME="$2"; shift 2 ;;
    # Overrides the whole list, not one entry. Comma or space separated.
    # Narrowing this is how a half-rotation gets done deliberately, so it is
    # deliberately awkward to do by accident.
    --account)   CONSUMER_KEYS="${2//,/ }"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--email <addr>] [--stdin] [--propagate] [--secret <name>] [--account <alias>[,<alias>]]\n' "$0"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# --- resolve the consuming accounts ----------------------------------------
#
# Resolved up front, all of them, before anything is written. An alias that
# does not resolve should stop the run rather than be discovered after the
# first account has already been rotated.

ACCOUNTS=""
for key in ${CONSUMER_KEYS}; do
  acct="$(get_param "/org/account/${key}")"
  [[ -n "${acct}" && "${acct}" != "None" ]] \
    || die "Account ${key} is not registered in SSM. Nothing has been written."
  ACCOUNTS="${ACCOUNTS}${key}:${acct} "
done

info "consuming accounts: $(printf '%s' "${ACCOUNTS}")"

# --- helpers ----------------------------------------------------------------
#
# Each runs in a subshell so the assumed-role credentials cannot leak into the
# next iteration. Getting that wrong would write the second account's secret
# using the first account's credentials, which fails confusingly or, worse,
# silently writes the same account twice.

in_account() (
  # in_account <account-id> <command...>
  local acct="$1"; shift
  local creds ak sk st
  creds="$(aws sts assume-role \
    --role-arn "arn:aws:iam::${acct}:role/OrganizationAccountAccessRole" \
    --role-session-name set-jira-token \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${creds}" ]] || return 3
  read -r ak sk st <<<"${creds}"
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${ak}" AWS_SECRET_ACCESS_KEY="${sk}" \
         AWS_SESSION_TOKEN="${st}"
  MSYS_NO_PATHCONV=1 "$@"
)

read_payload() {
  # Prints the stored secret string, or nothing. Used by --propagate and by
  # the fingerprint check; the value is never echoed to a terminal.
  in_account "$1" aws secretsmanager get-secret-value \
    --secret-id "${SECRET_NAME}" --query SecretString --output text 2>/dev/null \
    | no_cr || true
}

fingerprint() {
  # A short hash, so two accounts can be COMPARED without either value being
  # displayed. Twelve hex characters of sha256 identifies a mismatch without
  # being useful to anyone reading over a shoulder.
  python -c '
import hashlib, sys
raw = sys.stdin.buffer.read().strip()
print(hashlib.sha256(raw).hexdigest()[:12] if raw else "absent")
'
}

# --- obtain the payload -----------------------------------------------------

if [[ "${PROPAGATE}" == "1" ]]; then
  # Repair, not rotation. The credential already exists somewhere and one or
  # more accounts are missing it or hold an older value. Nothing is prompted,
  # because re-typing a token to fan it out is how a typo becomes a rotation
  # nobody meant to do.
  PAYLOAD=""
  SOURCE_KEY=""
  for entry in ${ACCOUNTS}; do
    key="${entry%%:*}"; acct="${entry##*:}"
    candidate="$(read_payload "${acct}")"
    if [[ -n "${candidate}" && "${candidate}" != "None" ]]; then
      PAYLOAD="${candidate}"; SOURCE_KEY="${key}"
      break
    fi
  done
  [[ -n "${PAYLOAD}" ]] \
    || die "--propagate found no existing credential in any consuming account. There is nothing to fan out; run without --propagate to store one."
  ok "propagating the value already held by ${SOURCE_KEY}"
else
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
fi

WANT="$(printf '%s' "${PAYLOAD}" | fingerprint)"

if [[ "${DRY_RUN}" == "1" ]]; then
  for entry in ${ACCOUNTS}; do
    key="${entry%%:*}"; acct="${entry##*:}"
    have="$(read_payload "${acct}" | fingerprint)"
    printf '%s DRY%s  would store %s in %s (%s): %s -> %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${SECRET_NAME}" "${key}" "${acct}" \
      "${have}" "${WANT}"
  done
  exit 0
fi

# --- write every account, then verify every account -------------------------
#
# Written first, verified second, and the verification is a READ BACK rather
# than the absence of an error from the write. An API that accepted a
# put-secret-value has told us it accepted a request; the fingerprint tells us
# what is actually stored.

WROTE=""
FAILED=""
for entry in ${ACCOUNTS}; do
  key="${entry%%:*}"; acct="${entry##*:}"
  if in_account "${acct}" aws secretsmanager describe-secret \
       --secret-id "${SECRET_NAME}" >/dev/null 2>&1; then
    if in_account "${acct}" aws secretsmanager put-secret-value \
         --secret-id "${SECRET_NAME}" --secret-string "${PAYLOAD}" \
         --query VersionId --output text >/dev/null 2>&1; then
      ok "rotated ${SECRET_NAME} in ${key} (${acct})"
      WROTE="${WROTE}${key} "
    else
      FAILED="${FAILED}${key} "
    fi
  else
    if in_account "${acct}" aws secretsmanager create-secret \
         --name "${SECRET_NAME}" --secret-string "${PAYLOAD}" \
         --description "Jira Cloud service account for change reconciliation (8.2), quorum verification (8.3) and tenant provisioning (12.3). Written by scripts/set-jira-token.sh." \
         --query ARN --output text >/dev/null 2>&1; then
      ok "stored ${SECRET_NAME} in ${key} (${acct})"
      WROTE="${WROTE}${key} "
    else
      FAILED="${FAILED}${key} "
    fi
  fi
done
unset PAYLOAD

printf '\n'
info "verifying by read-back"
MISMATCH=""
for entry in ${ACCOUNTS}; do
  key="${entry%%:*}"; acct="${entry##*:}"
  have="$(read_payload "${acct}" | fingerprint)"
  if [[ "${have}" == "${WANT}" ]]; then
    ok "${key} holds ${have}"
  else
    warn "${key} holds ${have}, expected ${WANT}"
    MISMATCH="${MISMATCH}${key} "
  fi
done

if [[ -n "${MISMATCH}" ]]; then
  printf '\n'
  # Named explicitly, because "rotation failed" is the one message that would
  # leave somebody guessing which half of the estate is now stale. The accounts
  # that DID take the new value are named too — they are the ones that will
  # break when the old token is revoked.
  warn "PARTIAL ROTATION. Consuming accounts do not agree."
  warn "  updated:  ${WROTE:-none}"
  warn "  stale:    ${MISMATCH}"
  [[ -n "${FAILED}" ]] && warn "  failed:   ${FAILED}"
  die "Re-run to finish. Do NOT revoke the old Jira token until every account above reports the same fingerprint."
fi

ok "all consuming accounts hold the same credential (${WANT})"
