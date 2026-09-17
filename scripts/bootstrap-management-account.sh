#!/usr/bin/env bash
#
# Bootstrap the Organization management account (738815759702).
#
# Runs ONCE against a brand-new, empty AWS account. Idempotent: safe to re-run,
# every step checks current state before acting.
#
# The management account holds the Organization and nothing else. No workloads,
# no pipelines, minimal access (design doc 02). This script therefore does the
# smallest possible set of things:
#
#   1  Confirm we are in the right account
#   2  Create the Organization with ALL features
#   3  Enable the policy types we will use
#   4  Enable trusted access for the services the platform delegates
#   5  Set the account alias
#   6  Set alternate contacts (optional, from a gitignored config)
#   7  Publish derived facts to SSM Parameter Store
#
# It deliberately does NOT create OUs — that is deploy-org-structure.sh, which
# runs CloudFormation so the tree is version-controlled and drift-detectable.
#
# Usage:
#   scripts/bootstrap-management-account.sh --dry-run
#   scripts/bootstrap-management-account.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  cat <<'USAGE'
Usage: bootstrap-management-account.sh [--dry-run]

  --dry-run   Print every mutating call without executing it. Read-only calls
              still run, so the report reflects real current state.
  -h, --help  This message.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

# ---------------------------------------------------------------------------
# Policy types enabled on the root.
# ---------------------------------------------------------------------------
# RESOURCE_CONTROL_POLICY is included deliberately. RCPs evaluate on the
# resource side regardless of which principal acts, which is a materially
# stronger tool than SCPs for the load-bearing assumption in design doc 04 and
# open item V1: "a member account with full local administrator cannot remove
# detective controls". Enabling the type now costs nothing; policies come in
# phase 1.2.
POLICY_TYPES=(
  SERVICE_CONTROL_POLICY
  RESOURCE_CONTROL_POLICY
  TAG_POLICY
  BACKUP_POLICY
  AISERVICES_OPT_OUT_POLICY
)

# ---------------------------------------------------------------------------
# Trusted access. Each entry is a service the platform administers org-wide
# from a delegated admin account rather than per-account.
# ---------------------------------------------------------------------------
SERVICE_PRINCIPALS=(
  cloudtrail.amazonaws.com                              # org trail -> Log Archive
  config.amazonaws.com                                  # Config recorder
  config-multiaccountsetup.amazonaws.com                # Config aggregation -> Audit
  guardduty.amazonaws.com                               # GuardDuty -> Audit
  securityhub.amazonaws.com                             # Security Hub -> Audit
  macie.amazonaws.com                                   # Macie -> Audit
  inspector2.amazonaws.com                              # Inspector (CVE scanning)
  access-analyzer.amazonaws.com                         # IAM Access Analyzer
  backup.amazonaws.com                                  # org backup policies + vault
  sso.amazonaws.com                                     # IAM Identity Center
  member.org.stacksets.cloudformation.amazonaws.com     # baseline StackSets
  ram.amazonaws.com                                     # resource sharing
  health.amazonaws.com                                  # org-wide Health events
  ssm.amazonaws.com                                     # Session Manager, Automation
  account.amazonaws.com                                 # alternate contacts org-wide
  reporting.trustedadvisor.amazonaws.com                # org Trusted Advisor
  compute-optimizer.amazonaws.com                       # rightsizing input to cost reporting
  tagpolicies.tag.amazonaws.com                         # tag policy enforcement
)

hr
log "AltDigital Managed Platform — management account bootstrap"
log "  target account : ${PLATFORM_MGMT_ACCOUNT_ID}"
log "  home region    : ${PLATFORM_HOME_REGION}"
if [[ "${DRY_RUN}" == "1" ]]; then
  log "  mode           : DRY RUN — no changes will be made"
else
  log "  mode           : LIVE — this will make changes"
fi
hr

# --- 1. guards -------------------------------------------------------------

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
warn_if_root
log "  caller: $(caller_arn)"
hr

# --- 2. organization -------------------------------------------------------

ORG_ID="$(org_exists || true)"
if [[ -n "${ORG_ID}" && "${ORG_ID}" != "None" ]]; then
  FEATURE_SET="$(aws organizations describe-organization \
    --query Organization.FeatureSet --output text)"
  skip "Organization ${ORG_ID} already exists (feature set: ${FEATURE_SET})."
  if [[ "${FEATURE_SET}" != "ALL" ]]; then
    die "Organization is CONSOLIDATED_BILLING only. SCPs require ALL features.
      Enable via: aws organizations enable-all-features
      That starts a handshake every member account must accept — with no members
      yet, it should complete immediately."
  fi
