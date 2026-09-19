#!/usr/bin/env bash
#
# Publish the account -> notification window map the incident clock reads.
#
#   scripts/sync-notification-windows.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# Why the clock needs this, and why it is a published map
# ---------------------------------------------------------------------------
# design 07's notification chain runs from discovery_time against two windows:
#
#   AltDigital -> Partner   (PARTNER.altdig_hours)
#   Partner    -> Client    (PARTNER.notify_hours)
#
# Both live in the registry, which is a DynamoDB table in another account. The
# clock runs in Audit, during an incident, and making it read across an account
# boundary to find out what its own deadline is would put a second account's
# availability on the path of a legally significant clock.
#
# So the map is published INTO Audit as a single SSM parameter, exactly as
# scripts/sync-account-tiers.sh publishes the tier map for the router. Same
# reasoning, same shape, same failure mode if it is not re-run: a newly vested
# account has no entry, and the clock reports that its obligations are unknown
# rather than assuming a default. Re-run this after vesting a client or
# changing a partner's windows.
#
# ---------------------------------------------------------------------------
# What is NOT here
# ---------------------------------------------------------------------------
# design 07 says notification windows are fields "at TWO levels — per partner
# and per client". The registry carries them at the PARTNER level only;
# scripts/registry.sh writes no window onto a CLIENT record, and the vesting
# questionnaire's own baa_window_partner_to_client_hours (field 2.6) is
# validated by derive-vesting.py and then not stored anywhere the clock can
# read.
#
# So a client-specific window that differs from its partner's default cannot
# currently be honoured. The partner default is used for every client under it.
# B-059.

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
PARAM_NAME="/platform/incident/notification-windows"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

REGISTRY_ACCOUNT="$(get_param "/org/account/altdig-infra-tooling")"
[[ -n "${REGISTRY_ACCOUNT}" && "${REGISTRY_ACCOUNT}" != "None" ]] \
  || die "Registry account not registered."

info "Reading the registry in ${REGISTRY_ACCOUNT}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${REGISTRY_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-windows-read \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the registry account."
read -r AK SK ST <<<"${CREDS}"

# A SUBSHELL with `unset AWS_PROFILE`, not an inline `AWS_PROFILE=` prefix.
#
# An inline assignment sets the variable to the EMPTY STRING, and the AWS CLI
# then looks for a profile literally named "" and fails with
# "The config profile () could not be found" -- which reads like a broken
# ~/.aws/config rather than like the credentials being overridden correctly.
# There is no way to unset a variable for the duration of one command, so the
# subshell is the mechanism.
#
# stderr is captured rather than discarded for the same reason: the previous
# version sent it to /dev/null and reported "could not read the registry",
# which points at the table and at permissions, and the cause was neither.
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

MAP="$(SCAN_JSON="${SCAN}" "${PY}" - <<'MAPEOF'
import json, os, sys

scan = json.loads(os.environ["SCAN_JSON"])
items = scan.get("Items", [])


def val(item, key):
    cell = item.get(key)
    if not cell:
        return None
    return next(iter(cell.values()))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


partners, accounts = {}, []
for item in items:
    kind = val(item, "record_type")
    if kind == "PARTNER":
        partners[val(item, "slug")] = {
            "altdig_to_partner_hours": _int(val(item, "altdig_hours")),
            "partner_to_client_hours": _int(val(item, "notify_hours")),
        }
    elif kind == "ACCOUNT":
        accounts.append(item)

out = {}
for item in accounts:
    account_id = val(item, "account_id")
    partner = val(item, "partner")
    if not account_id:
        continue
    w = partners.get(partner) or {}
    out[account_id] = {
        "partner": partner,
        "client": val(item, "client"),
        # Both may be null. The clock reports an unset window as a finding
        # rather than skipping the incident -- an unknown deadline is worse
        # than a tight one.
        "altdig_to_partner_hours": w.get("altdig_to_partner_hours"),
        "partner_to_client_hours": w.get("partner_to_client_hours"),
    }

print(json.dumps(out, sort_keys=True))
MAPEOF
)" || die "Could not build the window map."

COUNT="$(printf '%s' "${MAP}" | "${PY}" -c 'import json,sys; print(len(json.load(sys.stdin)))')"

hr
log "Notification window map"
log "  accounts : ${COUNT}"
log "  parameter: ${PARAM_NAME}"
log "  target   : altdig-security-audit (${AUDIT})"
hr
printf '%s\n' "${MAP}" | "${PY}" -m json.tool

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s in %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-windows-write \
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
  --description "Account -> notification windows. Written by scripts/sync-notification-windows.sh from the registry PARTNER records. Read by the incident clock." \
  --overwrite \
  --output text --query Version >/dev/null

ok "wrote ${PARAM_NAME} in ${AUDIT}"
hr
log "The clock re-reads this on a cold start. Re-run after vesting a client or"
log "changing a partner's notification windows, or new accounts will report"
log "their notification obligations as unknown."
hr
