#!/usr/bin/env bash
#
# Deploy the CommercialReadOnly permission set and reconcile its membership.
#
# Three things happen here, in order, and each is idempotent:
#
#   1. The Identity Center group is created if absent.
#   2. The permission set and its assignment are deployed by CloudFormation.
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

read -r INSTANCE_ARN IDENTITY_STORE_ID < <(
  aws sso-admin list-instances \
    --query 'Instances[0].[InstanceArn,IdentityStoreId]' \
    --output text 2>/dev/null | no_cr
) || true

[[ -n "${INSTANCE_ARN:-}" && "${INSTANCE_ARN}" != "None" ]] \
  || die "No IAM Identity Center instance found in ${AWS_DEFAULT_REGION}.
      Identity Center must be enabled in the home region before this runs.
      See docs/bootstrap-runbook.md step 4."

# The home region is also the Identity Center home region and cannot be changed
# without deleting the instance. If these ever disagree, something is being
# deployed against the wrong region and the assignment would silently target a
# different instance.
ok "Identity Center ${INSTANCE_ARN}"
ok "Identity store ${IDENTITY_STORE_ID}"

put_param /identity/instance-arn "${INSTANCE_ARN}" \
  "IAM Identity Center instance ARN"
put_param /identity/identity-store-id "${IDENTITY_STORE_ID}" \
  "IAM Identity Center identity store id"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# --alternate-identifier must be JSON, not AWS CLI shorthand. Shorthand looks
# like it ought to work and fails with "Shorthand syntax does not support
# document types" — AttributeValue is a document type, so the whole argument
# has to be JSON.
#
# The failure mode is what makes this worth a comment. Both lookups below are
# wrapped in `2>/dev/null || true` because "not found" is a legitimate answer,
# and that swallows the shorthand parse error too: the call returns empty on
# EVERY invocation, so an existing group reads as absent and the next run tries
# to create a duplicate instead of reusing it. Cost one deploy to find.
#
# alternate_identifier builds the JSON with printf rather than inline escaped
# quotes. Same result, but a shell-quoted "{\"UniqueAttribute\":{...}}" is
# unreadable and one deleted backslash away from being silently wrong.
#
# Safe on Git Bash: the JSON has no leading slash, so MSYS path conversion has
# nothing to rewrite, and common.sh disables it regardless.
#
# --member-id is a plain union with a string member, not a document type, so
# shorthand is genuinely fine there. Left as shorthand rather than made
# uniform, because the difference between the two is the point.

# alternate_identifier <attribute-path> <value> -> JSON for --alternate-identifier
alternate_identifier() {
  printf '{"UniqueAttribute":{"AttributePath":"%s","AttributeValue":"%s"}}' "$1" "$2"
}

# group_id_by_name <display-name> -> group id, or empty
group_id_by_name() {
  aws identitystore get-group-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --alternate-identifier "$(alternate_identifier displayName "$1")" \
    --query GroupId --output text 2>/dev/null | no_cr || true
}

# user_id_by_name <user-name> -> user id, or empty
user_id_by_name() {
  aws identitystore get-user-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --alternate-identifier "$(alternate_identifier userName "$1")" \
    --query UserId --output text 2>/dev/null | no_cr || true
}

# is_member <group-id> <user-id> -> 0 if already a member
is_member() {
  aws identitystore get-group-membership-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --group-id "$1" --member-id "UserId=$2" \
    --query MembershipId --output text >/dev/null 2>&1
}

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

hr
log "Commercial access deploy"
log "  stack    : ${STACK_NAME}"
log "  instance : ${INSTANCE_ARN}"
log "  group    : ${COMMERCIAL_READONLY_GROUP} ${GROUP_ID:-<not yet created>}"
log "  target   : ${PLATFORM_MGMT_ACCOUNT_ID}  (deviation D-009)"
log "  region   : ${AWS_DEFAULT_REGION}"
hr

if [[ -n "${GROUP_ID}" ]]; then
  cfn_deploy "${STACK_NAME}" "${TEMPLATE}" \
    "InstanceArn=${INSTANCE_ARN}" \
    "GroupId=${GROUP_ID}" \
    "TargetAccountId=${PLATFORM_MGMT_ACCOUNT_ID}" \
    "SsmPrefix=${PLATFORM_SSM_PREFIX}"
else
  skip "stack ${STACK_NAME} — no group id available in this dry run"
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

# configured_emails <spec> -> the third field of each record, one per line
configured_emails() {
  split_records "$1" | cut -d'|' -f3 \
    | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' || true
}

