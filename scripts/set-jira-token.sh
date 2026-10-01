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
TENANT=""
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
    # A tenant's OWN Atlassian site (questionnaire change.ticketing): its
    # read-only service account, stored under the conventional name that
    # sync-tenant-facts.sh publishes, in Audit only -- change reconciliation is
    # the one consumer. <partner>-<client>, e.g. oeight-oeight.
    --tenant)    TENANT="$2"; shift 2 ;;
    # Overrides the whole list, not one entry. Comma or space separated.
    # Narrowing this is how a half-rotation gets done deliberately, so it is
    # deliberately awkward to do by accident.
    --account)   CONSUMER_KEYS="${2//,/ }"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--email <addr>] [--stdin] [--propagate] [--secret <name>] [--account <alias>[,<alias>]] [--tenant <partner>-<client>]\n' "$0"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

SECRET_PURPOSE="Jira Cloud service account for change reconciliation (8.2), quorum verification (8.3) and tenant provisioning (12.3). Written by scripts/set-jira-token.sh."
if [[ -n "${TENANT:-}" ]]; then
  [[ "${TENANT}" =~ ^[a-z0-9][a-z0-9-]*-[a-z0-9][a-z0-9-]*$ ]] \
    || die "--tenant must be <partner>-<client>, e.g. oeight-oeight."
  SECRET_NAME="platform/jira/tenant/${TENANT}"
  CONSUMER_KEYS="altdig-security-audit"
  SECRET_PURPOSE="Read-only Jira service account on tenant ${TENANT}'s own Atlassian site, for change reconciliation (8.2). Written by scripts/set-jira-token.sh --tenant."
fi

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# --- resolve the consuming accounts ----------------------------------------

mapfile -t ACCOUNTS < <(resolve_accounts ${CONSUMER_KEYS})
info "consuming accounts: ${ACCOUNTS[*]}"

# --- obtain the payload -----------------------------------------------------

if [[ "${PROPAGATE}" == "1" ]]; then
  # Repair, not rotation. The credential already exists somewhere and one or
  # more accounts are missing it or hold an older value. Nothing is prompted,
  # because re-typing a token to fan it out is how a typo becomes a rotation
  # nobody meant to do.
  PAYLOAD=""
  for entry in "${ACCOUNTS[@]}"; do
    candidate="$(read_secret "${entry##*:}" "${SECRET_NAME}")"
    if [[ -n "${candidate}" && "${candidate}" != "None" ]]; then
      PAYLOAD="${candidate}"
      ok "propagating the value already held by ${entry%%:*}"
      break
    fi
  done
  [[ -n "${PAYLOAD}" ]] \
    || die "--propagate found no existing credential in any consuming account. There is nothing to fan out; run without --propagate to store one."
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

store_secret_everywhere   "${SECRET_NAME}"   "${SECRET_PURPOSE}"   "${PAYLOAD}"   "${ACCOUNTS[@]}"
unset PAYLOAD
