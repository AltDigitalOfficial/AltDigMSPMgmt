#!/usr/bin/env bash
#
# Record AltDigital's own accounts in the platform registry.
#
#   scripts/register-platform-accounts.sh [--dry-run]
#
# The registry is the spine — design 13's phrase — and it had no ACCOUNT
# records at all. The six platform accounts were created before the vesting
# flow existed and live only in SSM under /platform/org/account/, so anything
# deriving from the registry (Cost Categories, prompt 9.2) saw an empty estate.
#
# These are `kind: shared`, not tenant. design 10:
#
#     Log Archive, Audit, Forensics, Platform Tooling and Shared Services
#     accounts generate real cost that nobody pays a flat fee for.
#     [...] Position: absorbed into the percentage uplift.
#
# Absorbed is a billing decision, not a reason to leave them out of the
# registry. Left out, they would be invisible to every control that iterates
# accounts — which is B-064 exactly, where a hand-maintained list missed
# Forensics and nothing said so.

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

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
ACCOUNTS="$(aws ssm get-parameters-by-path --path /platform/org/account/ \
  --query 'Parameters[].[Name,Value]' --output text 2>/dev/null | no_cr)"
[[ -n "${ACCOUNTS}" ]] || die "No accounts under /platform/org/account/."

# The management account, which is NOT in that path and never will be.
#
# CLAUDE.md keeps it holding "the Organization and nothing else", so it is not
# a provisioned member and has no /platform/org/account/ entry. It still costs
# money — Organizations, CloudTrail, the consolidated bill — and the Cost
# Category sync correctly reported it as `unregistered` on its first run.
#
# `unregistered` is the right thing for that check to SAY and the wrong place
# for the spend to live. Appended explicitly, so its absence from the registry
# path reads as the reason rather than as an oversight.
ACCOUNTS="${ACCOUNTS}
/platform/org/account/altdig-management	${PLATFORM_MGMT_ACCOUNT_ID}"

# Environment per account, from the tier map the alerting layer already
# maintains in Audit. Derived rather than restated here: two lists of which
# account is production is one list too many, and the one nobody updates is
# always the one being read.
AUDIT="$(get_param /org/account/altdig-security-audit)"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name read-tiers \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into Audit to read the tier map."
read -r AK SK ST <<<"${CREDS}"
TIERS="$(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  MSYS_NO_PATHCONV=1 aws ssm get-parameter \
    --name /platform/routing/account-tiers \
    --query Parameter.Value --output text 2>/dev/null | no_cr
)"
[[ -n "${TIERS}" ]] || die "Could not read /platform/routing/account-tiers."

PY="$(command -v python || command -v python3)" || die "python not found."

COUNT=0
while read -r NAME ACCOUNT_ID; do
  [[ "${ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] || continue
  ALIAS="${NAME##*/}"
  TIER="$("${PY}" -c "
import json,sys
print((json.loads(sys.argv[1]) or {}).get(sys.argv[2], 'prod'))
" "${TIERS}" "${ACCOUNT_ID}")"

  # The registry's environment vocabulary, from the tier.
  case "${TIER}" in
    dev)  ENV_VALUE="dev" ;;
    test) ENV_VALUE="test" ;;
    uat)  ENV_VALUE="uat" ;;
    *)    ENV_VALUE="prod" ;;
  esac

  # Every platform account is managed by AltDigital and in scope for its own
  # SOC 2 — design 15 puts infrastructure operations, platform controls, the
  # evidence pipeline, incident response and change management in the
  # reference report. That is not a per-account judgement to make here.
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  %-32s %s  kind=shared env=%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${ALIAS}" "${ACCOUNT_ID}" "${ENV_VALUE}"
  else
    "${REPO_ROOT}/scripts/registry.sh" put-account \
      --account-id "${ACCOUNT_ID}" \
      --kind shared \
      --partner altdigital \
      --slug platform \
      --app "${ALIAS#altdig-}" \
      --env "${ENV_VALUE}" \
      --service-model managed \
      --compliance soc2 \
      --state active >/dev/null \
      || die "Could not record ${ALIAS} (${ACCOUNT_ID})."
    ok "${ALIAS} (${ACCOUNT_ID}) recorded: shared / ${ENV_VALUE}"
  fi
  COUNT=$((COUNT + 1))
done <<<"${ACCOUNTS}"

hr
log "Platform accounts recorded: ${COUNT}"
log "  kind          : shared — cost absorbed into the uplift, not allocated"
log "  service model : managed"
log "  compliance    : soc2 (AltDigital's own reference report, design 15)"
hr
info "Re-run after creating a platform account. A tenant account is recorded"
log "  by the vesting flow, not by this."
hr
