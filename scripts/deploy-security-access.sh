#!/usr/bin/env bash
#
# Give the security function estate-wide read access.
#
#   scripts/deploy-security-access.sh [--dry-run]
#
# Four things, in order, every one of them idempotent:
#
#   1. The SecurityReadOnly Identity Center group, created if absent.
#   2. Each member of config/identity.env's SECURITY_READONLY_MEMBERS created
#      in the identity store if absent, and added to the group.
#   3. identity/30-security-readonly.yaml deployed with that group's id.
#   4. The permission set assigned to EVERY ACTIVE ACCOUNT in the Organization.
#
# ---------------------------------------------------------------------------
# Why step 4 is derived rather than listed
# ---------------------------------------------------------------------------
# Identity Center assignments are per-account, and this permission set is meant
# for every account in the Organization including ones that do not exist yet --
# every tenant vested from here on.
#
# A fixed list of target accounts in the template would be a second copy of the
# account list, maintained by hand, and it would stop covering the estate the
# first time somebody vested a tenant and forgot to add it. The failure would be
# silent and would look exactly like working: the CISO signs in, sees seven
# accounts, and has no way to know there is an eighth.
#
# So the account list comes from Organizations. Re-run this after vesting.
# scripts/lib/cost_categories.py follows the same rule for the same reason:
# derive from the source of truth, do not keep a second copy of it.
#
# ---------------------------------------------------------------------------
# Nobody is ever removed
# ---------------------------------------------------------------------------
# A person in the group but not in config/identity.env is REPORTED and left
# alone, matching scripts/deploy-commercial-access.sh. A leaver is a deliberate
# act with a record, not a side effect of running a deployment script -- and a
# script that silently removed access would eventually remove somebody's during
# an incident because a config file was stale.
#
# ---------------------------------------------------------------------------
# Personal data
# ---------------------------------------------------------------------------
# Names and email addresses are read from config/identity.env, which is
# gitignored and blocked by the pre-commit hook. This script prints usernames
# during reconciliation because an operator needs to see which account it is
# acting on; it prints no email address and no display name.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

DRY_RUN=0
STACK_NAME="platform-security-access"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) printf 'Usage: %s [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

IDENTITY_ENV="${REPO_ROOT}/config/identity.env"
[[ -f "${IDENTITY_ENV}" ]] || die "config/identity.env not found.
      Copy config/identity.env.example and fill in locally. It is gitignored."
# shellcheck disable=SC1090
source "${IDENTITY_ENV}"

SECURITY_READONLY_GROUP="${SECURITY_READONLY_GROUP:-SecurityReadOnly}"
SECURITY_READONLY_MEMBERS="${SECURITY_READONLY_MEMBERS:-}"

INSTANCE_ARN="$(get_param /identity/instance-arn)"
IDENTITY_STORE_ID="$(get_param /identity/identity-store-id)"
[[ -n "${INSTANCE_ARN}" && "${INSTANCE_ARN}" != "None" ]] \
  || die "Identity Center instance ARN not published at /platform/identity/instance-arn."

split_records() { printf '%s\n' "$1" | tr ',' '\n'; }

hr
log "Security access"
log "  group    : ${SECURITY_READONLY_GROUP}"
log "  members  : $(printf '%s' "${SECURITY_READONLY_MEMBERS}" | tr ',' '\n' | grep -c . || true) configured"
log "  stack    : ${STACK_NAME}"
hr

