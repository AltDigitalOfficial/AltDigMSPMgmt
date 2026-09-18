#!/usr/bin/env bash
#
# Package the instrumentation Lambda and publish it to the artifact bucket.
#
#   scripts/publish-instrumentation.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# What this does that a plain zip does not
# ---------------------------------------------------------------------------
# 1. Ships design/06a-alarm-specification.yaml as the configuration, converted
#    to JSON.
#
#    06a is AUTHORITATIVE and is consumed directly. There is deliberately no
#    copy under instrumentation/ — an earlier version kept its own
#    alarm-sets.yaml, written before 06a existed, and two copies of an
#    authoritative document disagree silently while the wrong one is always the
#    one somebody edits.
#
#    JSON because the Lambda runtime ships boto3 but not PyYAML, and vendoring
#    PyYAML for one safe_load would add a dependency to audit inside a function
#    that creates alarms in every member account. Authoring stays YAML so 06a's
#    comments survive; only the data ships.
#
# 2. VALIDATES the specification first. A malformed 06a would otherwise surface
#    as every invocation erroring at cold start, in every member account, with
#    a stack trace instead of a reason.
#
#    One package, two handlers. drift.py imports handler.py to reuse
#    plan_alarms — they must never disagree about what an alarm should be, and
#    shipping them separately would make that possible.
#
# 3. Publishes under a CONTENT-ADDRESSED key. CloudFormation decides whether to
#    update a Lambda by comparing S3Key — publish new code to the same key and
#    every member account keeps running the old one while the stack reports
#    itself current.

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
  || die "Platform Tooling account not registered. Run:
      scripts/create-platform-account.sh --ou infra --role tooling"

BUCKET="altdig-platform-artifacts-${TOOLING}"
SRC="${REPO_ROOT}/instrumentation/src"
SPEC="${REPO_ROOT}/design/06a-alarm-specification.yaml"

[[ -f "${SPEC}" ]] || die "Alarm specification not found: ${SPEC}"

BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

cp "${SRC}/handler.py" "${BUILD}/handler.py"
cp "${SRC}/drift.py"   "${BUILD}/drift.py"

info "Validating design/06a-alarm-specification.yaml"
"${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/validate_alarm_spec.py")" \
  "$(win_path "${SPEC}")" "$(win_path "${BUILD}/alarm-spec.json")" \
  || die "The alarm specification is invalid. Nothing packaged."

# Zipped with python, not zip(1). Git for Windows does not ship zip, and more
# importantly the archive must be DETERMINISTIC: the key is a hash of the
# package, so identical input must produce an identical key. A zip stores each
# entry's mtime, so the same source zipped twice hashes differently and
# content-addressing degrades into "a new key every run".
info "Packaging"
"${PY}" - "$(win_path "${BUILD}")" <<'PYEOF'
import sys, zipfile, pathlib
build = pathlib.Path(sys.argv[1])
names = sorted(["handler.py", "drift.py", "alarm-spec.json"])
with zipfile.ZipFile(build / "package.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name in names:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, (build / name).read_bytes())
print(f"  {len(names)} file(s)")
PYEOF

HASH="$("${PY}" -c "
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:16])
" "$(win_path "${BUILD}/package.zip")")"
KEY="instrumentation/handler-${HASH}.zip"
SIZE="$(wc -c < "${BUILD}/package.zip" | tr -d ' ')"

hr
log "Instrumentation package"
log "  account : altdig-infra-tooling (${TOOLING})"
log "  spec    : design/06a-alarm-specification.yaml"
log "  key     : ${KEY}"
log "  size    : ${SIZE} bytes"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would upload s3://%s/%s\n' "${C_YELLOW}" "${C_RESET}" "${BUCKET}" "${KEY}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name publish-instrumentation \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

if MSYS_NO_PATHCONV=1 aws s3api head-object --bucket "${BUCKET}" --key "${KEY}" \
     >/dev/null 2>&1; then
  skip "identical package already published (content-addressed key)"
else
  MSYS_NO_PATHCONV=1 aws s3api put-object --bucket "${BUCKET}" --key "${KEY}" \
    --body "$(win_path "${BUILD}/package.zip")" >/dev/null
  ok "uploaded s3://${BUCKET}/${KEY}"
fi

hr
info "Deploy it with:"
log "  scripts/deploy-stackset.sh --name platform-instrumentation \\"
log "    --template baseline/50-instrumentation.yaml --ou sandbox \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    EnvironmentTier=dev AlertTopicArn=<audit topic arn>"
log ""
log "The key changes whenever the handler or the specification changes. A stack"
log "set still running an old key is running old code, however current it looks."
hr
