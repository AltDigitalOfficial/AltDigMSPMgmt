#!/usr/bin/env bash
#
# Publish the authorised quorum approvers to SSM in the Audit account.
#
#   scripts/sync-quorum-approvers.sh [--dry-run]
#
# Reads config/quorum.env, which is gitignored. An Atlassian accountId
# identifies an individual, so the list lives in a local file and in Parameter
# Store, never in the repository.
#
# design/03-identity-and-access.md: two approvals from {Jamie, Wayne, Art},
# requester and approvers distinct, for KMS key deletion, backup vault deletion
# or lock modification, account removal from the Organization, and disabling
# CloudTrail or the Config recorder.
#
# The set is all four partners, not the three design 03 names -- D-022.

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

QUORUM_FILE="${REPO_ROOT}/config/quorum.env"
[[ -f "${QUORUM_FILE}" ]] || die "config/quorum.env not found.
      Copy config/quorum.env.example and fill in the accountIds.
      Find one with scripts/jira-check.sh, or from
      https://<site>.atlassian.net/rest/api/3/myself"

# shellcheck disable=SC1090
source "${QUORUM_FILE}"

APPROVERS=()
for VAR in QUORUM_APPROVER_1 QUORUM_APPROVER_2 QUORUM_APPROVER_3 \
           QUORUM_APPROVER_4 QUORUM_APPROVER_5; do
  VALUE="${!VAR:-}"
  [[ -n "${VALUE}" ]] || continue
  # An Atlassian accountId looks like 712020:uuid or a bare hex id. An email
  # address here is the mistake worth catching by name: the approval records
  # carry accountIds, so an address would match nobody and every approval
  # would read as unauthorised -- a configuration error wearing the costume of
  # a security finding.
  [[ "${VALUE}" != *"@"* ]] \
    || die "${VAR} looks like an email address: ${VALUE}
      Quorum approvers are Atlassian accountIds, not addresses. An address
      matches no approval record, so every approval would be reported as
      coming from an unauthorised person."
  APPROVERS+=("${VALUE}")
done

MINIMUM="${QUORUM_MINIMUM_APPROVALS:-2}"

[[ "${#APPROVERS[@]}" -gt 0 ]] || die "config/quorum.env lists no approvers."

# design 03 says two. Refuse to publish a quorum of one rather than letting it
# be discovered at execution time -- a control that one person can satisfy is
# not two-party control, and the failure is silent unless somebody looks.
[[ "${MINIMUM}" -ge 2 ]] || die "QUORUM_MINIMUM_APPROVALS is ${MINIMUM}.
      design 03 requires two approvals from distinct people. A quorum of one
      is not a quorum, and this script will not publish one."

if [[ "${#APPROVERS[@]}" -lt "${MINIMUM}" ]]; then
  # Not fatal. The list grows as partners are onboarded, and a platform that
  # refuses to hold the parameter until then is harder to bootstrap than one
  # that holds it and refuses to EXECUTE. The verifier enforces the count.
  warn "${#APPROVERS[@]} approver(s) configured but ${MINIMUM} are required."
  warn "Quorum actions will correctly refuse to execute until there are"
  warn "${MINIMUM} distinct people on this list. This is B-079, not a bug."
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
log "Quorum approvers"
log "  account  : altdig-security-audit (${AUDIT})"
log "  approvers: ${#APPROVERS[@]}"
log "  minimum  : ${MINIMUM}"
log "  parameter: /platform/quorum/approvers"
hr
# The ids themselves are not printed. They identify people, and this output
# lands in terminals and CI logs.

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write /platform/quorum/approvers into %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-quorum-approvers \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${AUDIT}."
read -r AK SK ST <<<"${CREDS}"
(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  # MSYS_NO_PATHCONV: the parameter name starts with a slash and would
  # otherwise arrive as C:/Program Files/Git/platform/quorum/approvers.
  MSYS_NO_PATHCONV=1 aws ssm put-parameter \
    --name /platform/quorum/approvers \
    --value "${PAYLOAD}" \
    --type String \
    --description "Atlassian accountIds authorised to approve a quorum-controlled action, and how many are required. design 03." \
    --overwrite --output text --query Version >/dev/null \
    || exit 1
)
[[ $? -eq 0 ]] || die "Could not write the parameter into ${AUDIT}."
ok "published /platform/quorum/approvers"

hr
info "The verifier reads this on every quorum request. To change the list,"
log "  edit config/quorum.env and run this again."
log ""
info "It does NOT change who Jira lets approve. That is the Approver groups"
log "  field and the group membership, and the two lists are independent by"
log "  design: Jira decides who CAN answer, this decides whose answer COUNTS."
hr
