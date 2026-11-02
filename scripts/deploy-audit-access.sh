#!/usr/bin/env bash
#
# Give an audit engagement read access to the platform's evidence, and take it
# away again when the engagement ends.
#
#   scripts/deploy-audit-access.sh [--dry-run]
#   scripts/deploy-audit-access.sh --end-engagement [--dry-run]
#
# Granting, in order, every step idempotent:
#
#   1. The Auditors Identity Center group, created if absent.
#   2. Each member of config/identity.env's AUDITORS_MEMBERS created in the
#      identity store if absent, and added to the group.
#   3. identity/40-audit-evidence-reader.yaml deployed with that group's id.
#   4. security/50-trail-query.yaml deployed to Log Archive, with the
#      Organization id and EVERY account id derived from Organizations.
#   5. The permission set assigned to every ACTIVE account.
#   6. The engagement end date published at
#      /platform/identity/audit-engagement/ends.
#
# Ending (--end-engagement): every assignment of the permission set to the
# Auditors group is deleted, in every account it is provisioned to. The group,
# its members, the permission set and the query stack are left in place, so
# the next engagement is a config change and a re-run.
#
# ---------------------------------------------------------------------------
# Why an engagement has an end date, and why ending it is a separate act
# ---------------------------------------------------------------------------
# AuditEvidenceReader reads archived CloudTrail and platform log contents --
# personal data, in the form of principal names and source IPs. That is right
# for an engagement and wrong as a standing grant, and the difference between
# the two is whether anybody remembers to take it away.
#
# So AUDIT_ENGAGEMENT_ENDS is REQUIRED to grant. The script refuses to grant
# without one, refuses to grant past it, and says how long is left on every
# run. That makes the end date visible; it does not enforce it. Nothing
# revokes access automatically when the date passes -- see the gap below.
#
# Removal is a flag, not a side effect. Every other access script here adds
# and never removes, so that a stale config file cannot silently take access
# away mid-incident. This one is the same in normal operation: a member
# missing from config is reported, not removed. --end-engagement is the one
# removal, and it is a deliberate act with its own output on record.
#
# KNOWN GAP: a lapsed engagement whose assignments were never ended is
# invisible until somebody runs this script. Reading the published end date
# and raising a finding when it has passed with assignments still present is
# the check this needs; it is not built.
#
# ---------------------------------------------------------------------------
# Why the account list is derived, twice
# ---------------------------------------------------------------------------
# Assignments go to every ACTIVE account, for the reason recorded in
# deploy-security-access.sh: a hand-kept list silently stops covering the
# estate the first time somebody vests a tenant and forgets.
#
# The trail table projects EVERY account, any status. A closed account has no
# assignment to receive, but it has trail history an auditor may need.
#
# Re-run after vesting. A new account is not assignable, and not queryable in
# Athena, until the next run.
#
# ---------------------------------------------------------------------------
# Personal data
# ---------------------------------------------------------------------------
# As deploy-security-access.sh: names and addresses come from the gitignored
# config/identity.env, and usernames -- which are email addresses -- are
# printed during reconciliation. The OUTPUT of this script is personal data.
# Mask it before pasting a run anywhere.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

DRY_RUN=0
END_ENGAGEMENT=0
STACK_NAME="platform-audit-access"
QUERY_STACK="platform-trail-query"
QUERY_ACCOUNT="altdig-security-logarchive"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        DRY_RUN=1; shift ;;
    --end-engagement) END_ENGAGEMENT=1; shift ;;
    -h|--help) printf 'Usage: %s [--end-engagement] [--dry-run]\n' "$0"; exit 0 ;;
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

AUDITORS_GROUP="${AUDITORS_GROUP:-Auditors}"
AUDITORS_MEMBERS="${AUDITORS_MEMBERS:-}"
AUDIT_ENGAGEMENT_ENDS="${AUDIT_ENGAGEMENT_ENDS:-}"

INSTANCE_ARN="$(get_param /identity/instance-arn)"
IDENTITY_STORE_ID="$(get_param /identity/identity-store-id)"
[[ -n "${INSTANCE_ARN}" && "${INSTANCE_ARN}" != "None" ]] \
  || die "Identity Center instance ARN not published at /platform/identity/instance-arn."

TODAY="$(date -u +%F)"

split_records() { printf '%s\n' "$1" | tr ',' '\n'; }

