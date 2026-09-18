#!/usr/bin/env bash
#
# Package the routing layer and publish it to the artifact bucket.
#
#   scripts/publish-router.sh [--dry-run]

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
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
cp "${REPO_ROOT}/alerting/src/router.py" "${BUILD}/router.py"

info "Checking the router compiles"
"${PY}" -m py_compile "$(win_path "${REPO_ROOT}/alerting/src/router.py")" \
  || die "router.py does not compile. Nothing packaged."
ok "compiles"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="router/router-${HASH}.zip"

hr
log "Router package"
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
  --role-session-name publish-router \
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
info "Deploy the routing layer into the Audit account:"
log "  scripts/deploy-stack.sh --account audit \\"
log "    --template alerting/20-router.yaml --stack platform-router \\"
log "    OrganizationId=<o-...> AlertsKeyArn=<key> AlertsTopicArn=<topic> \\"
log "    LowUrgencyTopicArn=<low> RouterCodeBucket=${BUCKET} RouterCodeKey=${KEY}"
log ""
log "Then point the members at it:"
log "  scripts/deploy-stackset.sh --name platform-event-forwarding \\"
log "    --template baseline/70-event-forwarding.yaml --ou sandbox \\"
log "    CentralBusArn=<bus arn from the router stack>"
hr
