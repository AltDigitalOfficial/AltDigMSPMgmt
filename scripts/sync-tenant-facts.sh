#!/usr/bin/env bash
#
# Publish the account -> tenant facts map the reconciliation engine reads.
#
#   scripts/sync-tenant-facts.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# What is in it and why it is per account
# ---------------------------------------------------------------------------
# Two facts, both per tenant, both needed by change reconciliation (8.2):
#
#   jira_change_project   which Jira project holds THIS tenant's change records
#   change_approver       who an unapproved-deployment ticket is routed to
#
# design 08 routes a deployment with no change record to "the tenant's change
# approver", and questionnaire 1.7 collects that per client. Tenants must not
# see each other's change records, so the project is a property of the account
# rather than a platform constant.
#
# Same publishing arrangement as /platform/routing/account-tiers and
# /platform/incident/notification-windows: the registry is the source of truth
# and lives in another account, and the consumer runs in Audit. Reading across
# an account boundary on every reconciliation run would put a second account's
# availability in the path of a compliance control.
#
# ---------------------------------------------------------------------------
# Three maps, three scripts, one registry
# ---------------------------------------------------------------------------
# This is the third script that scans the registry and publishes a slice of it
# into Audit. The duplication is real and is deliberate for now: each map has
# one consumer and one meaning, and collapsing them into a single tenant-facts
# document would couple the routing layer, the incident clock and the
# reconciliation engine to one another's release cycles.
#
# Worth revisiting once there is a fourth. B-066.

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
PARAM_NAME="/platform/change/tenant-facts"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

REGISTRY_ACCOUNT="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${REGISTRY_ACCOUNT}" && "${REGISTRY_ACCOUNT}" != "None" ]] \
  || die "Registry account not registered."

info "Reading the registry in ${REGISTRY_ACCOUNT}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${REGISTRY_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-facts-read \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the registry account."
read -r AK SK ST <<<"${CREDS}"

# Subshell with `unset AWS_PROFILE`, not an inline AWS_PROFILE= prefix -- that
# sets an EMPTY profile name and the CLI fails with "The config profile ()
# could not be found", which points at ~/.aws/config. B-061.
SCAN_ERR="$(mktemp)"
SCAN="$( unset AWS_PROFILE
         export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}"
         export AWS_SESSION_TOKEN="${ST}" MSYS_NO_PATHCONV=1
         aws dynamodb scan --table-name "${TABLE}" --output json \
           2>"${SCAN_ERR}" )" || true
if [[ -z "${SCAN}" ]]; then
  die "Could not read the registry table ${TABLE} in ${REGISTRY_ACCOUNT}:
      $(cat "${SCAN_ERR}")"
fi
rm -f "${SCAN_ERR}"

MAP="$(SCAN_JSON="${SCAN}" DEFAULT_PROJECT="${PLATFORM_JIRA_DEFAULT_CHANGE_PROJECT:-}" \
       "${PY}" - <<'MAPEOF'
import json, os

scan = json.loads(os.environ["SCAN_JSON"])
default_project = os.environ.get("DEFAULT_PROJECT") or None


def val(item, key):
    cell = item.get(key)
    if not cell:
        return None
    return next(iter(cell.values()))


clients, accounts = {}, []
for item in scan.get("Items", []):
    kind = val(item, "record_type")
    if kind == "CLIENT":
        clients[(val(item, "partner"), val(item, "slug"))] = item
    elif kind == "ACCOUNT":
        accounts.append(item)

out = {}
for item in accounts:
    account_id = val(item, "account_id")
    if not account_id:
        continue
    client = clients.get((val(item, "partner"), val(item, "client"))) or {}
    out[account_id] = {
        "partner": val(item, "partner"),
        "client": val(item, "client"),
        # Null is meaningful, not missing. design 08 allows a tenant to keep
        # their own pipeline, and some will keep their own Jira -- the engine
        # then reports the change source as UNAVAILABLE rather than reporting
        # every deployment as unapproved.
        "jira_change_project": val(item, "jira_change_project")
                               or val(client, "jira_change_project")
                               or default_project,
        "change_approver": val(client, "change_approver"),
    }

print(json.dumps(out, sort_keys=True))
MAPEOF
)" || die "Could not build the tenant facts map."

COUNT="$(printf '%s' "${MAP}" | "${PY}" -c 'import json,sys; print(len(json.load(sys.stdin)))')"

hr
log "Tenant facts map"
log "  accounts : ${COUNT}"
log "  parameter: ${PARAM_NAME}"
log "  target   : altdig-security-audit (${AUDIT})"
log "  default  : ${PLATFORM_JIRA_DEFAULT_CHANGE_PROJECT:-<unset>}"
hr
printf '%s\n' "${MAP}" | "${PY}" -m json.tool

if [[ "${COUNT}" == "0" ]]; then
  warn "No member accounts in the registry yet."
  log  "      Reconciliation will have nothing to reconcile, which is correct"
  log  "      before the first tenant is vested -- not a failure."
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s in %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-facts-write \
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
  --description "Account -> tenant facts for change reconciliation. Written by scripts/sync-tenant-facts.sh from the registry." \
  --overwrite \
  --output text --query Version >/dev/null

ok "wrote ${PARAM_NAME} in ${AUDIT}"
hr
log "Re-run after vesting a client or changing a tenant's change project."
log "An account absent from this map has its deployments reconciled against"
log "the platform default, and every finding records that it did so."
hr
