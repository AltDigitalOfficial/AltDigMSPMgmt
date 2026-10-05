#!/usr/bin/env bash
#
# Publish which accounts are HIPAA-scoped, for the eligibility sweep.
#
#   scripts/sync-hipaa-accounts.sh [--dry-run]
#
# Writes /platform/eligibility/hipaa-accounts in Audit:
#
#   { "<account id>": { "environment": "dev", "alias": "...", "partner": "...",
#                       "client": "...", "enforced": false } }
#
# Scope comes from the REGISTRY: an account is in the map when its
# compliance_scope contains "hipaa" (from questionnaire 4.1). Nothing else is
# swept, which is what keeps tenants without HIPAA exposure untouched
# (docs/hipaa-eligibility.md, "Scope").
#
# "enforced" comes from ORGANIZATIONS, not from the registry. It answers "is
# PlatformHipaaEligibleOnly (or an exception variant of it) attached to this
# account right now", and the sweep's reading of a usage hit depends on it:
# with the SCP attached, a non-eligible call cannot have succeeded, so it is
# reported as a denied attempt. Taking that from the registry's intent rather
# than the live attachment would turn a missing attachment into a quiet
# "denied attempt" — exactly backwards.
#
# Same publishing arrangement as sync-tenant-facts.sh and sync-account-tiers.sh:
# the registry is the source of truth, the SSM copy is what a Lambda in Audit
# can read without a cross-account call on its path. Re-run after vesting a
# HIPAA client, after attaching or detaching the SCP, and after a design 11
# scope upgrade.

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
TABLE="${PLATFORM_REGISTRY_TABLE:-platform-registry}"
PARAM_NAME="/platform/eligibility/hipaa-accounts"
POLICY_PREFIX="PlatformHipaaEligibleOnly"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."
REGISTRY_ACCOUNT="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${REGISTRY_ACCOUNT}" && "${REGISTRY_ACCOUNT}" != "None" ]] \
  || die "Registry account not registered."

info "Reading the registry in ${REGISTRY_ACCOUNT}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${REGISTRY_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-hipaa-accounts-read \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the registry account."
read -r AK SK ST <<<"${CREDS}"

SCAN_ERR="$(mktemp)"
SCAN="$( unset AWS_PROFILE
         export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}"
         export AWS_SESSION_TOKEN="${ST}" MSYS_NO_PATHCONV=1
         aws dynamodb scan --table-name "${TABLE}" --output json \
           2>"${SCAN_ERR}" )" || true
[[ -n "${SCAN}" ]] || die "Could not read the registry table ${TABLE}:
      $(cat "${SCAN_ERR}")"
rm -f "${SCAN_ERR}"

# Registry -> the HIPAA-scoped accounts, before attachment state.
SCOPED="$(SCAN_JSON="${SCAN}" "${PY}" - <<'PYEOF'
import json, os
scan = json.loads(os.environ["SCAN_JSON"])
def val(item, key):
    cell = item.get(key)
    return next(iter(cell.values())) if cell else None
out = {}
for item in scan.get("Items", []):
    if val(item, "record_type") != "ACCOUNT":
        continue
    scope = (val(item, "compliance_scope") or "").split("_")
    if "hipaa" not in scope:
        continue
    out[val(item, "account_id")] = {
        "environment": val(item, "environment"),
        "partner": val(item, "partner"),
        "client": val(item, "client"),
        "app": val(item, "app") or None,
        "state": val(item, "state"),
    }
print(json.dumps(out, sort_keys=True))
PYEOF
)"

# Organizations -> attached now? Read from the management account, where SCP
# attachment lives.
MAP="${SCOPED}"
for ID in $("${PY}" -c 'import json,sys;print(" ".join(json.loads(sys.argv[1])))' "${SCOPED}"); do
  POLICIES="$(aws organizations list-policies-for-target --target-id "${ID}" \
    --filter SERVICE_CONTROL_POLICY --query 'Policies[].Name' --output text 2>/dev/null \
    | no_cr | tr '\t' ' ')" || die "Could not read SCP attachments for ${ID}."
  NAME="$(aws organizations describe-account --account-id "${ID}" \
    --query 'Account.Name' --output text 2>/dev/null | no_cr)" || NAME=""
  MAP="$("${PY}" -c "
import json,sys
m = json.loads(sys.argv[1]); a = sys.argv[2]
m[a]['enforced'] = any(p.startswith(sys.argv[3]) for p in sys.argv[4].split())
m[a]['alias'] = sys.argv[5] or None
print(json.dumps(m, sort_keys=True))
" "${MAP}" "${ID}" "${POLICY_PREFIX}" "${POLICIES}" "${NAME}")"
done

hr
log "HIPAA-scoped accounts"
"${PY}" -c "
import json,sys
m = json.loads(sys.argv[1])
for a, v in sorted(m.items(), key=lambda kv: (kv[1]['partner'] or '', kv[1]['client'] or '', kv[1]['environment'] or '')):
    print(f\"  {a}  {v['partner']}/{v['client']}  {v['environment']:<5} enforced={'yes' if v.get('enforced') else 'no '}  {v.get('alias') or ''}\")
print(f'  {len(m)} account(s)')
clients = sorted({(v['partner'], v['client']) for v in m.values()})
print()
print('  Client OUs the reader StackSet must target:')
for p, c in clients:
    print(f'    client:{p}/{c}')
" "${MAP}"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s in %s\n' "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-hipaa-accounts \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account (${AUDIT})."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

MSYS_NO_PATHCONV=1 aws ssm put-parameter \
  --name "${PARAM_NAME}" \
  --value "${MAP}" \
  --type String \
  --description "HIPAA-scoped accounts for the eligibility sweep: scope from the registry, enforced from live SCP attachment. Written by scripts/sync-hipaa-accounts.sh." \
  --overwrite \
  --output text --query Version >/dev/null

ok "wrote ${PARAM_NAME} in ${AUDIT}"
