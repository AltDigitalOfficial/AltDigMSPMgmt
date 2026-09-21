#!/usr/bin/env bash
#
# Package the telemetry pipeline and publish it to the artifact bucket.
#
#   scripts/publish-telemetry.sh [--dry-run]
#
# gap.py is the whole module: it gathers five signals per instance and
# matches them against design 11's discriminator table.

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
> "${BUILD}/.sources"
for MODULE in gap; do
  cp "${REPO_ROOT}/telemetry/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/telemetry/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done
# B-060: a hand-written module list left two handlers out of the archive while
# the stack was already deployed with Lambdas pointing at them.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/telemetry/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "telemetry sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
# One, and it protects the property that would fail most quietly.

info "Checking an unreadable signal is still not a false one"
"${PY}" - "$(win_path "${REPO_ROOT}/telemetry/src/gap.py")" <<'HONESTEOF'
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

problems = []

# A signal that could not be read must never be recorded as False.
#
# Assuming False for an unreadable telemetry check manufactures the exact
# signature this module hunts: the platform reporting an agent killed because
# it could not see the metric. It would fire on a permissions lapse, name a
# developer, and be entirely wrong.
if "unreadable" not in source:
    problems.append(
        "gap.py no longer tracks unreadable signals. An unreadable check "
        "recorded as False manufactures the agent-kill signature.")

if "if signals.unreadable" not in source:
    problems.append(
        "classify() no longer refuses to judge when a signal was unreadable. "
        "Every row of the table is a positive claim about four signals, and "
        "an unknown is not a value any of them take.")

# Exactly two rows of design 11's table alarm. A third would mean somebody
# widened it; zero would mean the detector is inert.
alarms = source.count('"alarm": True')
if alarms != 2:
    problems.append(
        f"{alarms} scenario(s) alarm; design 11's table has exactly two. "
        "Check SCENARIOS against the table before changing this.")

if problems:
    sys.exit("gap.py has lost a control:" + nl
             + nl.join("      - " + p for p in problems))
print("  unreadable stays unreadable; two rows alarm")
HONESTEOF
[[ $? -eq 0 ]] || die "gap.py is not safe to publish."
ok "unreadable is not false"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="telemetry/telemetry-${HASH}.zip"

hr
log "Telemetry package"
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
  --role-session-name publish-telemetry \
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
info "Deploy the pipeline into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template telemetry/10-telemetry.yaml --stack platform-telemetry \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    JiraSite=\${PLATFORM_JIRA_SITE} LogsKeyArn=<alerts key>"
log ""
info "Then the executor role into each target account, passing the"
info "ExecuteRoleArn the stack above outputs:"
log "  scripts/deploy-to-account.sh --account <target> \\"
log "    --template telemetry/00-executor-role.yaml --stack platform-telemetry-executor \\"
log "    TelemetryPipelineRoleArn=<ExecuteRoleArn> Scope=member"
log ""
info "And publish the approver list, which is NOT in the repository:"
log "  scripts/sync-telemetry-approvers.sh"
hr
