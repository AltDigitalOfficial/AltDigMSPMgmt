#!/usr/bin/env bash
#
# Publish HIPAA eligibility tenant alert routing (E8) from config/hipaa-contacts.env.
#
#   scripts/sync-hipaa-alerts.sh [--dry-run]
#
# docs/hipaa-eligibility.md E8: a CRITICAL finding in a HIPAA tenant's dev or
# test goes to that tenant's named contact by email and to a ticket in the
# tenant's own Jira. The router decides that; this wires where it lands.
#
# Two things, both in the Audit account:
#
#   1. /platform/eligibility/tenant-alerts — per tenant, the Jira site, project
#      and issue type that eligibility/src/tenant_jira.py files into. Not
#      personal data, but kept beside the address it pairs with.
#
#   2. An SNS EMAIL subscription per tenant contact on the tenant alert topic,
#      with a filter policy on the tenant, so a contact receives their own
#      tenant's findings and no one else's. The address lives in the
#      subscription and in the gitignored file, nowhere in the repository.
#
# Why SNS email rather than the digest's SES: SES in this organisation is still
# in the sandbox (B-028/B-155), where every recipient must first verify. SNS
# email needs one confirmation click from the recipient and nothing from AWS.
# A subscription stays "PendingConfirmation" until that click, and this script
# says so on every run until it happens: an alert path nobody has confirmed is
# not an alert path.
#
# Removing a contact from the file does NOT unsubscribe them here; the script
# lists the stray subscription and leaves removal to a deliberate step.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) printf 'Usage: %s [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

CONTACTS="${REPO_ROOT}/config/hipaa-contacts.env"
[[ -f "${CONTACTS}" ]] || die "config/hipaa-contacts.env not found. Copy the .example and fill it in."
# shellcheck source=/dev/null
source "${CONTACTS}"
[[ -n "${HIPAA_ALERT_TENANTS:-}" ]] || die "HIPAA_ALERT_TENANTS is empty in config/hipaa-contacts.env."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
PY="$(command -v python || command -v python3)" || die "python not found."

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

TARGETS="{}"
declare -A EMAILS
for TENANT in ${HIPAA_ALERT_TENANTS}; do
  validate_slug tenant "${TENANT}"
  V="${TENANT//-/_}"
  email_var="${V}_EMAIL"; site_var="${V}_JIRA_SITE"; proj_var="${V}_JIRA_PROJECT"; type_var="${V}_JIRA_ISSUE_TYPE"
  [[ -n "${!email_var:-}" ]] || die "${email_var} is not set for tenant ${TENANT}."
  EMAILS["${TENANT}"]="${!email_var}"
  TARGETS="$("${PY}" -c "
import json,sys
m = json.loads(sys.argv[1])
m[sys.argv[2]] = {'jira_site': sys.argv[3] or None, 'jira_project': sys.argv[4] or None,
                  'issue_type': sys.argv[5] or 'Task'}
print(json.dumps(m, sort_keys=True))
" "${TARGETS}" "${TENANT}" "${!site_var:-}" "${!proj_var:-}" "${!type_var:-}")"
done

hr
log "HIPAA tenant alerts"
"${PY}" -c "
import json,sys
for t, v in sorted(json.loads(sys.argv[1]).items()):
    print(f\"  {t}: Jira {v['jira_project'] or '(none)'} at {v['jira_site'] or '(none)'}, type {v['issue_type']}; email contact set\")
" "${TARGETS}"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write /platform/eligibility/tenant-alerts and ensure %d email subscription(s)\n' \
    "${C_YELLOW}" "${C_RESET}" "${#EMAILS[@]}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-hipaa-alerts \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account (${AUDIT})."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

TOPIC="$(MSYS_NO_PATHCONV=1 aws cloudformation describe-stacks --stack-name platform-hipaa-eligibility \
  --query "Stacks[0].Outputs[?OutputKey=='TenantAlertTopicArn'].OutputValue | [0]" --output text | no_cr)"
[[ "${TOPIC}" == arn:aws:sns:* ]] || die "platform-hipaa-eligibility has no TenantAlertTopicArn output. Deploy it first."

MSYS_NO_PATHCONV=1 aws ssm put-parameter --name /platform/eligibility/tenant-alerts \
  --type String --overwrite --value "${TARGETS}" \
  --description "HIPAA eligibility E8: per-tenant Jira target for CRITICAL dev/test findings. From config/hipaa-contacts.env by scripts/sync-hipaa-alerts.sh." \
  --query Version --output text >/dev/null
ok "wrote /platform/eligibility/tenant-alerts"

SUBS="$(MSYS_NO_PATHCONV=1 aws sns list-subscriptions-by-topic --topic-arn "${TOPIC}" \
  --query "Subscriptions[?Protocol=='email'].[Endpoint,SubscriptionArn]" --output text | no_cr)"

for TENANT in "${!EMAILS[@]}"; do
  ADDR="${EMAILS[${TENANT}]}"
  FILTER="{\"tenant\":[\"${TENANT}\"]}"
  ARN="$(printf '%s\n' "${SUBS}" | awk -v a="${ADDR}" '$1==a {print $2; exit}')"
  if [[ -z "${ARN}" ]]; then
    # The filter policy is set AT subscribe time, so the subscription is never
    # unfiltered, not even before confirmation.
    MSYS_NO_PATHCONV=1 aws sns subscribe --topic-arn "${TOPIC}" --protocol email \
      --notification-endpoint "${ADDR}" \
      --attributes "{\"FilterPolicy\":\"${FILTER//\"/\\\"}\"}" >/dev/null
    warn "${TENANT}: subscription created. AWS has emailed the contact a confirmation link;"
    warn "  nothing is delivered until they click it."
  elif [[ "${ARN}" == "PendingConfirmation" ]]; then
    warn "${TENANT}: subscription still awaiting the contact's confirmation click."
  else
    MSYS_NO_PATHCONV=1 aws sns set-subscription-attributes --subscription-arn "${ARN}" \
      --attribute-name FilterPolicy --attribute-value "${FILTER}"
    ok "${TENANT}: subscription confirmed, filtered to ${TENANT}"
  fi
done

# Strays: subscribed addresses no longer in the file.
WANT=" ${EMAILS[*]} "
while read -r ep arn; do
  [[ -z "${ep}" ]] && continue
  [[ "${WANT}" == *" ${ep} "* ]] || warn "an email subscription not in config/hipaa-contacts.env remains on the topic (${arn}). Remove it deliberately if intended."
done <<<"${SUBS}"
hr
