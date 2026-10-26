#!/usr/bin/env bash
#
# Account-level security settings that CloudFormation cannot express (B-139).
#
#   scripts/apply-account-settings.sh --account <id> [--dry-run]
#   scripts/apply-account-settings.sh --account <id> --verify
#
# Run from the management account; assumes into the target. Idempotent: each
# setting is read first and changed only if it differs. --verify changes
# nothing and exits non-zero on any gap.
#
# ---------------------------------------------------------------------------
# What, and why a script
# ---------------------------------------------------------------------------
# Security Hub reported these on the Sandbox accounts, and every account the
# baseline creates would inherit the same gaps:
#
#   per region  EBS encryption by default                     EC2.7
#               EBS snapshot public sharing blocked           EC2.182
#               SSM document public sharing disabled          SSM.7
#               SSM Automation script output to CloudWatch    SSM.6 (B-174)
#               default security groups carry no rules        EC2.2, CIS 4.3
#   global      account-level S3 Block Public Access          S3.1
#               IAM password policy                           IAM.7, CIS 1.5-1.11
#               a role for AWS Support incidents              CIS 1.20
#               alternate contacts                            Account.1 (B-174)
#
# Only SSM.7, EC2.182 and the Support role have CloudFormation resources. Two
# homes for one list of account settings would be two lists that drift, so
# all of them live here. Snapshot blocking also becomes an EC2 declarative
# policy in 15.4; that overrides the account setting and makes this one
# redundant, not wrong.
#
# Called from vest-client.sh and create-platform-account.sh, so a new account
# is not created without them.
#
# The alternate contacts are the one setting made from the MANAGEMENT account
# rather than inside the target. They need Organizations trusted access for
# Account Management, which bootstrap-management-account.sh enables. The
# values are personal data from the gitignored config/contacts.env, and only
# the contact type and email domain are ever printed.
#
# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
# Two settings can break something that is already running, so each checks
# first and refuses rather than breaking it:
#
#   S3 Block Public Access  refused if any bucket is public by policy or ACL.
#                           Blocking it would cut off whatever reads it.
#   default SG rules        a VPC is skipped if any network interface uses its
#                           default group. Stripping rules under a running
#                           workload is an outage.
#
# Removing default SG rules is hardening at provisioning, not remediation, and
# it is reversible: every rule removed is printed first, in the form
# authorize-security-group-* accepts back.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""; VERIFY=0

usage() {
  cat <<'USAGE'
Usage: apply-account-settings.sh --account <id> [--dry-run | --verify]

  --account  Target member or platform account. Not the management account.
  --dry-run  Report current state and every change that would be made.
  --verify   Read-only. Exit non-zero if any setting is not in place.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --verify)  VERIFY=1; DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || { usage; die "--account <12-digit id> is required."; }