# =========================================================================
# --end-engagement
# =========================================================================
if [[ "${END_ENGAGEMENT}" == "1" ]]; then
  GROUP_ID="$(group_id_by_name "${AUDITORS_GROUP}")"
  [[ -n "${GROUP_ID}" && "${GROUP_ID}" != "None" ]] \
    || die "Group ${AUDITORS_GROUP} does not exist; there is nothing to end."
  PS_ARN="$(get_param /identity/permission-set/audit-evidence-reader)"
  [[ -n "${PS_ARN}" && "${PS_ARN}" != "None" ]] \
    || die "AuditEvidenceReader is not published at /platform/identity/permission-set/audit-evidence-reader."

  hr
  log "Ending audit engagement"
  log "  group          : ${AUDITORS_GROUP}"
  log "  engagement end : ${AUDIT_ENGAGEMENT_ENDS:-<not set>}"
  log "  today (UTC)    : ${TODAY}"
  hr

  # From where the permission set IS provisioned, not from the Organization's
  # current account list: an account closed during the engagement may still
  # carry an assignment, and an end that misses one is not an end.
  PROVISIONED="$(aws sso-admin list-accounts-for-provisioned-permission-set \
    --instance-arn "${INSTANCE_ARN}" --permission-set-arn "${PS_ARN}" \
    --query 'AccountIds' --output text | no_cr)" \
    || die "Could not list accounts for AuditEvidenceReader."

  REMOVED=0
  for ACCT in ${PROVISIONED}; do
    HAVE="$(aws sso-admin list-account-assignments \
      --instance-arn "${INSTANCE_ARN}" --account-id "${ACCT}" \
      --permission-set-arn "${PS_ARN}" \
      --query "AccountAssignments[?PrincipalId=='${GROUP_ID}'].PrincipalId" \
      --output text 2>/dev/null | no_cr)" || HAVE=""
    [[ -n "${HAVE}" && "${HAVE}" != "None" ]] || continue
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  remove assignment in %s\n' "${C_YELLOW}" "${C_RESET}" "${ACCT}"
      REMOVED=$((REMOVED + 1))
      continue
    fi
    aws sso-admin delete-account-assignment \
      --instance-arn "${INSTANCE_ARN}" \
      --target-id "${ACCT}" --target-type AWS_ACCOUNT \
      --permission-set-arn "${PS_ARN}" \
      --principal-type GROUP --principal-id "${GROUP_ID}" \
      --query 'AccountAssignmentDeletionStatus.Status' --output text >/dev/null \
      || die "Could not remove the assignment in ${ACCT}. Assignments removed before it stay removed; re-run to finish."
    ok "removed from ${ACCT}"
    REMOVED=$((REMOVED + 1))
  done

  put_param /identity/audit-engagement/ends "ended ${TODAY}" \
    "Audit engagement end. 'ended <date>' once scripts/deploy-audit-access.sh --end-engagement has run."

  hr
  log "  assignments removed : ${REMOVED}"
  hr
  info "Access ends when each assignment's deletion completes, normally within"
  log "  a minute. A console session already open runs to its 4-hour expiry --"
  log "  Identity Center does not revoke issued role sessions."
  log "  The group, its members and the permission set are left in place."
  hr
  exit 0
fi

# =========================================================================
# Grant
# =========================================================================

# --- the engagement window -------------------------------------------------
[[ -n "${AUDIT_ENGAGEMENT_ENDS}" ]] || die "AUDIT_ENGAGEMENT_ENDS is not set in config/identity.env.
      An audit grant has an end date, and the end date is set before access
      is, not after. Set it as YYYY-MM-DD and re-run."
