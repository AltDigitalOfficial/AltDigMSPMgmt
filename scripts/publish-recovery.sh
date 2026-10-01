#!/usr/bin/env bash
#
# Package the restore workflow and publish it to the artifact bucket.
#
#   scripts/publish-recovery.sh [--dry-run]
#
# One package, two functions. worker.py and teardown.py share an archive and
# are separated by handler and by ROLE, not by artifact — see
# recovery/10-restore-workflow.yaml on why the function that can delete has a
# role of its own. Shipping them separately would mean two content hashes that
# can drift, and a teardown built against a worker it has never seen.

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
require_committed recovery/src

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
cp "${REPO_ROOT}/recovery/src/worker.py" "${BUILD}/worker.py"
cp "${REPO_ROOT}/recovery/src/teardown.py" "${BUILD}/teardown.py"

info "Checking both handlers compile"
"${PY}" -m py_compile \
  "$(win_path "${REPO_ROOT}/recovery/src/worker.py")" \
  "$(win_path "${REPO_ROOT}/recovery/src/teardown.py")" \
  || die "recovery sources do not compile. Nothing packaged."
ok "compiles"

# The state machine names five worker steps and invokes teardown separately. A
# state machine that references a step the worker does not implement fails at
# RUN time, in the middle of a restore, with "unknown step" — and in recovery
# mode that is during an outage. Checked here instead, where it costs nothing.
info "Checking the state machine's steps exist in the worker"
"${PY}" - "$(win_path "${REPO_ROOT}/recovery/10-restore-workflow.yaml")" \
          "$(win_path "${REPO_ROOT}/recovery/src/worker.py")" <<'CHECKEOF'
import re
import sys

asl = open(sys.argv[1], encoding="utf-8").read()
src = open(sys.argv[2], encoding="utf-8").read()

wanted = set(re.findall(r'"step":\s*"([a-z]+)"', asl))
block = re.search(r"STEPS = \{(.*?)\}", src, re.S)
if block is None:
    sys.exit("could not find STEPS in worker.py")
have = set(re.findall(r'"([a-z]+)":', block.group(1)))

missing = wanted - have
unused = have - wanted
if missing:
    sys.exit(f"state machine calls steps the worker does not implement: "
             f"{', '.join(sorted(missing))}")
print(f"  {len(wanted)} step(s) matched: {', '.join(sorted(wanted))}")
if unused:
    # Not fatal. A worker step no state calls is dead code, not a broken
    # workflow, and saying so is more useful than refusing to publish.
    print(f"  note: worker implements unused step(s): {', '.join(sorted(unused))}")
CHECKEOF
[[ $? -eq 0 ]] || die "state machine and worker have diverged. Nothing packaged."
ok "state machine and worker agree"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="recovery/recovery-${HASH}.zip"

hr
log "Recovery package"
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
  --role-session-name publish-recovery \
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
info "1. The sandbox test fixture (sandbox only — it is scaffolding, not a"
log "   backup policy; see the header of recovery/00-restore-fixture.yaml):"
log "  scripts/deploy-to-account.sh --account altdig-sandbox-canary \\"
log "    --template recovery/00-restore-fixture.yaml \\"
log "    --stack platform-restore-fixture"
log ""
info "2. Take the first recovery point, so there is something to restore:"
log "  scripts/seed-restore-fixture.sh"
log ""
info "3. The workflow:"
log "  scripts/deploy-to-account.sh --account altdig-sandbox-canary \\"
log "    --template recovery/10-restore-workflow.yaml \\"
log "    --stack platform-restore \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    EnvironmentTier=dev BackupVaultName=platform-restore-fixture \\"
log "    SourceResourceArn=<volume arn from the fixture stack> \\"
log "    TestVpcId=<isolated vpc> TestSubnetIds=<subnet>"
log ""
info "4. The schedule (uat and prod only — design/14 line 85):"
log "  scripts/deploy-to-account.sh --account altdig-sandbox-canary \\"
log "    --template recovery/20-restore-schedule.yaml \\"
log "    --stack platform-restore-schedule \\"
log "    StateMachineArn=<from the workflow stack> EnvironmentTier=uat"
hr
log "The thresholds are PLACEHOLDERS until open-items.md B3 is closed. Every"
log "record the workflow emits says so; do not quote a duration result as a"
log "commitment before then."
hr
