#!/usr/bin/env bash
#
# Backfill cross-region replication for objects that predate the replication
# rule, using S3 Batch Replication.
#
#   scripts/backfill-replication.sh [--bucket <name>] [--dry-run]
#
# ---------------------------------------------------------------------------
# Why this script has to exist
# ---------------------------------------------------------------------------
# An S3 replication rule is not retroactive. It applies to objects written
# after it exists and to nothing already in the bucket. When the rule was added
# on 2026-09-17 the log archive held 916 CloudTrail objects and the Config
# archive 46 — nine months of evidence that would have stayed single-region
# permanently while the bucket reported itself as replicated.
#
# This is worth being blunt about because the failure is silent and reads as
# success. `get-bucket-replication` returns a valid configuration, the console
# shows replication enabled, and the replica bucket contains objects. Nothing
# says "and the first 916 are not among them".
#
# ---------------------------------------------------------------------------
# Re-running this is safe and expected
# ---------------------------------------------------------------------------
# The manifest is GENERATED, not supplied, and filtered to objects whose
# replication status is NONE or FAILED. An object already replicated is not in
# the manifest, so a second run costs a manifest generation and nothing else.
#
# Run it after any change to a replication rule, after adding a bucket to the
# archive, and whenever the ReplicationFailed metric is non-zero.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACCOUNT_KEY="altdig-security-logarchive"
ONLY_BUCKET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)  ONLY_BUCKET="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TARGET="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TARGET}" && "${TARGET}" != "None" ]] \
  || die "No account registered at ${PLATFORM_SSM_PREFIX}/org/account/${ACCOUNT_KEY}."

info "Assuming OrganizationAccountAccessRole in ${TARGET}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TARGET}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-backfill \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume OrganizationAccountAccessRole in ${TARGET}."
read -r AK SK ST <<<"${CREDS}"

# The assumed session must not fall back to a named profile for the inner
# calls; unset rather than blank, because an empty AWS_PROFILE is treated as a
# profile named "" and fails with "The config profile () could not be found".
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

ROLE_ARN="arn:aws:iam::${TARGET}:role/PlatformArchiveBatchReplication"

BUCKETS=(
  "altdig-log-archive-${TARGET}"
  "altdig-config-archive-${TARGET}"
  "altdig-flow-logs-${TARGET}"
)
[[ -n "${ONLY_BUCKET}" ]] && BUCKETS=("${ONLY_BUCKET}")

hr
log "Batch replication backfill"
log "  account : ${ACCOUNT_KEY} (${TARGET})"
log "  region  : ${AWS_DEFAULT_REGION}"
log "  role    : ${ROLE_ARN}"
hr

for BUCKET in "${BUCKETS[@]}"; do
  # A bucket with no replication rule is skipped rather than failed: the flow
  # log bucket is empty and the rule is conditional, so this is a normal state
  # rather than an error.
  if ! MSYS_NO_PATHCONV=1 aws s3api get-bucket-replication --bucket "${BUCKET}" \
       >/dev/null 2>&1; then
    warn "${BUCKET}: no replication configuration, skipping"
    continue
  fi

  COUNT="$(aws s3 ls "s3://${BUCKET}" --recursive 2>/dev/null | wc -l | tr -d ' ')"
  info "${BUCKET}: ${COUNT} object(s) present"

  if [[ "${COUNT}" == "0" ]]; then
    ok "${BUCKET}: nothing to backfill"
    continue
  fi

  # ConfirmationRequired=false submits the job in Ready state rather than
  # Suspended. A Suspended job sits there doing nothing until someone opens the
  # console and confirms it, which is a good default for a destructive job and
  # the wrong one for a job that only copies.
  MANIFEST=$(cat <<JSON
{
  "S3JobManifestGenerator": {
    "ExpectedBucketOwner": "${TARGET}",
    "SourceBucket": "arn:aws:s3:::${BUCKET}",
    "EnableManifestOutput": false,
    "Filter": {
      "EligibleForReplication": true,
      "ObjectReplicationStatuses": ["NONE", "FAILED"]
    }
  }
}
JSON
)

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  would submit a batch replication job for %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${BUCKET}"
    continue
  fi

  JOB_ID="$(MSYS_NO_PATHCONV=1 aws s3control create-job \
    --account-id "${TARGET}" \
    --operation '{"S3ReplicateObject":{}}' \
    --manifest-generator "${MANIFEST}" \
    --report '{"Enabled":false}' \
    --priority 10 \
    --role-arn "${ROLE_ARN}" \
    --no-confirmation-required \
    --description "Backfill replication for ${BUCKET}" \
    --query JobId --output text 2>&1 | no_cr)" || true

  if [[ "${JOB_ID}" != job-* && ! "${JOB_ID}" =~ ^[0-9a-f-]{36}$ ]]; then
    warn "${BUCKET}: ${JOB_ID}"
    continue
  fi
  ok "${BUCKET}: job ${JOB_ID} submitted"
  printf '  status: aws s3control describe-job --account-id %s --job-id %s\n' \
    "${TARGET}" "${JOB_ID}"
done

hr
info "Jobs run asynchronously. Re-run this script to catch anything that failed."