reconcile_members() {
  local group_id="$1" spec="$2"
  [[ -n "${spec}" ]] || { skip "no members configured"; return 0; }
  [[ -n "${group_id}" ]] || { skip "membership — no group id in this dry run"; return 0; }

  local record given family email uid
  while read -r record; do
    record="$(trim "${record}")"
    [[ -n "${record}" ]] || continue

    given="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f1)")"
    family="$(trim "$(printf '%s' "${record}" | cut -d'|' -f2)")"
    email="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f3)")"

    [[ -n "${given}" && -n "${family}" && -n "${email}" ]] \
      || die "Malformed member record in config/identity.env.
      Expected GivenName|FamilyName|email, comma-separated between records."
    [[ "${email}" == *@*.* ]] \
      || die "Member record does not contain a valid email address."

    uid="$(user_id_by_name "${email}")"

    if [[ -z "${uid}" || "${uid}" == "None" ]]; then
      if [[ "${DRY_RUN}" == "1" ]]; then
        printf '%s DRY%s  create identity store user %s\n' "${C_YELLOW}" "${C_RESET}" "${email}"
        continue
      fi
      info "Creating identity store user ${email}"
      uid="$(aws identitystore create-user \
        --identity-store-id "${IDENTITY_STORE_ID}" \
        --user-name "${email}" \
        --display-name "${given} ${family}" \
        --name "GivenName=${given},FamilyName=${family}" \
        --emails "Value=${email},Type=work,Primary=true" \
        --query UserId --output text | no_cr)"
      ok "User created ${email}"
    fi

    if is_member "${group_id}" "${uid}"; then
      ok "${email} already in ${COMMERCIAL_READONLY_GROUP}"
      continue
    fi

    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  add %s to %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${email}" "${COMMERCIAL_READONLY_GROUP}"
      continue
    fi
    aws identitystore create-group-membership \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --group-id "${group_id}" --member-id "UserId=${uid}" \
      --query MembershipId --output text >/dev/null
    ok "${email} added to ${COMMERCIAL_READONLY_GROUP}"
  done < <(split_records "${spec}")
}

info "Reconciling ${COMMERCIAL_READONLY_GROUP} membership"
reconcile_members "${GROUP_ID}" "${COMMERCIAL_READONLY_MEMBERS}"

# Report anyone in the group who is not in the config file. Not removed — see
# the header. A leaver should be handled deliberately, with a record.
if [[ -n "${GROUP_ID}" && "${DRY_RUN}" != "1" ]]; then
  CONFIGURED="$(configured_emails "${COMMERCIAL_READONLY_MEMBERS}")"
  while read -r member_email; do
    [[ -n "${member_email}" ]] || continue
    # -F -x: the whole line, literally. A substring or regex match would treat
    # jamie@altdigital.ai as covering notjamie@altdigital.ai.
    if ! printf '%s\n' "${CONFIGURED}" | grep -Fxq -- "${member_email}"; then
      warn "In the group but not in config/identity.env: ${member_email}"
      warn "  Not removed. Remove deliberately if this is a leaver."
    fi
  done < <(
    aws identitystore list-group-memberships \
      --identity-store-id "${IDENTITY_STORE_ID}" --group-id "${GROUP_ID}" \
      --query 'GroupMemberships[].MemberId.UserId' --output text 2>/dev/null \
      | no_cr | tr '\t' '\n' \
      | while read -r u; do
          [[ -n "${u}" ]] || continue
          aws identitystore describe-user \
            --identity-store-id "${IDENTITY_STORE_ID}" --user-id "${u}" \
            --query UserName --output text 2>/dev/null | no_cr
        done
  )
fi

if [[ "${DRY_RUN}" != "1" ]]; then
  hr
  cfn_outputs "${STACK_NAME}" | sed 's/^/  /'
  hr
  log "Access portal: https://altdigital.awsapps.com/start"
  log ""
  log "Verify the grant holds, signed in as a group member:"
  log "  aws ce get-cost-and-usage --region us-east-1 \\"
  log "    --time-period Start=\$(date -d '1 month ago' +%Y-%m-01),End=\$(date +%Y-%m-%d) \\"
  log "    --granularity MONTHLY --metrics UnblendedCost"
  log ""
  log "And that it does not reach further than billing — this must be denied:"
  log "  aws organizations list-policies --filter SERVICE_CONTROL_POLICY"
  log "  aws logs describe-log-groups"
  hr
fi
