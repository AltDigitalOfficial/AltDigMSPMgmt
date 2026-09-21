#!/usr/bin/env bash
#
# Package the commercial pipeline and publish it to the artifact bucket.
#
#   scripts/publish-commercial.sh [--dry-run]
#
# route.py is the whole module: design 12's fourth destination, and the
# structural separation between being informed and being the notifier.

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
for MODULE in route notify; do
  cp "${REPO_ROOT}/commercial/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/commercial/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done
# B-060: a hand-written module list left two handlers out of the archive while
# the stack was already deployed with Lambdas pointing at them.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/commercial/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "commercial sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
# design 12 asks that informed and notifier stay separate "or the distinction
# erodes the first time someone is being helpful under pressure". A convention
# erodes; this checks the structure that replaces it.

info "Checking informed is still structurally separate from notifier"
"${PY}" - "$(win_path "${REPO_ROOT}/commercial/src/route.py")" <<'SEPEOF'
import re
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

problems = []

# The awareness filter must be an ALLOW-list. A deny-list lets a field added
# upstream later flow straight through to the commercial channel -- and the
# fields added later are exactly the ones an incident turned out to need.
m = re.search(r"AWARENESS_FIELDS = \((.*?)\)", source, re.S)
if not m:
    problems.append(
        "AWARENESS_FIELDS has gone. Without an allow-list, a security incident "
        "reaches the commercial channel whole and somebody can notify from it.")
else:
    allowed = m.group(1)
    for leaky in ("affected", "individuals", "classification", "blast",
                  "principal", "finding", "objects"):
        if leaky in allowed:
            problems.append(
                f"'{leaky}' appears in AWARENESS_FIELDS. That is notifiable "
                "detail and must not reach the commercial channel.")

# to_awareness must build from the allow-list, not copy and delete.
if "to_awareness" not in source:
    problems.append("to_awareness has gone; nothing strips the incident.")
elif re.search(r"def to_awareness.*?del", source, re.S):
    problems.append(
        "to_awareness deletes fields rather than selecting them. Deleting is a "
        "deny-list wearing different clothes: a new field survives it.")

# Refusing to emit without a notifier is the other half of the structure.
if "NotifierUnknown" not in source:
    problems.append(
        "the router no longer refuses to emit when the notifier is unknown. An "
        "awareness event naming nobody leaves the reader as the only candidate.")

if problems:
    sys.exit("route.py has lost the separation:" + nl
             + nl.join("      - " + p for p in problems))
print("  allow-list intact; refuses to emit without a named notifier")
SEPEOF
[[ $? -eq 0 ]] || die "route.py is not safe to publish."
ok "informed is not notifier"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="commercial/commercial-${HASH}.zip"

hr
log "Commercial package"
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
  --role-session-name publish-commercial \
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
info "Deploy into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template commercial/10-commercial-routing.yaml \\"
log "    --stack platform-commercial \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    LogsKeyArn=<alerts key>"
log ""
info "Then name the notifier, which the router refuses to work without:"
log "  aws ssm put-parameter --name /platform/commercial/notifier \\"
log "    --type String --value '"'"'{\"role\": \"contract-owner\"}'"'"'"
log ""
log "  A ROLE, not a person. design 12 assigns notification to contract"
log "  owners; a name here goes stale and is personal data besides."
hr
