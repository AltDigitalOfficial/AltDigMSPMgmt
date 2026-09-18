#!/usr/bin/env bash
#
# Package the response engine and publish it to the artifact bucket.
#
#   scripts/publish-response.sh [--dry-run]
#
# Converts runbooks/catalog.yaml to JSON the same way the instrumentation
# package converts 06a: the Lambda runtime ships boto3 but not PyYAML, and
# vendoring PyYAML to call safe_load once would add a third-party dependency to
# audit inside a function that can stop a container.
#
# The catalog is VALIDATED against actions.py before packaging. A catalog
# naming an unimplemented action would otherwise raise mid-incident, having
# already decided to act; an implementation of a refused action would restore a
# capability somebody decided against, with nothing about the deployment
# looking unusual.

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

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
SRC="${REPO_ROOT}/runbooks/src"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

for f in engine.py rails.py actions.py; do
  cp "${SRC}/${f}" "${BUILD}/${f}"
done

info "Validating the runbook catalog"
"${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/validate_catalog.py")" \
  "$(win_path "${REPO_ROOT}/runbooks/catalog.yaml")" \
  "$(win_path "${SRC}/actions.py")" \
  "$(win_path "${BUILD}/catalog.json")" \
  || die "runbooks/catalog.yaml is invalid. Nothing packaged."

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="response/engine-${HASH}.zip"

hr
log "Response package"
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
  --role-session-name publish-response \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

if MSYS_NO_PATHCONV=1 aws s3api head-object --bucket "${BUCKET}" --key "${KEY}" \
     >/dev/null 2>&1; then
  skip "identical package already published"
else
  MSYS_NO_PATHCONV=1 aws s3api put-object --bucket "${BUCKET}" --key "${KEY}" \
    --body "$(win_path "${BUILD}/package.zip")" >/dev/null
  ok "uploaded s3://${BUCKET}/${KEY}"
fi

hr
info "Deploy with:"
log "  scripts/deploy-stackset.sh --name platform-response \\"
log "    --template baseline/60-response.yaml --ou sandbox \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} EnvironmentTier=dev \\"
log "    AlertTopicArn=<audit topic> LowUrgencyTopicArn=<low topic>"
log ""
log "The kill switch deploys DISABLED and the alarm trigger deploys DISABLED."
log "Nothing acts on anything until both are turned on deliberately, which is"
log "the right default for the first component able to stop a container."
hr