[[ "${AUDIT_ENGAGEMENT_ENDS}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || die "AUDIT_ENGAGEMENT_ENDS must be YYYY-MM-DD; got '${AUDIT_ENGAGEMENT_ENDS}'."
[[ "${AUDIT_ENGAGEMENT_ENDS}" > "${TODAY}" ]] \
  || die "AUDIT_ENGAGEMENT_ENDS (${AUDIT_ENGAGEMENT_ENDS}) is not in the future.
      To end the engagement, run with --end-engagement. To extend it, change
      the date -- an extension is a decision, and the config change is its
      record."

DAYS_LEFT="$(( ( $(date -u -d "${AUDIT_ENGAGEMENT_ENDS}" +%s) - $(date -u -d "${TODAY}" +%s) ) / 86400 ))"

hr
log "Audit access"
log "  group          : ${AUDITORS_GROUP}"
log "  members        : $(printf '%s' "${AUDITORS_MEMBERS}" | tr ',' '\n' | grep -c . || true) configured"
log "  engagement end : ${AUDIT_ENGAGEMENT_ENDS} (${DAYS_LEFT} days)"
log "  stacks         : ${STACK_NAME} (management), ${QUERY_STACK} (${QUERY_ACCOUNT})"
hr
if [[ "${DAYS_LEFT}" -gt 120 ]]; then
  warn "The engagement runs ${DAYS_LEFT} days. A SOC 2 Type 2 fieldwork window is"
  warn "  usually far shorter; if this is meant to be standing access, it is the"
  warn "  wrong permission set."
fi

# --- 1. the group ----------------------------------------------------------
GROUP_ID="$(group_id_by_name "${AUDITORS_GROUP}")"
if [[ -z "${GROUP_ID}" || "${GROUP_ID}" == "None" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create group %s\n' "${C_YELLOW}" "${C_RESET}" "${AUDITORS_GROUP}"
    GROUP_ID="00000000-0000-0000-0000-000000000000"
  else
    info "Creating group ${AUDITORS_GROUP}"
    GROUP_ID="$(aws identitystore create-group \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --display-name "${AUDITORS_GROUP}" \
      --description "Audit engagements. Read access to platform evidence for the length of an engagement." \
      --query GroupId --output text | no_cr)" \
      || die "Could not create group ${AUDITORS_GROUP}."
    ok "group created"
  fi
else
  ok "group ${AUDITORS_GROUP} exists"
fi

# --- 2. members ------------------------------------------------------------
if [[ -z "${AUDITORS_MEMBERS}" ]]; then
  warn "AUDITORS_MEMBERS is empty in config/identity.env."
  warn "  The group and its access are being created with no members."
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

    # By userName, the identity store's unique attribute -- see the same
    # lookup in deploy-security-access.sh for why the email is not the key.
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
      ok "${username} already in ${AUDITORS_GROUP}"
      continue
    fi
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  add %s to %s\n' "${C_YELLOW}" "${C_RESET}" "${username}" "${AUDITORS_GROUP}"
      continue
    fi
    aws identitystore create-group-membership \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --group-id "${GROUP_ID}" --member-id "UserId=${UID_}" \
      --query MembershipId --output text >/dev/null \
      || die "Could not add ${username} to ${AUDITORS_GROUP}."
    ok "${username} added to ${AUDITORS_GROUP}"
  done < <(split_records "${AUDITORS_MEMBERS}")
fi

# Reported, never removed -- a member removed from config stays in the group
# until somebody removes them deliberately. --end-engagement is what takes
# the group's access away.
if [[ "${DRY_RUN}" != "1" ]]; then
  CONFIGURED="$(split_records "${AUDITORS_MEMBERS}" | awk -F'|' 'NF{u=$4; if(u=="")u=$3; gsub(/^ +| +$/,"",u); print u}')"
  while read -r present; do
    [[ -n "${present}" ]] || continue
    grep -qxF "${present}" <<<"${CONFIGURED}" \
      || warn "${present} is in ${AUDITORS_GROUP} but not in config/identity.env. Left in place."
  done < <(group_member_usernames "${GROUP_ID}")
fi

# --- 3. the permission set -------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  deploy %s with GroupId=%s\n' "${C_YELLOW}" "${C_RESET}" "${STACK_NAME}" "${GROUP_ID}"
else
  cfn_deploy "${STACK_NAME}" "${REPO_ROOT}/identity/40-audit-evidence-reader.yaml" \
    "InstanceArn=${INSTANCE_ARN}" "GroupId=${GROUP_ID}"
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

# --- 4. the trail query stack, in Log Archive ------------------------------
hr
info "Trail query stack"
# Its own script since 2026-11-01, so the table does not depend on an
# engagement being granted (ADR-008 amendment).
TQ_ARGS=()
[[ "${DRY_RUN}" == "1" ]] && TQ_ARGS+=(--dry-run)
"${REPO_ROOT}/scripts/deploy-trail-query.sh" "${TQ_ARGS[@]}" \
  || die "Could not deploy the trail query stack."

# --- 5. assignments, one per active account --------------------------------
hr
info "Assigning to every active account"

ACCOUNTS="$(aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].Id' --output text | no_cr)"
[[ -n "${ACCOUNTS}" ]] || die "Could not list Organization accounts."

CREATED=0
EXISTING=0
for ACCT in ${ACCOUNTS}; do
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

# --- 6. the end date, published --------------------------------------------
put_param /identity/audit-engagement/ends "${AUDIT_ENGAGEMENT_ENDS}" \
  "Audit engagement end date. AuditEvidenceReader assignments should not outlive it."

hr
log "  accounts       : $(printf '%s' "${ACCOUNTS}" | wc -w | tr -d ' ') active"
log "  assigned       : ${CREATED} new, ${EXISTING} already in place"
log "  trail table    : platform_audit.cloudtrail, workgroup platform-audit (Log Archive)"
log "  engagement end : ${AUDIT_ENGAGEMENT_ENDS} (${DAYS_LEFT} days)"
hr
info "Re-run after vesting: a new account is neither assigned nor queryable"
log "  until the next run."
info "When the engagement ends:  scripts/deploy-audit-access.sh --end-engagement"
log "  Nothing ends it for you."
hr