[[ "${TARGET_ACCOUNT}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "Not the management account. Its settings belong to bootstrap-management-account.sh."

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# One assumption for the run, not one per call: there are ~30 calls, and a
# per-call assume is 30 extra STS round trips and 30 extra CloudTrail events
# for the same session.
read -r T_KEY T_SECRET T_TOKEN <<<"$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-account-settings \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume OrganizationAccountAccessRole in ${TARGET_ACCOUNT}.
      A freshly created account can take a minute before the role is assumable; re-run."

as_t() { AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" \
         AWS_SESSION_TOKEN="${T_TOKEN}" "$@"; }

# For mutating calls whose JSON response is noise. The redirect lives inside
# the wrapper, not after `run`, or it would swallow run's own DRY line too.
as_tq() { as_t "$@" >/dev/null; }

GAPS=0
have()  { ok "$1"; }
gap()   {
  GAPS=$((GAPS+1))
  if   [[ ${VERIFY} -eq 1 ]];     then warn "GAP  $1"
  elif [[ "${DRY_RUN}" == "1" ]]; then info "would set  $1"
  else                                 info "set  $1"; fi
}
refuse(){ GAPS=$((GAPS+1)); warn "REFUSED  $1"; }

IFS=',' read -r -a REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"

hr
log "Account settings"
log "  account : ${TARGET_ACCOUNT}"
log "  regions : ${REGIONS[*]}"
log "  mode    : $([[ ${VERIFY} -eq 1 ]] && echo VERIFY || { [[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo APPLY; })"
hr

# --- per region ------------------------------------------------------------

for R in "${REGIONS[@]}"; do
  info "${R}"

  v="$(as_t aws ec2 get-ebs-encryption-by-default --region "${R}" \
         --query EbsEncryptionByDefault --output text | no_cr)"
  if [[ "${v}" == "True" ]]; then have "${R} EBS encryption by default"; else
    gap "${R} EBS encryption by default (was ${v})"
    run "enable EBS encryption by default in ${R}" \
      as_tq aws ec2 enable-ebs-encryption-by-default --region "${R}"
  fi

  v="$(as_t aws ec2 get-snapshot-block-public-access-state --region "${R}" \
         --query State --output text | no_cr)"
  if [[ "${v}" == "block-all-sharing" ]]; then have "${R} snapshot public sharing blocked"; else
    gap "${R} snapshot public sharing blocked (was ${v})"
    run "block all public sharing of EBS snapshots in ${R}" \
      as_tq aws ec2 enable-snapshot-block-public-access --state block-all-sharing \
        --region "${R}"
  fi

  v="$(as_t aws ssm get-service-setting --region "${R}" \
         --setting-id /ssm/documents/console/public-sharing-permission \
         --query ServiceSetting.SettingValue --output text | no_cr)"
  if [[ "${v}" == "Disable" ]]; then have "${R} SSM document public sharing disabled"; else
    gap "${R} SSM document public sharing disabled (was ${v})"
    run "disable public sharing of SSM documents in ${R}" \
      as_t aws ssm update-service-setting --region "${R}" \
        --setting-id /ssm/documents/console/public-sharing-permission --setting-value Disable
  fi

  # SSM.6: output of scripts that SSM Automation runs goes to CloudWatch Logs.
  # Without it, a runbook's executeScript output exists only in the Automation
  # execution history, which is not retained like logs and is not under the
  # account's log data protection policy. That matters when Automation is
  # what the response runbooks use. SSM writes to its default log group,
  # /aws/ssm/automation/executeScript, which it creates on first use.
  v="$(as_t aws ssm get-service-setting --region "${R}" \
         --setting-id /ssm/automation/customer-script-log-destination \
         --query ServiceSetting.SettingValue --output text | no_cr)"
  if [[ "${v}" == "CloudWatch" ]]; then have "${R} SSM Automation output to CloudWatch"; else
    gap "${R} SSM Automation output to CloudWatch (was ${v})"
    run "send SSM Automation script output to CloudWatch Logs in ${R}" \
      as_t aws ssm update-service-setting --region "${R}" \
        --setting-id /ssm/automation/customer-script-log-destination --setting-value CloudWatch
  fi

  # Default security groups. Every VPC has one, including the AWS-created
  # default VPC, and any resource launched without an explicit group lands in
  # it with its rules: all egress, and all ingress from itself.
  for sg in $(as_t aws ec2 describe-security-groups --region "${R}" \
                --filters Name=group-name,Values=default \
                --query 'SecurityGroups[].GroupId' --output text | no_cr); do
    ingress="$(as_t aws ec2 describe-security-groups --region "${R}" --group-ids "${sg}" \
                 --query 'SecurityGroups[0].IpPermissions' --output json)"
    egress="$(as_t aws ec2 describe-security-groups --region "${R}" --group-ids "${sg}" \
                --query 'SecurityGroups[0].IpPermissionsEgress' --output json)"
    vpc="$(as_t aws ec2 describe-security-groups --region "${R}" --group-ids "${sg}" \
             --query 'SecurityGroups[0].VpcId' --output text | no_cr)"
    if [[ "$(printf '%s' "${ingress}${egress}" | tr -d ' \r\n')" == "[][]" ]]; then
      have "${R} ${vpc} default SG ${sg} has no rules"; continue
    fi
    users="$(as_t aws ec2 describe-network-interfaces --region "${R}" \
               --filters "Name=group-id,Values=${sg}" \
               --query 'length(NetworkInterfaces)' --output text | no_cr)"
    if [[ "${users}" != "0" ]]; then
      refuse "${R} ${vpc} default SG ${sg}: ${users} network interface(s) use it. Move them to an explicit group first."
      continue
    fi
    gap "${R} ${vpc} default SG ${sg} carries rules"
    # Printed before removal so the change can be reversed by hand with
    # authorize-security-group-ingress/-egress --ip-permissions '<this>'.
    log "      ingress being removed: $(printf '%s' "${ingress}" | tr -d '\r\n ')"
    log "      egress being removed : $(printf '%s' "${egress}" | tr -d '\r\n ')"
    [[ "$(printf '%s' "${ingress}" | tr -d ' \r\n')" == "[]" ]] || \
      run "remove ingress rules from ${sg}" \
        as_tq aws ec2 revoke-security-group-ingress --region "${R}" --group-id "${sg}" \
          --ip-permissions "${ingress}"
    [[ "$(printf '%s' "${egress}" | tr -d ' \r\n')" == "[]" ]] || \
      run "remove egress rules from ${sg}" \
        as_tq aws ec2 revoke-security-group-egress --region "${R}" --group-id "${sg}" \
          --ip-permissions "${egress}"
  done
done

# --- global ----------------------------------------------------------------

info "global"

bpa="$(as_t aws s3control get-public-access-block --account-id "${TARGET_ACCOUNT}" \
         --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
         --output text 2>/dev/null | no_cr || true)"
if [[ "${bpa}" == $'True\tTrue\tTrue\tTrue' ]]; then have "S3 Block Public Access (account)"; else
  public=""
  for b in $(as_t aws s3api list-buckets --query 'Buckets[].Name' --output text | no_cr); do
    loc="$(as_t aws s3api get-bucket-location --bucket "${b}" --query LocationConstraint --output text 2>/dev/null | no_cr)"
    [[ -z "${loc}" || "${loc}" == "None" ]] && loc=us-east-1
    p="$(as_t aws s3api get-bucket-policy-status --bucket "${b}" --region "${loc}" \
           --query PolicyStatus.IsPublic --output text 2>/dev/null | no_cr || true)"
    a="$(as_t aws s3api get-bucket-acl --bucket "${b}" --region "${loc}" \
           --query "length(Grants[?Grantee.URI=='http://acs.amazonaws.com/groups/global/AllUsers' || Grantee.URI=='http://acs.amazonaws.com/groups/global/AuthenticatedUsers'])" \
           --output text 2>/dev/null | no_cr || true)"
    if [[ "${p}" == "True" || ( -n "${a}" && "${a}" != "0" ) ]]; then public="${public} ${b}"; fi
  done
  if [[ -n "${public}" ]]; then
    refuse "S3 Block Public Access: public bucket(s):${public}. Decide about them first."
  else
    gap "S3 Block Public Access (account) (was ${bpa:-unset})"
    run "block public access for every bucket in the account" \
      as_t aws s3control put-public-access-block --account-id "${TARGET_ACCOUNT}" \
        --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  fi
fi

# There are no IAM users in member accounts (CLAUDE.md), so the policy governs
# nobody today. It is set because an account without one accepts any password
# the day someone does create a user, and it clears eight CIS/FSBP findings
# that would otherwise hide real ones. Values are CIS 1.8-1.11.
PW_WANT=$'14\tTrue\tTrue\tTrue\tTrue\t90\t24'
pw="$(as_t aws iam get-account-password-policy \
        --query 'PasswordPolicy.[MinimumPasswordLength,RequireSymbols,RequireNumbers,RequireUppercaseCharacters,RequireLowercaseCharacters,MaxPasswordAge,PasswordReusePrevention]' \
        --output text 2>/dev/null | no_cr || true)"
if [[ "${pw}" == "${PW_WANT}" ]]; then have "IAM password policy"; else
  gap "IAM password policy (was ${pw:-none})"
  run "set the IAM password policy" \
    as_t aws iam update-account-password-policy --minimum-password-length 14 \
      --require-symbols --require-numbers --require-uppercase-characters \
      --require-lowercase-characters --allow-users-to-change-password \
      --max-password-age 90 --password-reuse-prevention 24
fi

# CIS 1.20: someone in the account can open and manage an AWS Support case
# during an incident. Trusts the account itself, so it is available to whoever
# already holds sts:AssumeRole there and grants nothing new beyond Support.
SUPPORT_ROLE="PlatformSupportAccess"
if as_t aws iam get-role --role-name "${SUPPORT_ROLE}" >/dev/null 2>&1; then
  have "Support role ${SUPPORT_ROLE}"
else
  gap "Support role ${SUPPORT_ROLE}"
  run "create ${SUPPORT_ROLE}" \
    as_tq aws iam create-role --role-name "${SUPPORT_ROLE}" \
      --description "AWS Support incident access (CIS 1.20). Created by apply-account-settings.sh." \
      --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${TARGET_ACCOUNT}:root\"},\"Action\":\"sts:AssumeRole\"}]}" \
      --tags Key=platform-managed,Value=true Key=platform-layer,Value=infrastructure
fi
if [[ "${DRY_RUN}" != "1" ]] || as_t aws iam get-role --role-name "${SUPPORT_ROLE}" >/dev/null 2>&1; then
  attached="$(as_t aws iam list-attached-role-policies --role-name "${SUPPORT_ROLE}" \
                --query "AttachedPolicies[?PolicyName=='AWSSupportAccess'] | length(@)" \
                --output text 2>/dev/null | no_cr || true)"
  if [[ "${attached}" == "1" ]]; then have "${SUPPORT_ROLE} has AWSSupportAccess"; else
    gap "${SUPPORT_ROLE} has AWSSupportAccess"
    run "attach AWSSupportAccess to ${SUPPORT_ROLE}" \
      as_t aws iam attach-role-policy --role-name "${SUPPORT_ROLE}" \
        --policy-arn arn:aws:iam::aws:policy/AWSSupportAccess
  fi
fi

# Account.1. AWS sends security, operations and billing notices to these.
# Unset, they reach only the root mailbox, a plus-addressed alias that no
# person reads as their own. The values are the same in every account: AWS's
# notices concern the infrastructure, which AltDigital operates whichever
# team owns the application.
#
# Set as the management account with --account-id, not through as_t. That is
# the Organizations-native path, and it is not subject to any SCP on the
# member. Not routed through run(): its dry-run line echoes the full command,
# which would print names, direct emails and phone numbers into the terminal
# and anything it is pasted into.
CONTACTS_FILE="${REPO_ROOT}/config/contacts.env"
if [[ ! -f "${CONTACTS_FILE}" ]]; then
  refuse "alternate contacts: config/contacts.env not found. Copy config/contacts.env.example and fill it in."
else
  # shellcheck source=/dev/null
  source "${CONTACTS_FILE}"
  for type in SECURITY OPERATIONS BILLING; do
    name_var="CONTACT_${type}_NAME";   email_var="CONTACT_${type}_EMAIL"
    phone_var="CONTACT_${type}_PHONE"; title_var="CONTACT_${type}_TITLE"
    if [[ -z "${!name_var:-}" || -z "${!email_var:-}" || -z "${!phone_var:-}" || -z "${!title_var:-}" ]] \
       || ! phone="$(normalise_phone "${!phone_var}")"; then
      refuse "${type} alternate contact: incomplete in contacts.env, or the phone is not a resolvable number."
      continue
    fi
    domain="@${!email_var##*@}"
    cur="$(aws account get-alternate-contact --account-id "${TARGET_ACCOUNT}" \
             --alternate-contact-type "${type}" \
             --query 'AlternateContact.[Name,EmailAddress,PhoneNumber,Title]' \
             --output text 2>/dev/null | no_cr || true)"
    if [[ "${cur}" == "${!name_var}"$'\t'"${!email_var}"$'\t'"${phone}"$'\t'"${!title_var}" ]]; then
      have "${type} alternate contact (${domain})"
    else
      gap "${type} alternate contact (${domain}; was $([[ -n "${cur}" ]] && echo different || echo unset))"
      if [[ "${DRY_RUN}" != "1" ]]; then
        aws account put-alternate-contact --account-id "${TARGET_ACCOUNT}" \
          --alternate-contact-type "${type}" \
          --name "${!name_var}" --email-address "${!email_var}" \
          --phone-number "${phone}" --title "${!title_var}"
      fi
    fi
  done
fi

hr
if [[ ${GAPS} -eq 0 ]]; then
  ok "All account settings in place for ${TARGET_ACCOUNT}."
elif [[ ${VERIFY} -eq 1 ]]; then
  die "${GAPS} account setting(s) not in place for ${TARGET_ACCOUNT}."
elif [[ "${DRY_RUN}" == "1" ]]; then
  log "  ${GAPS} change(s) or refusal(s) above. Nothing was changed."
else
  log "  ${GAPS} change(s) or refusal(s) above. Re-run with --verify to confirm."
fi
