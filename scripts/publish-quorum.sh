#!/usr/bin/env bash
#
# Package the quorum pipeline and publish it to the artifact bucket.
#
#   scripts/publish-quorum.sh [--dry-run]
#
# verify.py decides, execute.py acts, reverse.py proves the negative. jira.py
# is the same read-only client reconciliation uses, copied in rather than
# duplicated -- one client, one place where a Jira quirk gets fixed.

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
require_committed change/src quorum/src

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
> "${BUILD}/.sources"
for MODULE in verify execute reverse; do
  cp "${REPO_ROOT}/quorum/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/quorum/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done
# Shared with 8.2. Copied at package time rather than vendored, so a fix to the
# Jira client reaches both pipelines.
cp "${REPO_ROOT}/change/src/jira.py" "${BUILD}/jira.py"
printf '%s\n' "$(win_path "${REPO_ROOT}/change/src/jira.py")" \
  >> "${BUILD}/.sources"

# B-060: a hand-written module list left two handlers out of the archive while
# the stack was already deployed with Lambdas pointing at them.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/quorum/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "quorum sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------
# Each one protects a control that would disappear SILENTLY. That is the test
# for whether a guard earns its place here: not "could this be wrong" but
# "if this were wrong, would anything fail visibly?" Nothing below would.
#
# And each matches the CONSTRUCT it forbids, never a word that describes it.
# Four guards in this codebase have fired on their own explanatory prose.

info "Checking the verifier cannot act"
"${PY}" - "$(win_path "${REPO_ROOT}/quorum/src/verify.py")" <<'VERIFYEOF'
import re
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

# verify.py decides whether an action is permitted. If it could also perform
# one, or write to the evidence table, it could manufacture the record of its
# own decision -- the same circularity that keeps jira.py read-only.
#
# boto3 client METHOD CALLS, not words. `.put_item(` is a construct; "put" in
# a sentence is not.
forbidden = [
    "assume_role(", "put_item(", "update_item(", "delete_item(",
    "schedule_key_deletion(", "delete_backup_vault(", "stop_logging(",
    "delete_trail(", "remove_account_from_organization(",
    "stop_configuration_recorder(", "put_events(",
]
body = nl.join(l for l in source.split(nl) if not l.strip().startswith("#"))
hits = [f for f in forbidden if f in body]
if hits:
    sys.exit("verify.py can act, not just judge: " + ", ".join(hits) + nl
             + "      A component that decides whether an action is permitted"
             + nl + "      must not be able to perform it or to record its own"
             + nl + "      decision.")
print(f"  no acting calls; {len(source.splitlines())} lines checked")
VERIFYEOF
[[ $? -eq 0 ]] || die "verify.py is not safe to publish."
ok "the verifier only judges"

info "Checking the executor still narrows its credentials"
"${PY}" - "$(win_path "${REPO_ROOT}/quorum/src/execute.py")" <<'EXECEOF'
import re
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

problems = []

# THE control. execute.py assumes the executor role with a session policy
# scoped to the single approved resource, so its credentials cannot express a
# request against anything else.
#
# Delete the `Policy=` argument and everything still works -- every test
# passes, the approved action succeeds, and the only difference is that a bug
# passing the wrong ARN would now delete the wrong thing instead of getting
# AccessDenied. Nothing fails visibly. That is exactly what a guard is for.
if not re.search(r"assume_role\([^)]*Policy\s*=", source, re.S):
    problems.append(
        "assume_role is called without a session Policy. The credentials "
        "would carry the executor role's FULL permissions instead of being "
        "narrowed to the approved resource.")

# The session name is what ties every CloudTrail entry to the change record
# that authorised it, and the executor role's trust policy requires the shape.
if not re.search(r"RoleSessionName\s*=", source):
    problems.append(
        "assume_role is called without a RoleSessionName. CloudTrail would "
        "carry no link to the authorising change record, and reverse.py "
        "would report every action as having come from outside the pipeline.")
