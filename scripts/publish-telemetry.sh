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
# Only committed sources are packaged: what runs must be a commit someone
# can name. lib/common.sh, require_committed.
require_committed telemetry/src

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

# The exact set of scenarios that alarm, by name, each with its source.
#
#   cloudwatch-agent-killed            design 11's table, "CloudWatch agent killed"
#   both-agents-killed-or-path-broken  design 11's table, "Both agents killed"
#   instance-stopped-off-schedule      design 14 / prompt 13.4 (a8d6dbf): a stop
#                                      the platform did not perform is reported,
#                                      not treated like a scheduled turndown
#
# This used to count to two, from design 11 alone. a8d6dbf added the third on
# purpose, and the count made publishing impossible from 2026-09-21 (B-145). A
# count was also the weaker check: swapping one alarming row for another kept
# it at two. A set catches a widening, a removal and a swap, and names which.
import re
EXPECTED = {"cloudwatch-agent-killed", "both-agents-killed-or-path-broken",
            "instance-stopped-off-schedule"}
alarming = {n for n, a in re.findall(r'"name":\s*"([^"]+)".*?"alarm":\s*(True|False)',
                                     source, re.S) if a == "True"}
if alarming != EXPECTED:
    problems.append(
        "the scenarios that alarm are " + (", ".join(sorted(alarming)) or "none")
        + "; expected " + ", ".join(sorted(EXPECTED))
        + ". Widened: " + (", ".join(sorted(alarming - EXPECTED)) or "none")
        + ". Missing: " + (", ".join(sorted(EXPECTED - alarming)) or "none")
        + ". Check SCENARIOS against design 11's table and design 14 before changing this.")

if problems:
    sys.exit("gap.py has lost a control:" + nl
             + nl.join("      - " + p for p in problems))
print("  unreadable stays unreadable; the three expected rows alarm")
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
# These instructions named telemetry/10-telemetry.yaml, an executor role and
# an approver sync, none of which exist. They had been copied from the quorum
# component. Found 2026-10-02 (B-145). The live stack is platform-telemetry,
# built from 10-telemetry-gap.yaml, and the per-account role is the reader.
info "Deploy the scan into the Audit account (other parameters keep their values):"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template telemetry/10-telemetry-gap.yaml --stack platform-telemetry \\"
log "    ArtifactKey=${KEY}"
log ""
info "The read-only role it assumes in each account is a separate template:"
log "  telemetry/00-telemetry-reader.yaml  (TelemetryScanRoleArn = the scan's role)"
hr
