#!/usr/bin/env bash
#
# Deploy the commercial permission sets and reconcile their group's membership.
#
# Three things happen here, in order, and each is idempotent:
#
#   1. The Identity Center group is created if absent.
#   2. Both permission sets and their assignments are deployed by
#      CloudFormation: CommercialPayerReadOnly to the management account, then
#      CommercialReadOnly to FinOps.
#   3. Members listed in config/identity.env are created in the identity store
#      if absent and added to the group if not already in it.
#
# Steps 1 and 3 are not CloudFormation. Step 1 because the group must survive
# the eventual switch of the identity source to Entra as a parameter rather
# than a managed resource — see the note on the Assignment resource in the
# template. Step 3 because membership is personal data and the repository holds
# none.
#
# Nobody is ever removed from the group by this script. Removing access is a
# leaver action with consequences that should not be a side effect of running a
# deploy with a stale local config file. It reports the difference and stops
# there.
#
# Usage:
#   scripts/deploy-commercial-access.sh --dry-run
#   scripts/deploy-commercial-access.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

STACK_NAME=platform-identity-commercial
TEMPLATE="${REPO_ROOT}/identity/00-commercial-readonly.yaml"
PAYER_STACK_NAME=platform-identity-commercial-payer
PAYER_TEMPLATE="${REPO_ROOT}/identity/05-commercial-payer-readonly.yaml"
IDENTITY_ENV="${REPO_ROOT}/config/identity.env"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: deploy-commercial-access.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMMERCIAL_READONLY_GROUP="CommercialReadOnly"
COMMERCIAL_READONLY_MEMBERS=""

if [[ -f "${IDENTITY_ENV}" ]]; then
  # shellcheck source=/dev/null
  source "${IDENTITY_ENV}"
else
  warn "config/identity.env not found. The permission set and group will be"
  warn "deployed, but nobody will be added to the group."
  warn "  cp config/identity.env.example config/identity.env"
fi

# ---------------------------------------------------------------------------
# Identity Center instance
# ---------------------------------------------------------------------------
#
# Resolved live rather than configured. There can only be one instance per
# Organization and its ARN is generated at enablement, so reading it is
# strictly better than recording it in platform.env where it could drift.

identity_center_init

# The home region is also the Identity Center home region and cannot be changed
# without deleting the instance. If these ever disagree, something is being
# deployed against the wrong region and the assignment would silently target a
# different instance.
ok "Identity Center ${INSTANCE_ARN}"
ok "Identity store ${IDENTITY_STORE_ID}"

# ---------------------------------------------------------------------------
# 1. The group
# ---------------------------------------------------------------------------

GROUP_ID="$(group_id_by_name "${COMMERCIAL_READONLY_GROUP}")"

if [[ -n "${GROUP_ID}" && "${GROUP_ID}" != "None" ]]; then
  ok "Group ${COMMERCIAL_READONLY_GROUP} exists (${GROUP_ID})"
