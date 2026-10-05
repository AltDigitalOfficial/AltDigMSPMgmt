#!/usr/bin/env bash
#
# Package HIPAA eligibility detection and publish it to the artifact bucket.
#
#   scripts/publish-eligibility.sh [--dry-run]
#
# One package, two handlers (sweep, upstream), and the catalog itself. The
# catalog travels INSIDE the package rather than being read from S3 or the
# repository at run time, so that what a deployed function judges against is a
# commit someone can name — the same reason only committed sources are
# packaged. Changing the catalog therefore means republishing, which is the
# point: a catalog edit is a security review, and it reaches production through
# the same door as code. docs/hipaa-eligibility.md §1.

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

require_committed eligibility

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] || die "Platform Tooling account not registered."
BUCKET="altdig-platform-artifacts-${TOOLING}"

# A catalog that does not validate never reaches a function. Offline is not
# accepted here: a prefix typo is exactly what the online check exists to find.
info "Validating the catalog"
"${PY}" "$(win_path "${REPO_ROOT}/scripts/validate-hipaa-eligibility.py")" \
  || die "The HIPAA eligibility catalog does not validate. Nothing packaged."

# The developer guide carries the same lists, generated. A catalog change that
# reached the sweep without the guide would tell developers one thing and deny
# them another.
"${PY}" "$(win_path "${REPO_ROOT}/scripts/render-hipaa-eligibility-guide.py")" --check   || die "docs/developer-guide.md is stale. Run scripts/render-hipaa-eligibility-guide.py and commit."

BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

> "${BUILD}/.sources"
for MODULE in catalog state sweep upstream tenant_jira; do
  cp "${REPO_ROOT}/eligibility/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/eligibility/src/${MODULE}.py")" >> "${BUILD}/.sources"
done
cp "${REPO_ROOT}/eligibility/hipaa-eligible-services.tsv" "${BUILD}/hipaa-eligible-services.tsv"

# B-060: every handler a template names must be in the archive.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' "${REPO_ROOT}"/eligibility/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "a template declares ${HANDLER}.handler but ${HANDLER}.py is not packaged."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" || die "eligibility sources do not compile."
rm -f "${BUILD}/.sources"
ok "compiles"

info "Running the eligibility tests"
"${PY}" "$(win_path "${REPO_ROOT}/scripts/test-hipaa-eligibility.py")" >/dev/null 2>&1 \
  || die "scripts/test-hipaa-eligibility.py fails. Run it for the detail. Nothing packaged."
ok "tests pass"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="eligibility/eligibility-${HASH}.zip"

hr
log "HIPAA eligibility package"
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
  --role-session-name publish-eligibility \
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
info "Deploy detection into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template eligibility/10-hipaa-eligibility.yaml --stack platform-hipaa-eligibility \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY}"
log ""
info "The reader each HIPAA client's accounts carry (one StackSet, client OUs only):"
log "  scripts/deploy-stackset.sh --name platform-hipaa-eligibility-reader \\"
log "    --template eligibility/00-hipaa-eligibility-reader.yaml \\"
log "    --ou client:<partner>/<client> --confirm-production AuditAccountId=<audit>"
log "  (further clients: --add-ou; scripts/sync-hipaa-accounts.sh lists them)"
hr