else
  run "Create Organization (feature set ALL)" \
    aws organizations create-organization --feature-set ALL \
      --query Organization.Id --output text
  ORG_ID="$(org_exists || echo 'o-dryrun')"
fi

ROOT_ID="$(org_root_id || true)"
if [[ -z "${ROOT_ID}" || "${ROOT_ID}" == "None" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    ROOT_ID="r-dryrun"
    warn "No Organization yet; using placeholder root id '${ROOT_ID}' for the dry run."
  else
    die "Organization exists but no root could be listed. Investigate before continuing."
  fi
fi
ok "Organization ${ORG_ID}, root ${ROOT_ID}"
hr

# --- 3. policy types -------------------------------------------------------

info "Enabling policy types on root ${ROOT_ID}"
ENABLED_TYPES=""
if [[ "${ROOT_ID}" != "r-dryrun" ]]; then
  # Filtered with awk rather than a JMESPath literal comparison, which needs
  # backticks and is awkward to quote safely across shells.
  ENABLED_TYPES="$(aws organizations list-roots \
    --output text --query 'Roots[0].PolicyTypes[].[Type,Status]' 2>/dev/null \
    | no_cr | awk '$2 == "ENABLED" { print $1 }' | tr '\n' ' ' || true)"
fi
for pt in "${POLICY_TYPES[@]}"; do
  if [[ " ${ENABLED_TYPES} " == *" ${pt} "* ]]; then
    skip "policy type ${pt} already enabled"
  else
    run "enable policy type ${pt}" \
      aws organizations enable-policy-type \
        --root-id "${ROOT_ID}" --policy-type "${pt}" \
        --output text --query 'Root.Id'
  fi
done
hr

# --- 4. trusted access -----------------------------------------------------

info "Enabling trusted access for delegated services"
ACTIVE_PRINCIPALS=""
if [[ "${ROOT_ID}" != "r-dryrun" ]]; then
  # --output text returns a list TAB-separated on a single line. Without
  # translating those tabs to spaces the substring test below matches only the
  # first entry, so every already-enabled principal is re-enabled on each run:
  # harmless, since the API is idempotent, but it defeats the skip and costs
  # ~90 seconds of needless round trips.
  ACTIVE_PRINCIPALS="$(aws organizations list-aws-service-access-for-organization \
    --query 'EnabledServicePrincipals[].ServicePrincipal' --output text 2>/dev/null \
    | no_cr | tr '\t\n' '  ' || true)"
fi
for sp in "${SERVICE_PRINCIPALS[@]}"; do
  if [[ " ${ACTIVE_PRINCIPALS} " == *" ${sp} "* ]]; then
    skip "trusted access ${sp}"
  else
    run "enable trusted access ${sp}" \
      aws organizations enable-aws-service-access --service-principal "${sp}"
  fi
done
hr

# --- 4b. CloudFormation organizations access ------------------------------
#
# Distinct from trusted access for member.org.stacksets.cloudformation.amazonaws.com
# above, and easy to conflate. Trusted access lets StackSets act within the
# Organization; THIS enables service-managed stack sets specifically. Without
# it, create-stack-set --permission-model SERVICE_MANAGED fails with
# "You must enable organizations access to operate a service managed stack set",
# which does not point at this call.

CFN_ORG_ACCESS="$(aws cloudformation describe-organizations-access --call-as SELF   --query Status --output text 2>/dev/null | no_cr || true)"
if [[ "${CFN_ORG_ACCESS}" == "ENABLED" ]]; then
  skip "CloudFormation organizations access already enabled"
else
  run "Enable CloudFormation organizations access"     aws cloudformation activate-organizations-access
fi
hr

# --- 5. account alias ------------------------------------------------------

CURRENT_ALIAS="$(aws iam list-account-aliases --query 'AccountAliases[0]' \
  --output text 2>/dev/null | no_cr | sed 's/^None$//')"
if [[ -z "${CURRENT_ALIAS}" ]]; then
  run "Set account alias ${PLATFORM_MGMT_ALIAS}" \
    aws iam create-account-alias --account-alias "${PLATFORM_MGMT_ALIAS}"
elif [[ "${CURRENT_ALIAS}" == "${PLATFORM_MGMT_ALIAS}" ]]; then
  skip "account alias already ${CURRENT_ALIAS}"
else
  warn "Account alias is '${CURRENT_ALIAS}', config expects '${PLATFORM_MGMT_ALIAS}'."
  warn "Not changing it automatically — the alias is the sign-in URL."
fi
hr

# --- 6. alternate contacts -------------------------------------------------
#
# AWS sends billing, operations and security notices to these. They are
# distinct from the root email and are frequently left unset, which means
# security notices reach only the root mailbox.
#
# Contact details are personal data and are NOT stored in this repository.
# Populate config/contacts.env (gitignored) from config/contacts.env.example.

CONTACTS_FILE="${REPO_ROOT}/config/contacts.env"
if [[ -f "${CONTACTS_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${CONTACTS_FILE}"
  for type in BILLING OPERATIONS SECURITY; do
    name_var="CONTACT_${type}_NAME"
    email_var="CONTACT_${type}_EMAIL"
    phone_var="CONTACT_${type}_PHONE"
    title_var="CONTACT_${type}_TITLE"
    if [[ -n "${!name_var:-}" && -n "${!email_var:-}" \
       && -n "${!phone_var:-}" && -n "${!title_var:-}" ]]; then
      # Normalised to E.164 as a house standard. AWS would accept the raw
      # value; consistency matters because these propagate to member accounts.
      if ! phone="$(normalise_phone "${!phone_var}")"; then
        warn "${type} phone number could not be normalised to E.164 — skipped."
        warn "  Expected 10 digits, 11 starting with 1, or a leading +."
        continue
      fi
      # Deliberately NOT routed through run(): its dry-run mode echoes the full
      # command, which would put names, direct emails and phone numbers into
      # the terminal and into any log or ticket this output is pasted into.
      # Only the contact type and the email domain are ever printed.
      if [[ "${DRY_RUN}" == "1" ]]; then
        printf '%s DRY%s  Set %s alternate contact (name, title, @%s, phone redacted)\n' \
          "${C_YELLOW}" "${C_RESET}" "${type}" "${!email_var##*@}"
      else
        aws account put-alternate-contact \
          --alternate-contact-type "${type}" \
          --name "${!name_var}" \
          --email-address "${!email_var}" \
          --phone-number "${phone}" \
          --title "${!title_var}"
        ok "${type} alternate contact set (@${!email_var##*@})"
      fi
    else
      warn "${type} alternate contact incomplete in contacts.env — skipped."
    fi
  done
else
  warn "config/contacts.env not found — alternate contacts NOT set."
  warn "Copy config/contacts.env.example and fill it in, then re-run. This is a"
  warn "real gap: AWS security notices will reach only the root mailbox until then."
fi
hr

# --- 7. publish derived facts ---------------------------------------------

info "Publishing platform facts to SSM Parameter Store"
put_param "/org/id"                 "${ORG_ID}"                    "AWS Organization id"
put_param "/org/root-id"            "${ROOT_ID}"                   "AWS Organization root id"
put_param "/org/mgmt-account-id"    "${PLATFORM_MGMT_ACCOUNT_ID}"  "Organization management account id"
put_param "/config/home-region"     "${PLATFORM_HOME_REGION}"      "Platform home region; Identity Center home region"
put_param "/config/allowed-regions" "${PLATFORM_ALLOWED_REGIONS}"  "Regions member accounts may operate in"
put_param "/config/account-prefix"  "${PLATFORM_ACCOUNT_PREFIX}"   "Account alias prefix"
put_param "/config/email-local"     "${PLATFORM_EMAIL_LOCAL}"      "Plus-addressing local part for member root emails"
put_param "/config/email-domain"    "${PLATFORM_EMAIL_DOMAIN}"     "Mail domain for member root emails"
hr

ok "Bootstrap complete."
log ""
log "Still outstanding — see docs/bootstrap-runbook.md:"
if [[ ! -f "${CONTACTS_FILE}" ]] || [[ -z "${CONTACT_SECURITY_EMAIL:-}" ]]; then
log "  * Alternate contacts are NOT set. AWS billing, operations and security"
log "    notices reach only the root mailbox. Fill config/contacts.env and"
log "    re-run this script — it is idempotent and will set only what changed"
fi
log "  * Second root MFA device on separate hardware (deviation D-006)"
log "  * Verify plus-addressed mail reaches ${PLATFORM_ROOT_EMAIL}"
log "  * Entra federation for Identity Center; delete PlatformBootstrapAdmin"
log "    once JIT elevation exists (deviation D-007)"
log ""
log "Next: scripts/deploy-org-structure.sh --dry-run"
