#!/usr/bin/env bash
#
# Package the instrumentation digest Lambda and publish it.
#
#   scripts/publish-digest.sh [--dry-run]
#
# Shares zip_deterministic and package_hash with publish-instrumentation.sh via
# scripts/lib/common.sh. The content-addressed key matters for the same reason
# it does there: CloudFormation compares S3Key to decide whether to update a
# function, so publishing new code to the same key leaves the old code running
# while the stack reports itself current.

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

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

cp "${REPO_ROOT}/reporting/src/digest.py" "${BUILD}/digest.py"

info "Packaging"
zip_deterministic "${BUILD}"

HASH="$(package_hash "${BUILD}/package.zip")"
KEY="digest/digest-${HASH}.zip"

hr
log "Digest package"
log "  account : altdig-infra-tooling (${TOOLING})"
log "  key     : ${KEY}"
log "  size    : $(wc -c < "${BUILD}/package.zip" | tr -d ' ') bytes"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would upload s3://%s/%s\n' "${C_YELLOW}" "${C_RESET}" "${BUCKET}" "${KEY}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name publish-digest \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

if MSYS_NO_PATHCONV=1 aws s3api head-object --bucket "${BUCKET}" --key "${KEY}" >/dev/null 2>&1; then
  skip "identical package already published"
else
  MSYS_NO_PATHCONV=1 aws s3api put-object --bucket "${BUCKET}" --key "${KEY}" \
    --body "$(win_path "${BUILD}/package.zip")" >/dev/null
  ok "uploaded s3://${BUCKET}/${KEY}"
fi

hr
info "Deploy with:"
log "  scripts/deploy-to-account.sh --account altdig-infra-tooling \\"
log "    --template reporting/10-digest-sender.yaml --stack platform-digest \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    EvidenceBucket=altdig-log-archive-<logarchive-acct>"
log ""
log "Leave DigestSender EMPTY until an SES identity is verified. The function"
log "then gathers and renders and sends nothing, so the content can be read"
log "before a client's technical contact ever receives one."
hr
