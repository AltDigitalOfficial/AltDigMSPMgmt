#!/usr/bin/env bash
#
# Package and upload the HOOP scheduler and its cycle verification.
#
#   scripts/publish-scheduling.sh [--dry-run]
#
# Two handlers in one archive: hoop.handler schedules, cycle.handler verifies.
# They share the manifest and the refusals, so shipping them apart would allow
# a verification running against a scheduler it does not match — and the
# mismatch would surface as a turnup reported incomplete for resources the
# running scheduler never touched.
#
# Written rather than copied from publish-commercial.sh. B-104: a publish
# script copied and renamed only in its head uploaded one component's package
# under another's name, and nothing noticed because both archives existed.

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
require_committed scheduling/src

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

for MODULE in hoop cycle state interactions; do
  cp "${REPO_ROOT}/scheduling/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
done

# B-060: the templates are the authority on which handlers must be present.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z_]+\.handler' \
                   "${REPO_ROOT}"/scheduling/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
"${PY}" -m py_compile \
  "$(win_path "${REPO_ROOT}/scheduling/src/hoop.py")" \
  "$(win_path "${REPO_ROOT}/scheduling/src/cycle.py")" \
  || die "scheduling sources do not compile. Nothing packaged."
ok "compiles"

# --- the structural check --------------------------------------------------
#
# CLAUDE.md forbids automation that terminates or purges. The scheduler is
# allowed to stop things precisely because stopping is reversible, and that
# distinction lives one word away from its opposite: stop_instances and
# terminate_instances differ by a verb.
#
# The IAM role already withholds the destructive actions, so a call would fail
# at runtime. This refuses to PACKAGE one, because a failing destructive call
# in a scheduler is still a scheduler somebody wrote a destructive call into.
info "Checking nothing destructive was packaged"
"${PY}" - "$(win_path "${REPO_ROOT}/scheduling/src")" <<'SEPEOF'
import pathlib
import re
import sys

FORBIDDEN = (
    "terminate_instances",
    "delete_db_instance",
    "delete_db_cluster",
    "delete_auto_scaling_group",
    "delete_service",
    "delete_volume",
    "deregister_",
)

problems = []
for path in pathlib.Path(sys.argv[1]).glob("*.py"):
    source = path.read_text(encoding="utf-8")
    # Strip comments and docstrings so the words may be DISCUSSED. Several of
    # these appear in prose explaining why they are absent, and a guard that
    # fired on its own rationale would be removed within the week -- that
    # lesson cost six false positives elsewhere in this repository.
    code = re.sub(r"#.*", "", source)
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)
    for name in FORBIDDEN:
        if re.search(r"\.\s*" + re.escape(name) + r"\s*\(", code):
            problems.append(f"{path.name} calls {name}()")

if problems:
    print("  " + "\n  ".join(problems))
    print("")
    print("  A HOOP stops compute; it never destroys it. CLAUDE.md: security")
    print("  automation isolates and preserves, operational automation is")
    print("  additive or reversible. Storage, load balancers and NAT gateways")
    print("  persist through a turndown by design.")
    sys.exit(1)
SEPEOF
[[ $? -eq 0 ]] || die "a destructive call is present in scheduling/src. Nothing packaged."
ok "stop and start only; nothing destructive"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="scheduling/scheduling-${HASH}.zip"

hr
log "Scheduling package"
log "  account : altdig-infra-tooling (${TOOLING})"
log "  key     : ${KEY}"
log "  size    : $(wc -c < "${BUILD}/package.zip" | tr -d ' ') bytes"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would upload to s3://%s/%s\n' \
    "${C_YELLOW}" "${C_RESET}" "${BUCKET}" "${KEY}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name publish-scheduling \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  # win_path on the body. A bare MSYS path reaches aws.exe as something it
  # cannot open, and the error -- "Blob values must be a path to a file" --
  # describes a file that exists and is perfectly readable.
  MSYS_NO_PATHCONV=1 aws s3api put-object \
    --bucket "${BUCKET}" --key "${KEY}" \
    --body "$(win_path "${BUILD}/package.zip")" \
    --query ETag --output text >/dev/null
)
ok "uploaded s3://${BUCKET}/${KEY}"

hr
info "Next: deploy into the account being scheduled"
log "  scripts/deploy-to-account.sh --account <alias> \\"
log "    --template scheduling/10-hoop.yaml --stack platform-hoop \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    AccountId=<id> AccountTier=<dev|test|uat> AccountKind=<tenant|shared> \\"
log "    AlertBusArn=<arn> HoopConfig='{\"timezone\":\"America/Chicago\",...}'"
log ""
log "  AccountTier does not accept prod. That refusal is CloudFormation's,"
log "  before any code runs."
hr