elif [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  create identity store group %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${COMMERCIAL_READONLY_GROUP}"
  warn "Group does not exist, so the stack cannot be changeset-tested in this"
  warn "dry run — GroupId is a required template parameter. Re-run without"
  warn "--dry-run to create the group, then dry-run the stack if wanted."
  GROUP_ID=""
else
  info "Creating identity store group ${COMMERCIAL_READONLY_GROUP}"
  GROUP_ID="$(aws identitystore create-group \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --display-name "${COMMERCIAL_READONLY_GROUP}" \
    --description "Billing and cost data only — design doc 12" \
    --query GroupId --output text | no_cr)"
  ok "Group created (${GROUP_ID})"
fi

# ---------------------------------------------------------------------------
# 2. Permission set and assignment
# ---------------------------------------------------------------------------

# Two permission sets for one group since 15.9 (D-009 narrowed):
#   CommercialPayerReadOnly  management account  invoices, payments, tax
#   CommercialReadOnly       FinOps              Cost Explorer, Athena
# The payer set deploys FIRST. Retargeting CommercialReadOnly removes the
# management-account assignment, and the group must already hold the payer
# grant at that moment, or invoices are unreachable in between.

FINOPS="$(get_param /org/account/altdig-infra-finops)"
[[ -n "${FINOPS}" && "${FINOPS}" != "None" ]] \
  || die "altdig-infra-finops is not registered. Create it first (15.9 step 1)."

hr
log "Commercial access deploy"
log "  instance : ${INSTANCE_ARN}"
log "  group    : ${COMMERCIAL_READONLY_GROUP} ${GROUP_ID:-<not yet created>}"
log "  payer    : ${PAYER_STACK_NAME} -> ${PLATFORM_MGMT_ACCOUNT_ID} (management, D-009)"
log "  analysis : ${STACK_NAME} -> ${FINOPS} (FinOps)"
log "  region   : ${AWS_DEFAULT_REGION}"
hr

if [[ -n "${GROUP_ID}" ]]; then
  cfn_deploy "${PAYER_STACK_NAME}" "${PAYER_TEMPLATE}" \
    "InstanceArn=${INSTANCE_ARN}" \
    "GroupId=${GROUP_ID}" \
    "ManagementAccountId=${PLATFORM_MGMT_ACCOUNT_ID}" \
    "SsmPrefix=${PLATFORM_SSM_PREFIX}"
  cfn_deploy "${STACK_NAME}" "${TEMPLATE}" \
    "InstanceArn=${INSTANCE_ARN}" \
    "GroupId=${GROUP_ID}" \
    "TargetAccountId=${FINOPS}" \
    "SsmPrefix=${PLATFORM_SSM_PREFIX}"
else
  skip "stacks ${PAYER_STACK_NAME}, ${STACK_NAME} — no group id available in this dry run"
fi

# ---------------------------------------------------------------------------
# 3. Membership
# ---------------------------------------------------------------------------
#
# Personal data is read here and never echoed in full. Progress lines print the
# email only, because the operator needs to know which record failed and the
# email is already on screen in the config file they just edited.

trim() { printf '%s' "$1" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'; }

# split_records <spec> -> one record per line
#
# Deliberately not `local IFS=','` with an unquoted for-loop. Changing IFS in a
# function changes what "$*" joins with, and every log helper in common.sh uses
# "$*" — the result is progress lines reading "Creating,identity,store,user".
# Learned the obvious way.
#
# printf '%s\n', not printf '%s'. Without the trailing newline the last --
# and with a single member, the only -- record has no line terminator, `read`
# returns non-zero on it, and the while loop body never runs. The script then
# finishes successfully having done nothing, which is the worst outcome
# available here: the group exists, the stack deploys, and nobody has access.
split_records() { printf '%s\n' "$1" | tr ',' '\n'; }

# record_username <record> -> field 4 if present, otherwise field 3
#
# The Identity Center userName is the sign-in identifier and is NOT required to
# be the email address. For staff provisioned by this script it is the email,
# which is what SCIM will key on after the Entra cutover. The optional fourth
# field exists for users created before this script — `jamie` was created by
# hand at bootstrap with a bare username — so their EXISTING record can be
# referenced rather than a duplicate identity created alongside it.
#
# Looking such a user up by email would return nothing and the script would
# cheerfully create a second user for the same person, which is much worse than
# failing.
record_username() {
  local u e
  u="$(trim "$(printf '%s' "$1" | cut -d'|' -f4)")"
  e="$(trim "$(printf '%s' "$1" | cut -d'|' -f3)")"
  printf '%s' "${u:-${e}}"
}

# configured_usernames <spec> -> the sign-in name of each record, one per line
#
# Must be the userName and not the email: the leaver check below compares this
# against describe-user's UserName, so comparing emails would report every
# override-using member as an unconfigured leaver on every run.
configured_usernames() {
  local record
  while read -r record; do
    record="$(trim "${record}")"
    [[ -n "${record}" ]] || continue
    record_username "${record}"; echo
  done < <(split_records "$1")
}

reconcile_members() {
  local group_id="$1" spec="$2"
  [[ -n "${spec}" ]] || { skip "no members configured"; return 0; }
  [[ -n "${group_id}" ]] || { skip "membership — no group id in this dry run"; return 0; }

  local record given family email username uid
  while read -r record; do
    record="$(trim "${record}")"
    [[ -n "${record}" ]] || continue

    given="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f1)")"
    family="$(trim "$(printf '%s' "${record}" | cut -d'|' -f2)")"
    email="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f3)")"
    username="$(record_username "${record}")"

    [[ -n "${given}" && -n "${family}" && -n "${email}" ]] \
      || die "Malformed member record in config/identity.env.
      Expected GivenName|FamilyName|email[|userName], comma-separated between
      records."
    [[ "${email}" == *@*.* ]] \
      || die "Member record does not contain a valid email address."

    # Looked up by userName, which is the identity store's unique attribute.
    # The email is an attribute OF the user, not the key — for anyone carrying
    # a fourth-field override the two differ, and searching on the email finds
    # nothing.
    uid="$(user_id_by_name "${username}")"

    if [[ -z "${uid}" || "${uid}" == "None" ]]; then
      if [[ "${DRY_RUN}" == "1" ]]; then
        printf '%s DRY%s  create identity store user %s\n' "${C_YELLOW}" "${C_RESET}" "${username}"
        continue
      fi
      info "Creating identity store user ${username}"
      uid="$(aws identitystore create-user \
        --identity-store-id "${IDENTITY_STORE_ID}" \
        --user-name "${username}" \
        --display-name "${given} ${family}" \
        --name "GivenName=${given},FamilyName=${family}" \
        --emails "Value=${email},Type=work,Primary=true" \
        --query UserId --output text | no_cr)"
      ok "User created ${username}"
    fi

    if is_member "${group_id}" "${uid}"; then
      ok "${username} already in ${COMMERCIAL_READONLY_GROUP}"
      continue
    fi

    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  add %s to %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${username}" "${COMMERCIAL_READONLY_GROUP}"
      continue
    fi
    aws identitystore create-group-membership \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --group-id "${group_id}" --member-id "UserId=${uid}" \
      --query MembershipId --output text >/dev/null
    ok "${username} added to ${COMMERCIAL_READONLY_GROUP}"
  done < <(split_records "${spec}")
}