if 'f"quorum-{' not in source and "'quorum-'" not in source:
    problems.append(
        "the session name no longer embeds the issue key. The trust policy "
        "requires quorum-*, so this would fail closed -- but reverse.py's "
        "whole method depends on the key being IN the name.")

# It must re-verify before acting. The verification that gated the state
# machine can be minutes old.
if "verify.verify(" not in source:
    problems.append(
        "execute.py no longer re-verifies before acting. Time-of-check to "
        "time-of-use: the record could be edited between the gate and the "
        "action.")

# It must never carry a TARGET of its own. Every resource comes from the
# approved record; a literal target ARN here would be something nobody
# approved.
#
# IAM role ARNs are exempt, and the exemption is the point rather than a
# loophole. The one ARN this module legitimately builds is the executor
# role it assumes -- that is infrastructure, not a target. A target is a
# KMS key, a vault, a trail or an account, and none of those is a role.
#
# The first version flagged every arn: literal and tripped on the
# assume_role line, which is the FIFTH guard in this codebase to fire on
# something that describes the offence rather than commits it. The rule
# that keeps being relearned: match the construct, never the shape.
ARN_LITERAL = r"""["'](arn:aws:[^"']*)["']"""

literals = [a for a in re.findall(ARN_LITERAL, source)
            if not a.startswith("arn:aws:iam:")]
if literals:
    problems.append(
        "execute.py names resource ARNs literally: " + ", ".join(literals)
        + ". Targets come from the approved change record, never from code.")

if problems:
    sys.exit("execute.py has lost a control:" + nl
             + nl.join("      - " + p for p in problems))
print("  session policy, session name, re-verification, no literal targets")
EXECEOF
[[ $? -eq 0 ]] || die "execute.py is not safe to publish."
ok "the executor narrows its own credentials"

info "Checking the reverse sweep stays read-only"
"${PY}" - "$(win_path "${REPO_ROOT}/quorum/src/reverse.py")" <<'REVEOF'
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()
body = nl.join(l for l in source.split(nl) if not l.strip().startswith("#"))

# reverse.py reads CloudTrail and reports. It must not be able to "fix"
# anything it finds: an unapproved key deletion is a finding for a human, and
# a sweep that could act on its own findings would be automated remediation
# against the CLAUDE.md rule -- and would be doing so on the strength of a
# CloudTrail parse.
forbidden = ["schedule_key_deletion(", "delete_backup_vault(", "stop_logging(",
             "delete_trail(", "remove_account_from_organization(",
             "stop_configuration_recorder(", "put_item(", "delete_item(",
             "attach_role_policy(", "put_role_policy("]
hits = [f for f in forbidden if f in body]
if hits:
    sys.exit("reverse.py can act on what it finds: " + ", ".join(hits) + nl
             + "      The reverse sweep reports. A finding is for a human.")
print(f"  reports only; {len(body.splitlines())} lines checked")
REVEOF
[[ $? -eq 0 ]] || die "reverse.py is not safe to publish."
ok "the reverse sweep reports and does not act"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="quorum/quorum-${HASH}.zip"

hr
log "Quorum package"
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
  --role-session-name publish-quorum \
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
log "    --template quorum/10-quorum.yaml --stack platform-quorum \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    JiraSite=\${PLATFORM_JIRA_SITE} LogsKeyArn=<alerts key>"
log ""
info "Then the executor role into each target account, passing the"
info "ExecuteRoleArn the stack above outputs:"
log "  scripts/deploy-to-account.sh --account <target> \\"
log "    --template quorum/00-executor-role.yaml --stack platform-quorum-executor \\"
log "    QuorumPipelineRoleArn=<ExecuteRoleArn> Scope=member"
log ""
info "And publish the approver list, which is NOT in the repository:"
log "  scripts/sync-quorum-approvers.sh"
hr
