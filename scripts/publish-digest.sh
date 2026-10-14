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
# Only committed sources are packaged: what runs must be a commit someone
# can name. lib/common.sh, require_committed.
require_committed reporting/src design/06a-alarm-specification.yaml

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

PY="$(command -v python || command -v python3)" || die "python not found."

# Two handlers, one package: digest.handler (Tooling, 10-digest-sender.yaml)
# and facts.handler (Audit, 20-digest-facts.yaml). digest.py imports facts.py
# for the window and the facts key layout, so the reader and the writer of a
# facts document cannot disagree about where it is.
cp "${REPO_ROOT}/reporting/src/digest.py" "${BUILD}/digest.py"
cp "${REPO_ROOT}/reporting/src/facts.py"  "${BUILD}/facts.py"

# 06a, for the gatherer's list of alarmable types. Converted and validated by
# the same script the instrumentation package uses, so both read one spec in
# one shape (publish-instrumentation.sh explains why 06a is never copied).
info "Validating design/06a-alarm-specification.yaml"
"${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/validate_alarm_spec.py")" \
  "$(win_path "${REPO_ROOT}/design/06a-alarm-specification.yaml")" \
  "$(win_path "${BUILD}/alarm-spec.json")" \
  || die "The alarm specification is invalid. Nothing packaged."

# tzdata, vendored and pinned. The digest renders in each reader's time zone
# (docs/digest-design.md §5), and zoneinfo needs a time zone database. Whether
# the Lambda image carries /usr/share/zoneinfo is not documented as a
# guarantee; the tzdata package is what zoneinfo falls back to, so shipping it
# makes the answer ours. Pinned so the package hash changes only when we
# change the pin. Bump it when IANA publishes a rule change that matters.
TZDATA_VERSION="2026.5"
info "Vendoring tzdata ${TZDATA_VERSION}"
"${PY}" -m pip install --quiet --disable-pip-version-check --no-deps \
  --only-binary=:all: --target "$(win_path "${BUILD}")" "tzdata==${TZDATA_VERSION}" \
  || die "Could not vendor tzdata ${TZDATA_VERSION}. Nothing packaged."
# pip's install metadata carries absolute paths and timestamps, which would
# make the content-addressed key differ between two builds of the same source.
rm -rf "${BUILD}"/tzdata-*.dist-info

info "Checking the sources compile"
"${PY}" -m py_compile \
  "$(win_path "${REPO_ROOT}/reporting/src/digest.py")" \
  "$(win_path "${REPO_ROOT}/reporting/src/facts.py")" \
  || die "reporting sources do not compile. Nothing packaged."

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
info "Deploy with, in this order (the sender reads what the gatherer writes):"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template reporting/20-digest-facts.yaml --stack platform-digest-facts \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} ToolingAccountId=${TOOLING}"
log ""
log "  scripts/deploy-to-account.sh --account altdig-infra-tooling \\"
log "    --template reporting/10-digest-sender.yaml --stack platform-digest \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    FactsBucket=altdig-digest-facts-<audit-acct>"
log ""
log "Parameters not passed keep their current values (UsePreviousValue)."
log "Then invoke platform-digest-facts once by hand, and platform-digest with"
log "{\"dry_run\": true} to read the rendered bodies before 13:00 UTC."
hr
