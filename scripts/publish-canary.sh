#!/usr/bin/env bash
#
# Package the canary scripts and publish them to the artifact bucket.
#
#   scripts/publish-canary.sh [--dry-run]
#
# ONE package serves both runners, which is possible because the two runtimes
# look in different places inside it:
#
#   python/heartbeat.py   CloudWatch Synthetics, Python runtime. The python/
#                         prefix is mandatory — a canary zipped flat deploys
#                         successfully and then fails every run with a handler
#                         it cannot import, which reports as the SERVICE being
#                         down rather than as a packaging fault.
#   results.py            the Fargate results Lambda, at the root where Lambda
#                         looks for it.
#
# Each runner ignores the other's file. Two packages would be tidier and would
# also be two things to keep in step for no benefit, since the canary
# stack deploys exactly one runner.

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
SRC="${REPO_ROOT}/canaries/src"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

mkdir -p "${BUILD}/python"
cp "${SRC}/heartbeat.py" "${BUILD}/python/heartbeat.py"
cp "${SRC}/results.py"   "${BUILD}/results.py"

info "Checking the canary scripts compile"
# Compiled, not imported. heartbeat.py imports aws_synthetics, which exists only
# inside the Synthetics runtime, so an import check here would fail on a file
# that is correct. A syntax error, meanwhile, is caught at deploy only by the
# canary failing every run — which looks exactly like the monitored service
# being down, and is the expensive way to find a typo.
"${PY}" -m py_compile "$(win_path "${SRC}/heartbeat.py")" \
                      "$(win_path "${SRC}/results.py")" \
  || die "A canary script does not compile. Nothing packaged."
ok "both scripts compile"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="canary/canary-${HASH}.zip"

hr
log "Canary package"
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
  --role-session-name publish-canary \
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
info "Deploy one canary per tenant service:"
log "  scripts/deploy-stackset.sh --name platform-canary-<service> \\"
log "    --template canaries/10-canary.yaml --ou sandbox \\"
log "    CanaryName=<service> RunnerType=synthetics \\"
log "    Endpoint=https://... ExpectedStatus=200 BodyMustContain='...' \\"
log "    AcceptableResponseMs=<questionnaire 7.4> EnvironmentTier=dev \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    AlertTopicArn=<audit topic> LowUrgencyTopicArn=<low topic>"
log ""
log "AcceptableResponseMs is questionnaire 7.4 and is used three times: the"
log "canary pass condition, the SLA alarm, and alb-target-response-time. It is"
log "written to /platform/monitoring/acceptable-response-ms and all three read"
log "it from there, so there is no second place to change."
hr
