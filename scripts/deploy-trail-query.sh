#!/usr/bin/env bash
#
# Deploy the organization trail's query layer (security/50-trail-query.yaml)
# to Log Archive, with the account list derived from Organizations.
#
#   scripts/deploy-trail-query.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# Why this is its own script
# ---------------------------------------------------------------------------
# It used to be step 4 of deploy-audit-access.sh, which grants an audit
# engagement and refuses to run without an end date. So the table only
# existed if somebody granted an audit, and it never did: ADR-008 recorded it
# as "not deployed and not proved" from 2026-10-08 until 2026-11-01. The
# table has consumers that are not auditors (ADR-004's digest gatherer, and
# anyone answering "who did this, when" past CloudTrail's 90-day history),
# so its deploy cannot wait on an engagement.
#
# deploy-audit-access.sh still calls this, so a grant keeps the table current.
#
# ---------------------------------------------------------------------------
# Why it runs whenever an account is added
# ---------------------------------------------------------------------------
# Partitions are projected from an account list fixed at deploy time
# (ADR-008). An account added later is invisible to every query until this
# runs again, and nothing about a query says an account is missing: the
# result is just quieter. create-platform-account.sh and vest-client.sh call
# this at the end of their runs for that reason.
#
# Every account, any status: a closed account keeps its trail history.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

QUERY_ACCOUNT="altdig-security-logarchive"
QUERY_STACK="platform-trail-query"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) printf 'Usage: %s [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

ORG_ID="$(aws organizations describe-organization \
  --query 'Organization.Id' --output text | no_cr)"
[[ "${ORG_ID}" =~ ^o-[a-z0-9]+$ ]] || die "Could not read the Organization id."

ALL_ACCOUNTS="$(aws organizations list-accounts \
  --query 'Accounts[].Id' --output text | no_cr | tr '\t' ',' | tr -s ', ' ',')"
ALL_ACCOUNTS="${ALL_ACCOUNTS%,}"
[[ -n "${ALL_ACCOUNTS}" ]] || die "Could not list Organization accounts."

info "Trail query: $(printf '%s' "${ALL_ACCOUNTS}" | tr ',' '\n' | wc -l | tr -d ' ') accounts projected"

DEPLOY_ARGS=(--account "${QUERY_ACCOUNT}" --template security/50-trail-query.yaml
  --stack "${QUERY_STACK}" "OrganizationId=${ORG_ID}" "AccountIds=${ALL_ACCOUNTS}")
[[ "${DRY_RUN}" == "1" ]] && DEPLOY_ARGS+=(--dry-run)
"${REPO_ROOT}/scripts/deploy-to-account.sh" "${DEPLOY_ARGS[@]}" \
  || die "Could not deploy ${QUERY_STACK} to ${QUERY_ACCOUNT}."
