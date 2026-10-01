#!/usr/bin/env bash
#
# Publish the authorised INFRASTRUCTURE change approvers to SSM in the Audit
# account, where change reconciliation reads them.
#
#   scripts/sync-change-approvers.sh [--dry-run]
#
# Reads config/change-approvers.env, which is gitignored and blocked by the
# pre-commit hook: an Atlassian accountId identifies a person.
#
# ---------------------------------------------------------------------------
# What this decides, and what it does not
# ---------------------------------------------------------------------------
# Jira decides who CAN answer an approval: the change record's Approver groups
# field and that group's membership. This decides whose answer COUNTS.
# Reconciliation accepts an infrastructure change as approved only when at
# least INFRA_MINIMUM_APPROVALS distinct people on this list approved it, and
# never counts the record's reporter. Jira's own rule is "any N members of the
# group", which neither checks identities against a list nor excludes the
# requester.
#
# Same arrangement, and same reasoning, as scripts/sync-quorum-approvers.sh for
# quorum-controlled actions. Kept as a separate list and parameter because the
# two sets are different people for different decisions: quorum guards
# destructive actions (design 03), this guards ordinary infrastructure change
# (design 08, decided 2026-10-01).

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

APPROVERS_FILE="${REPO_ROOT}/config/change-approvers.env"
PARAM_NAME="/platform/change/infrastructure-approvers"

[[ -f "${APPROVERS_FILE}" ]] || die "config/change-approvers.env not found.
      Copy config/change-approvers.env.example and fill in the accountIds.
      Find one with scripts/jira-check.sh, or from
      https://<site>.atlassian.net/rest/api/3/myself"

# shellcheck disable=SC1090
source "${APPROVERS_FILE}"

APPROVERS=()
for VAR in INFRA_APPROVER_1 INFRA_APPROVER_2 INFRA_APPROVER_3 \
           INFRA_APPROVER_4 INFRA_APPROVER_5; do
  VALUE="${!VAR:-}"
  [[ -n "${VALUE}" ]] || continue
  [[ "${VALUE}" != *"@"* ]] \
    || die "${VAR} looks like an email address.
      Approvers are Atlassian accountIds. An address matches no approval
      record, so every infrastructure change would read as unapproved."
  APPROVERS+=("${VALUE}")
done

MINIMUM="${INFRA_MINIMUM_APPROVALS:-2}"
[[ "${#APPROVERS[@]}" -gt 0 ]] || die "config/change-approvers.env lists no approvers."
[[ "${MINIMUM}" -ge 2 ]] || die "INFRA_MINIMUM_APPROVALS is ${MINIMUM}.
      Two approvals were decided on 2026-10-01. One approval from a list that
      excludes the requester is still one person's decision."
if [[ "${#APPROVERS[@]}" -lt "${MINIMUM}" ]]; then
  warn "${#APPROVERS[@]} approver(s) listed but ${MINIMUM} are required."
  warn "No infrastructure change can be counted approved until there are"
  warn "${MINIMUM} distinct people on this list."
fi

PAYLOAD="$("$(command -v python || command -v python3)" -c '
import json, sys
print(json.dumps({"approvers": sys.argv[1:-1],
                  "minimum": int(sys.argv[-1])}, separators=(",", ":")))
' "${APPROVERS[@]}" "${MINIMUM}")"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] \
  || die "Audit account not registered in SSM."

hr
log "Infrastructure change approvers"
log "  account  : altdig-security-audit (${AUDIT})"
log "  approvers: ${#APPROVERS[@]}"
log "  minimum  : ${MINIMUM}"
log "  parameter: ${PARAM_NAME}"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s into %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARAM_NAME}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-change-approvers \
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
    --description "Atlassian accountIds authorised to approve an infrastructure change, and how many are required. design 08." \
    --overwrite --output text --query Version >/dev/null \
    || exit 1
)
[[ $? -eq 0 ]] || die "Could not write the parameter into ${AUDIT}."
ok "published ${PARAM_NAME}"

hr
info "Reconciliation reads this on every run. It does NOT change who Jira lets"
log "  approve: that is the Approver groups field and the group's membership."
log "  Create the Jira group with the same people, and point the filer's"
log "  JIRA_APPROVER_GROUPS at it, or approvals will be refused in Jira first."
hr
