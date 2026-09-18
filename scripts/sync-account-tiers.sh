#!/usr/bin/env bash
#
# Publish the account -> environment tier map the routing layer reads.
#
#   scripts/sync-account-tiers.sh [--dry-run]
#
# Why the router needs this
# -------------------------
# A Security Hub or GuardDuty finding carries no environment tier, and neither
# does a raw CloudTrail event forwarded by one of 06a's event rules. The router
# has to decide whether a CRITICAL finding pages, and that answer is different
# in a sandbox and in production.
#
# Without the map it assumed "prod" for everything. Findings from the canary
# account were recorded with environment_tier null and routed as production;
# only their MEDIUM severity stopped them paging. A CRITICAL finding in a
# sandbox would have woken somebody at 3am for a dev box.
#
# Where the tier comes from
# -------------------------
# The DEPLOYED stack sets, not the account name.
#
# Account names end in a tier character by convention (-d, -t, -u, -p), and
# reading that would have been simpler and wrong in both directions: the
# platform's own accounts have no tier suffix at all, and the canary account is
# named altdig-infra-canary while its baseline is deployed with
# EnvironmentTier=dev. The name says what an account is called; the stack set
# parameter says how it is actually being monitored, which is the question the
# router is asking.
#
# Reading it from the stack sets also means the map cannot claim a tier for an
# account the platform has not actually instrumented.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

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

PY="$(command -v python || command -v python3)" || die "python not found."
AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

PARAM_NAME="/platform/routing/account-tiers"

# Every stack set that carries an EnvironmentTier. Each one's instances are the
# accounts at that tier. Listed explicitly rather than discovered, because a
# stack set that happens to have an EnvironmentTier parameter is not necessarily
# authoritative about monitoring.
STACKSETS=(platform-instrumentation platform-response)

info "Reading tiers from the deployed stack sets"
MAP="{}"
for ss in "${STACKSETS[@]}"; do
  TIER="$(aws cloudformation describe-stack-set --stack-set-name "${ss}" \
    --call-as SELF \
    --query "StackSet.Parameters[?ParameterKey=='EnvironmentTier'].ParameterValue | [0]" \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${TIER}" && "${TIER}" != "None" ]] || { skip "${ss}: no EnvironmentTier"; continue; }

  ACCOUNTS="$(aws cloudformation list-stack-instances --stack-set-name "${ss}" \
    --call-as SELF --query 'Summaries[].Account' --output text 2>/dev/null | no_cr)" || true
  [[ -n "${ACCOUNTS}" ]] || { skip "${ss}: no instances"; continue; }

  # shellcheck disable=SC2086
  MAP="$("${PY}" -c "
import json,sys
m = json.loads(sys.argv[1])
for a in sys.argv[3].split():
    m[a] = sys.argv[2]
print(json.dumps(m, sort_keys=True))
" "${MAP}" "${TIER}" "${ACCOUNTS}")"
  ok "${ss}: tier ${TIER} over $(printf '%s\n' ${ACCOUNTS} | wc -l | tr -d ' ') account(s)"
done

# The platform's own accounts. They run production infrastructure regardless of
# what tier the tenants they serve are at, so a finding in one of them pages.
#
# Listed by name through SSM rather than hardcoded, per the CLAUDE.md rule that
# nothing hardcodes an account id.
# Named exactly as registered. `altdig-log-archive` was wrong here first time
# and the loop skipped it without a word, which would have left the log archive
# account absent from the map and therefore treated as prod — the right answer
# by luck, from a bug. A missing name is now loud.
for name in altdig-security-audit altdig-security-logarchive altdig-infra-tooling; do
  ID="$(get_param "/org/account/${name}")" || true
  if [[ -z "${ID}" || "${ID}" == "None" ]]; then
    warn "${name} is not registered in SSM; it will be absent from the map and"
    log  "      therefore treated as prod by the router, which pages."
    continue
  fi
  MAP="$("${PY}" -c "
import json,sys
m = json.loads(sys.argv[1]); m[sys.argv[2]] = 'prod'
print(json.dumps(m, sort_keys=True))
" "${MAP}" "${ID}")"
done

hr
log "Account tier map"
"${PY}" -c "
import json,sys
m = json.loads(sys.argv[1])
for a, t in sorted(m.items(), key=lambda kv: (kv[1], kv[0])):
    print(f'  {a}  {t}')
print(f'  {len(m)} account(s)')
" "${MAP}"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s in %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-account-tiers \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account (${AUDIT})."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

MSYS_NO_PATHCONV=1 aws ssm put-parameter \
  --name "${PARAM_NAME}" \
  --type String \
  --overwrite \
  --value "${MAP}" \
  --description "Account to environment tier, for the routing layer. Derived from deployed stack set parameters by scripts/sync-account-tiers.sh. An account absent from this map is treated as prod, which pages." \
  --query 'Version' --output text >/dev/null
ok "wrote ${PARAM_NAME} in ${AUDIT}"

hr
info "The router caches this for the life of its execution environment, so a"
log "change takes effect within a few minutes rather than instantly. Re-run"
log "this whenever an account is vested or a tier changes."
hr