info "Reconciling ${COMMERCIAL_READONLY_GROUP} membership"
reconcile_members "${GROUP_ID}" "${COMMERCIAL_READONLY_MEMBERS}"

# Report anyone in the group who is not in the config file. Not removed — see
# the header. A leaver should be handled deliberately, with a record.
if [[ -n "${GROUP_ID}" && "${DRY_RUN}" != "1" ]]; then
  CONFIGURED="$(configured_usernames "${COMMERCIAL_READONLY_MEMBERS}")"
  while read -r member_username; do
    [[ -n "${member_username}" ]] || continue
    # -F -x: the whole line, literally. A substring or regex match would treat
    # a.person@example.com as covering not.a.person@example.com. A real
    # address is not needed to make the point, and CLAUDE.md keeps direct
    # emails out of the repository.
    if ! printf '%s\n' "${CONFIGURED}" | grep -Fxq -- "${member_username}"; then
      warn "In the group but not in config/identity.env: ${member_username}"
      warn "  Not removed. Remove deliberately if this is a leaver."
    fi
  done < <(group_member_usernames "${GROUP_ID}")
fi

if [[ "${DRY_RUN}" != "1" ]]; then
  hr
  cfn_outputs "${STACK_NAME}" | sed 's/^/  /'
  hr
  log "Access portal: https://altdigital.awsapps.com/start"
  log ""
  log "Cost analysis is in the FinOps account (CommercialReadOnly). In Cost"
  log "Explorer there, choose the billing view 'platform-organization'; the"
  log "default view shows FinOps's own costs only. Invoices, payments and tax"
  log "are in the management account (CommercialPayerReadOnly)."
  log ""
  log "Verify the grant holds, signed in to FinOps as a group member:"
  log "  aws ce get-cost-and-usage --region us-east-1 \\"
  log "    --billing-view-arn <platform-organization view ARN> \\"
  log "    --time-period Start=\$(date -d '1 month ago' +%Y-%m-01),End=\$(date +%Y-%m-%d) \\"
  log "    --granularity MONTHLY --metrics UnblendedCost"
  log ""
  log "And that it does not reach further than billing — this must be denied:"
  log "  aws organizations list-policies --filter SERVICE_CONTROL_POLICY"
  log "  aws logs describe-log-groups"
  hr
fi
