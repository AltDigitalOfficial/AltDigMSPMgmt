#!/usr/bin/env bash
#
# Generate Cost Categories from the platform registry and apply them.
#
#   scripts/sync-cost-categories.sh [--dry-run]
#
# design/10-commercial-model.md, "Cost visibility and allocation":
#
#     Cost Categories map account IDs to tenant, application, environment,
#     service model and compliance scope -- without requiring a single resource
#     tag.
#
# Cost Categories are a BILLING construct and live in the management account,
# which is the payer. That is the one thing in this platform that legitimately
# has to be there: CLAUDE.md keeps the management account nearly empty, and
# this is not a workload, it is a view over the consolidated bill.
#
# Run after vesting an account. Until this runs, a new account's spend appears
# as `unregistered` rather than under its tenant -- visible, and wrong in the
# direction that gets noticed.

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
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# --- the registry, from Tooling ------------------------------------------
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-cost-categories \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into Platform Tooling."
read -r AK SK ST <<<"${CREDS}"
(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  MSYS_NO_PATHCONV=1 aws dynamodb scan --table-name platform-registry \
    --query 'Items' --output json
) > "${WORK}/registry.json" 2>/dev/null \
  || die "Could not read the platform registry."

# --- every account the Organization actually contains ---------------------
#
# From Organizations, not from the registry. That is the whole point of the
# comparison: the registry says what SHOULD exist and Organizations says what
# does, and the gap between them is the finding. Reading both from the same
# place would make the check vacuous.
aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].Id' --output json \
  > "${WORK}/org.json" 2>/dev/null \
  || die "Could not list Organization accounts."

"${PY}" -c "
import json, sys
reg = json.load(open(sys.argv[1], encoding='utf-8'))
org = json.load(open(sys.argv[2], encoding='utf-8'))
json.dump({'registry': reg, 'org_accounts': org}, open(sys.argv[3], 'w', encoding='utf-8'))
" "$(win_path "${WORK}/registry.json")" "$(win_path "${WORK}/org.json")" \
  "$(win_path "${WORK}/input.json")"

"${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/cost_categories.py")" \
  < "${WORK}/input.json" > "${WORK}/out.json" \
  || die "Could not generate the Cost Category definitions."

UNREGISTERED="$("${PY}" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print(' '.join(d['unregistered']))
" "$(win_path "${WORK}/out.json")")"

NAMES="$("${PY}" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print(' '.join(c['Name'] for c in d['definitions']))
" "$(win_path "${WORK}/out.json")")"

hr
log "Cost Categories"
log "  account    : management (${PLATFORM_MGMT_ACCOUNT_ID})"
log "  categories : ${NAMES}"
hr

if [[ -n "${UNREGISTERED}" ]]; then
  # Loud, and not fatal. An unregistered account is a real finding -- newly
  # vested and not yet recorded, or something nobody meant to create -- but
  # refusing to sync would leave EVERY category stale because of one account,
  # which trades a visible problem for an invisible one.
  warn "Accounts in the Organization with no registry record:"
  for A in ${UNREGISTERED}; do warn "    ${A}"; done
  warn "Their spend will appear under 'unregistered' rather than under a"
  warn "tenant. Record them with scripts/registry.sh put-account, or"
  warn "scripts/register-platform-accounts.sh for AltDigital's own."
  hr
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  "${PY}" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
for c in d['definitions']:
    print('  %-18s %d rule(s): %s' % (
        c['Name'], len(c['Rules']),
        ', '.join(r['Value'] for r in c['Rules'])))
" "$(win_path "${WORK}/out.json")"
  printf '%s DRY%s  would create or update %s definition(s)\n' \
    "${C_YELLOW}" "${C_RESET}" "$(echo "${NAMES}" | wc -w | tr -d ' ')"
  exit 0
fi

EXISTING="$(aws ce list-cost-category-definitions \
  --query 'CostCategoryReferences[].[Name,CostCategoryArn]' \
  --output text 2>/dev/null | no_cr)"

for NAME in ${NAMES}; do
  "${PY}" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
c=[x for x in d['definitions'] if x['Name']==sys.argv[2]][0]
json.dump(c['Rules'], open(sys.argv[3],'w',encoding='utf-8'))
" "$(win_path "${WORK}/out.json")" "${NAME}" "$(win_path "${WORK}/rules.json")"

  ARN="$(printf '%s\n' "${EXISTING}" | awk -v n="${NAME}" '$1==n {print $2}')"
  if [[ -n "${ARN}" ]]; then
    # UPDATE, never delete-and-recreate.
    #
    # A Cost Category's history is attached to its ARN. Recreating it starts a
    # new one from today and orphans every closed month, so a report run over
    # last quarter would return nothing and look like a quarter with no spend.
    aws ce update-cost-category-definition \
      --cost-category-arn "${ARN}" \
      --rule-version CostCategoryExpression.v1 \
      --rules "file://$(win_path "${WORK}/rules.json")" >/dev/null \
      || die "Could not update Cost Category ${NAME}."
    ok "updated ${NAME}"
  else
    aws ce create-cost-category-definition \
      --name "${NAME}" \
      --rule-version CostCategoryExpression.v1 \
      --rules "file://$(win_path "${WORK}/rules.json")" >/dev/null \
      || die "Could not create Cost Category ${NAME}."
    ok "created ${NAME}"
  fi
done

hr
info "Cost Explorer takes up to 24 hours to backfill a new category."
log "  Until then a grouped report shows the dimension with no values, which"
log "  is AWS populating it rather than the platform having failed."
hr