# --- 1. the group ----------------------------------------------------------
GROUP_ID="$(group_id_by_name "${SECURITY_READONLY_GROUP}")"
if [[ -z "${GROUP_ID}" || "${GROUP_ID}" == "None" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create group %s\n' "${C_YELLOW}" "${C_RESET}" "${SECURITY_READONLY_GROUP}"
    GROUP_ID="00000000-0000-0000-0000-000000000000"
  else
    info "Creating group ${SECURITY_READONLY_GROUP}"
    GROUP_ID="$(aws identitystore create-group \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --display-name "${SECURITY_READONLY_GROUP}" \
      --description "Estate-wide read access to security controls, findings and evidence." \
      --query GroupId --output text | no_cr)" \
      || die "Could not create group ${SECURITY_READONLY_GROUP}."
    ok "group created"
  fi
else
  ok "group ${SECURITY_READONLY_GROUP} exists"
fi

# --- 2. members ------------------------------------------------------------
#
# An EMPTY member list is not an error, and this is the case that matters on
# first run: the group, the permission set and every assignment can all be in
# place before anybody is added to it. Adding a person is then one line in
# config/identity.env and a re-run, rather than a deployment.
if [[ -z "${SECURITY_READONLY_MEMBERS}" ]]; then
  warn "SECURITY_READONLY_MEMBERS is empty in config/identity.env."
  warn "  The group and its access are being created with no members. Add"
  warn "  records as GivenName|FamilyName|email and re-run to grant access."
else
  while IFS= read -r record; do
    record="$(printf '%s' "${record}" | sed 's/^ *//; s/ *$//')"
    [[ -n "${record}" ]] || continue
    IFS='|' read -r given family email username <<<"${record}"
    username="${username:-${email}}"
    [[ -n "${given}" && -n "${family}" && -n "${email}" ]] \
      || die "Malformed member record in config/identity.env.
      Expected GivenName|FamilyName|email[|userName], comma-separated between
      records."
    [[ "${email}" == *@*.* ]] \
      || die "Member record does not contain a valid email address."

    # By userName, which is the identity store's unique attribute. The email is
    # an attribute OF the user rather than the key; for anyone carrying a
    # fourth-field override the two differ and a search on the email finds
    # nothing, which would create a duplicate identity for a person who
    # already has one.
    UID_="$(user_id_by_name "${username}")"
    if [[ -z "${UID_}" || "${UID_}" == "None" ]]; then
      if [[ "${DRY_RUN}" == "1" ]]; then
        printf '%s DRY%s  create identity store user %s\n' "${C_YELLOW}" "${C_RESET}" "${username}"
        continue
      fi
      info "Creating identity store user ${username}"
      UID_="$(aws identitystore create-user \
        --identity-store-id "${IDENTITY_STORE_ID}" \
        --user-name "${username}" \
        --display-name "${given} ${family}" \
        --name "GivenName=${given},FamilyName=${family}" \
        --emails "Value=${email},Type=work,Primary=true" \
        --query UserId --output text | no_cr)" \
        || die "Could not create user ${username}."
      ok "user created ${username}"
    fi

    if is_member "${GROUP_ID}" "${UID_}"; then
      ok "${username} already in ${SECURITY_READONLY_GROUP}"
      continue
    fi
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  add %s to %s\n' "${C_YELLOW}" "${C_RESET}" "${username}" "${SECURITY_READONLY_GROUP}"
      continue
    fi
    aws identitystore create-group-membership \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --group-id "${GROUP_ID}" --member-id "UserId=${UID_}" \
      --query MembershipId --output text >/dev/null \
      || die "Could not add ${username} to ${SECURITY_READONLY_GROUP}."
    ok "${username} added to ${SECURITY_READONLY_GROUP}"
  done < <(split_records "${SECURITY_READONLY_MEMBERS}")
fi

# --- 3. the permission set -------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  deploy %s with GroupId=%s\n' \
    "${C_YELLOW}" "${C_RESET}" "${STACK_NAME}" "${GROUP_ID}"
else
  info "Deploying ${STACK_NAME}"
  aws cloudformation deploy \
    --stack-name "${STACK_NAME}" \
    --template-file "$(win_path "${REPO_ROOT}/identity/30-security-readonly.yaml")" \
    --parameter-overrides "InstanceArn=${INSTANCE_ARN}" "GroupId=${GROUP_ID}" \
    --capabilities CAPABILITY_NAMED_IAM >/dev/null \
    || die "Could not deploy ${STACK_NAME}."
  ok "permission set deployed"
fi

PS_ARN="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs[?OutputKey=='PermissionSetArn'].OutputValue" \
  --output text 2>/dev/null | no_cr)" || PS_ARN=""
if [[ -z "${PS_ARN}" || "${PS_ARN}" == "None" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    PS_ARN="arn:aws:sso:::permissionSet/dry-run"
  else
    die "Permission set ARN not found on ${STACK_NAME}."
  fi
fi

# --- 4. assignments, one per active account --------------------------------
hr
info "Assigning to every active account"

ACCOUNTS="$(aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].Id' --output text | no_cr)"
[[ -n "${ACCOUNTS}" ]] || die "Could not list Organization accounts."

CREATED=0
EXISTING=0
for ACCT in ${ACCOUNTS}; do
  # list-account-assignments is per (account, permission set), which is exactly
  # the idempotency question. Creating an assignment that already exists is not
  # an error in the API, but checking first keeps the output readable and makes
  # a re-run after vesting report only what changed.
  HAVE="$(aws sso-admin list-account-assignments \
    --instance-arn "${INSTANCE_ARN}" \
    --account-id "${ACCT}" \
    --permission-set-arn "${PS_ARN}" \
    --query "AccountAssignments[?PrincipalId=='${GROUP_ID}'].PrincipalId" \
    --output text 2>/dev/null | no_cr)" || HAVE=""

  if [[ -n "${HAVE}" && "${HAVE}" != "None" ]]; then
    EXISTING=$((EXISTING + 1))
    continue
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  assign to %s\n' "${C_YELLOW}" "${C_RESET}" "${ACCT}"
    CREATED=$((CREATED + 1))
    continue
  fi
  aws sso-admin create-account-assignment \
    --instance-arn "${INSTANCE_ARN}" \
    --target-id "${ACCT}" --target-type AWS_ACCOUNT \
    --permission-set-arn "${PS_ARN}" \
    --principal-type GROUP --principal-id "${GROUP_ID}" \
    --query 'AccountAssignmentCreationStatus.Status' --output text >/dev/null \
    || die "Could not assign to ${ACCT}."
  ok "assigned to ${ACCT}"
  CREATED=$((CREATED + 1))
done

hr
log "  accounts  : $(printf '%s' "${ACCOUNTS}" | wc -w | tr -d ' ')"
log "  assigned  : ${CREATED} new, ${EXISTING} already in place"
hr
info "Re-run this after vesting a tenant. The account list is read from"
log "  Organizations, so a new account is covered by the next run and by"
log "  nothing else."
hr
