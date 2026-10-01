#!/usr/bin/env bash
#
# Publish one tenant's APPLICATION change approvers to SSM in the Audit
# account, where change reconciliation reads them.
#
#   scripts/sync-application-approvers.sh --approvers completed_qs/<client>.approvers.yaml [--dry-run]
#
# The file format, and what the rule is, are in vesting/approvers.example.yaml.
# In short: every `required` person approved, at least `minimum` distinct listed
# people approved, and the requester counts for nothing.
#
# Applies only to a tenant whose change records live on its own Atlassian site
# (questionnaire change.ticketing.system: client-jira). The accountIds are the
# ones on THAT site. Infrastructure changes have their own list:
# scripts/sync-change-approvers.sh.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

APPROVERS_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --approvers) APPROVERS_FILE="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   printf 'Usage: %s --approvers <file> [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${APPROVERS_FILE}" ]] || die "--approvers is required."
[[ -f "${APPROVERS_FILE}" ]] || die "Not found: ${APPROVERS_FILE}"

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

PY="$(command -v python || command -v python3)" || die "python not found."

# Validated in Python because it is a nested document; every problem is
# reported at once, and nothing is published unless all of them are fixed.
PARSED="$("${PY}" - "$(win_path "${APPROVERS_FILE}")" <<'PYEOF'
import json, re, sys
try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
errs = []
slug = re.compile(r"^[a-z0-9][a-z0-9-]{0,17}$")
for key in ("partner", "client"):
    if not slug.match(str(doc.get(key) or "")):
        errs.append(f"{key}: missing or not a slug")

def ids(section):
    out = []
    for i, entry in enumerate(doc.get(section) or []):
        acct = str((entry or {}).get("account_id") or "").strip()
        if not acct:
            errs.append(f"{section}[{i}]: no account_id")
        elif "@" in acct:
            errs.append(f"{section}[{i}]: account_id looks like an email address. "
                        "Approval records carry Atlassian accountIds, so an address "
                        "would match nothing and every change would read unapproved.")
        else:
            out.append(acct)
    return out

required, pool = ids("required"), ids("pool")
try:
    minimum = int(doc.get("minimum", 2))
except (TypeError, ValueError):
    minimum, _ = 0, errs.append("minimum: not a number")
if minimum < 2:
    errs.append(f"minimum is {minimum}. Two approvals were decided on 2026-10-01; "
                "one approval excluding the requester is still one person's decision.")
everyone = set(required) | set(pool)
if len(set(required) & set(pool)):
    errs.append("someone is in both required and pool; list each person once")
if len(everyone) < minimum:
    errs.append(f"{len(everyone)} distinct approver(s) listed but minimum is {minimum}: "
                "no change could ever be counted approved")
if len(required) > minimum:
    errs.append(f"{len(required)} required approvers exceeds minimum {minimum}")
if errs:
    print("INVALID")
    for e in errs:
        print("  - " + e)
    sys.exit()
print(f"{doc['partner']}-{doc['client']}")
print(json.dumps({"required": sorted(set(required)), "pool": sorted(set(pool)),
                  "minimum": minimum}, separators=(",", ":")))
print(f"{len(required)} required, {len(pool)} in the pool, minimum {minimum}")
PYEOF
)" || die "Could not read ${APPROVERS_FILE}."

if [[ "$(printf '%s\n' "${PARSED}" | head -1)" == "INVALID" ]]; then
  die "${APPROVERS_FILE} is not publishable:
$(printf '%s\n' "${PARSED}" | tail -n +2)"
fi
TENANT="$(printf '%s\n' "${PARSED}" | sed -n 1p)"
PAYLOAD="$(printf '%s\n' "${PARSED}" | sed -n 2p)"
SUMMARY="$(printf '%s\n' "${PARSED}" | sed -n 3p)"
PARAM_NAME="/platform/change/application-approvers/${TENANT}"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered in SSM."

hr
log "Application change approvers — ${TENANT}"
log "  account  : altdig-security-audit (${AUDIT})"
log "  approvers: ${SUMMARY}"
log "  parameter: ${PARAM_NAME}"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s into %s\n' "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-application-approvers \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${AUDIT}."
read -r AK SK ST <<<"${CREDS}"

(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  MSYS_NO_PATHCONV=1 aws ssm put-parameter \
    --name "${PARAM_NAME}" \
    --value "${PAYLOAD}" \
    --type String \
    --description "Atlassian accountIds, on ${TENANT}'s own Jira site, authorised to approve an application change; required, pool and minimum. design 08." \
    --overwrite --output text --query Version >/dev/null \
    || exit 1
)
[[ $? -eq 0 ]] || die "Could not write the parameter into ${AUDIT}."
ok "published ${PARAM_NAME}"
log "  Reconciliation reads this on every run. It does not change who the"
log "  tenant's Jira lets approve; configure that to match."
