#!/usr/bin/env bash
#
# Package declaration and containment, and publish to the artifact bucket.
#
#   scripts/publish-containment.sh [--dry-run]
#
# One package, five handlers: declare.py creates the incident, contain.py runs
# the sequence, readout.py assembles the dossier, clock.py counts the
# notification deadlines, and exercise.py drills the whole thing monthly. All
# five read or write the same append-only incident record, so they are
# deployed together and cannot drift apart.

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
for MODULE in contain declare readout clock exercise; do
  cp "${REPO_ROOT}/containment/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/containment/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done

# Every handler the template names, checked against what is actually in the
# package. This list was previously written out by hand and a silent edit
# failure left readout.py and clock.py out of the archive while the stack had
# already been deployed with Lambdas pointing at them -- functions that would
# have failed at invocation with "Unable to import module", during an incident.
# EVERY containment template, not one of them. Reading 10-containment.yaml
# alone reproduced the same shape of gap as the hand-maintained module list it
# replaced: exercise.handler is declared in 40-exercise.yaml and went unchecked.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/containment/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the template declares handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they all compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "containment sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# The state machine names six stages. One it does not implement fails at RUN
# time -- during an incident -- with "unknown step". Checked here instead.
info "Checking the state machine's stages exist in contain.py"
"${PY}" - "$(win_path "${REPO_ROOT}/containment/10-containment.yaml")" \
          "$(win_path "${REPO_ROOT}/containment/src/contain.py")" <<'CHECKEOF'
import re
import sys

asl = open(sys.argv[1], encoding="utf-8").read()
src = open(sys.argv[2], encoding="utf-8").read()

wanted = set(re.findall(r'"step":\s*"([a-z_]+)"', asl))
block = re.search(r"STEPS = \{(.*?)\}", src, re.S)
if block is None:
    sys.exit("could not find STEPS in contain.py")
have = set(re.findall(r'"([a-z_]+)":', block.group(1)))

missing = wanted - have
if missing:
    sys.exit("state machine calls stages contain.py does not implement: "
             + ", ".join(sorted(missing)))
print(f"  {len(wanted)} stage(s) matched: {', '.join(sorted(wanted))}")
unused = have - wanted
if unused:
    print(f"  note: unused stage(s): {', '.join(sorted(unused))}")
CHECKEOF
[[ $? -eq 0 ]] || die "state machine and contain.py have diverged."
ok "state machine and contain.py agree"

# The whole security argument of the responder role is that it cannot destroy
# evidence. That is a property of a list, and a list is easy to extend without
# noticing. Checked on every publish rather than trusted to review.
info "Checking the responder role grants nothing destructive"
"${PY}" - "$(win_path "${REPO_ROOT}/containment/00-responder-role.yaml")" <<'DENYEOF'
import sys

forbidden = (
    "TerminateInstances", "DeleteSnapshot", "DeleteVolume", "StopInstances",
    "DeleteRole", "DeleteUser", "DeleteRolePolicy", "DeleteUserPolicy",
    "DeleteDBInstance", "DeleteObject", "DeleteBucket", "DeleteLogGroup",
    "DeleteTrail", "StopLogging", "ScheduleKeyDeletion", "DeleteBackupVault",
    "DeleteRecoveryPoint", "DeregisterImage",
)
# Comments out: the header promises these are absent, in prose, using the very
# words being searched for.
body = "\n".join(l for l in open(sys.argv[1], encoding="utf-8").read().split("\n")
                 if not l.strip().startswith("#"))
hits = [f for f in forbidden if f in body]
if hits:
    sys.exit("responder role grants destructive action(s): " + ", ".join(hits)
             + "\n      design 07: automation may never terminate, delete, "
               "deregister, roll back or purge.")
print(f"  none of {len(forbidden)} destructive actions present")
DENYEOF
[[ $? -eq 0 ]] || die "responder role is not safe to publish."
ok "responder role holds nothing destructive"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="containment/containment-${HASH}.zip"

hr
log "Containment package"
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
  --role-session-name publish-containment \
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
info "1. Deploy containment into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template containment/10-containment.yaml --stack platform-containment \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    AlertTopicArn=<audit alerts topic>"
log ""
info "2. Then the member-side responder role, which trusts the role that"
log "   stack just created:"
log "  scripts/deploy-stackset.sh --name platform-security-responder \\"
log "    --template containment/00-responder-role.yaml --ou sandbox \\"
log "    ContainmentRoleArn=<ContainmentRoleArn output from step 1> \\"
log "    ForensicsAccountId=<forensics account>"
hr
log "Order matters: the responder role names the containment role in its trust"
log "policy, and IAM rejects a trust policy naming a principal that does not"
log "exist yet."
hr
