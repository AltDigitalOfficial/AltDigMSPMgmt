#!/usr/bin/env bash
#
# Take the first recovery point of the sandbox restore-test fixture, so the
# restore workflow has something real to restore.
#
#   scripts/seed-restore-fixture.sh [--account <alias>] [--dry-run]
#
# ---------------------------------------------------------------------------
# Why this is a script and not a backup plan
# ---------------------------------------------------------------------------
# A backup PLAN would be policy: a schedule, a retention rule, a lifecycle, a
# selection. None of those values exist anywhere in the design package —
# design/05-resilience.md is absent — and inventing them is what CLAUDE.md
# forbids. See the header of recovery/00-restore-fixture.yaml and B-043.
#
# What the restore workflow needs to be PROVEN is not a policy. It is one
# recovery point. So this takes one, on demand, by hand, in the sandbox, and
# does not pretend to be anything else.
#
# Re-run it whenever the sandbox needs a fresher recovery point. It is
# idempotent in the sense that matters: a second run makes a second recovery
# point, the workflow picks the most recent COMPLETED one, and nothing is
# overwritten or deleted.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACCOUNT_KEY="altdig-sandbox-canary"
VAULT="platform-restore-fixture"
STACK="platform-restore-fixture"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    --vault)   VAULT="$2"; shift 2 ;;
    --stack)   STACK="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) printf 'Usage: %s [--account <alias>] [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TARGET="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TARGET}" && "${TARGET}" != "None" ]] \
  || die "Account ${ACCOUNT_KEY} is not registered in SSM."

info "Assuming into ${ACCOUNT_KEY} (${TARGET})"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET}:role/OrganizationAccountAccessRole" \
  --role-session-name seed-restore-fixture \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TARGET}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

# The volume ARN comes from the fixture stack's output rather than being
# constructed here. Two places building the same ARN is two places to get the
# partition or the region wrong.
VOLUME_ARN="$(aws cloudformation describe-stacks --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='VolumeArn'].OutputValue | [0]" \
  --output text 2>/dev/null | no_cr)"
[[ -n "${VOLUME_ARN}" && "${VOLUME_ARN}" != "None" ]] \
  || die "Stack ${STACK} has no VolumeArn output. Deploy
      recovery/00-restore-fixture.yaml first."

# AWS Backup needs a role to assume to take the backup, just as it does to
# restore. The restore role from recovery/10-restore-workflow.yaml cannot be
# reused: its managed policy grants RESTORE permissions, and a backup job needs
# CreateSnapshot on the source. AWS's own backup service role is used instead,
# created on demand here because it belongs to this scaffolding and not to the
# platform.
ROLE_NAME="PlatformRestoreFixtureBackup"
ROLE_ARN="arn:aws:iam::${TARGET}:role/${ROLE_NAME}"

if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  info "Creating ${ROLE_NAME}"
  if [[ "${DRY_RUN}" != "1" ]]; then
    MSYS_NO_PATHCONV=1 aws iam create-role --role-name "${ROLE_NAME}" \
      --description "Scaffolding for the sandbox restore-test fixture. Not part of the platform baseline." \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"backup.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      >/dev/null
    MSYS_NO_PATHCONV=1 aws iam attach-role-policy --role-name "${ROLE_NAME}" \
      --policy-arn 'arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup'
    ok "${ROLE_NAME} created"
    # IAM is eventually consistent and StartBackupJob validates the role at
    # submission. Without this the first run after creating the role fails with
    # "Role cannot be assumed", which reads as a policy error and is not one.
    info "Waiting for IAM to settle"
    sleep 12
  fi
else
  skip "${ROLE_NAME} already exists"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would start a backup job for %s into %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${VOLUME_ARN}" "${VAULT}"
  exit 0
fi

info "Starting the backup job"
JOB="$(MSYS_NO_PATHCONV=1 aws backup start-backup-job \
  --backup-vault-name "${VAULT}" \
  --resource-arn "${VOLUME_ARN}" \
  --iam-role-arn "${ROLE_ARN}" \
  --query BackupJobId --output text | no_cr)"
ok "backup job ${JOB}"

# Waited on rather than fired and forgotten. The point of this script is that a
# recovery point EXISTS afterwards; returning before it does would leave the
# next step to fail with "no recovery point", which is the very message the
# workflow uses for the real gap this is working around.
info "Waiting for it to complete"
for _ in $(seq 1 60); do
  STATUS="$(aws backup describe-backup-job --backup-job-id "${JOB}" \
    --query State --output text 2>/dev/null | no_cr)"
  case "${STATUS}" in
    COMPLETED)
      RP="$(aws backup describe-backup-job --backup-job-id "${JOB}" \
        --query RecoveryPointArn --output text | no_cr)"
      hr
      ok "recovery point ready"
      log "  ${RP}"
      hr
      log "Now deploy recovery/10-restore-workflow.yaml with:"
      log "  BackupVaultName=${VAULT}"
      log "  SourceResourceArn=${VOLUME_ARN}"
      exit 0 ;;
    FAILED|ABORTED|EXPIRED)
      REASON="$(aws backup describe-backup-job --backup-job-id "${JOB}" \
        --query StatusMessage --output text 2>/dev/null | no_cr)"
      die "Backup job ${STATUS}: ${REASON}" ;;
    *)
      printf '  %s ...\n' "${STATUS}"
      sleep 15 ;;
  esac
done

die "Backup job ${JOB} did not complete within 15 minutes. It is still running;
      check with: aws backup describe-backup-job --backup-job-id ${JOB}"
