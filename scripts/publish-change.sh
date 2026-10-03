#!/usr/bin/env bash
#
# Package change reconciliation and publish it to the artifact bucket.
#
#   scripts/publish-change.sh [--dry-run]
#
# One package: reconcile.py captures and compares, jira.py is the read-only
# client both it and the quorum pipeline (8.3) use.

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
require_committed change/src

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
> "${BUILD}/.sources"
for MODULE in reconcile jira filer; do
  cp "${REPO_ROOT}/change/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/change/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done

# Derived from the templates, not maintained by hand -- B-060, where a
# hand-written module list left two handlers out of the archive while the stack
# was already deployed with Lambdas pointing at them.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/change/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package. Add it to the MODULE list above."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "change sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# The Jira client's whole safety argument is that it cannot write. A write
# method added later would be invisible in review of a large diff and would
# make the quorum control circular -- a credential able to edit issues can
# manufacture the approval it then verifies.
info "Checking the Jira client is still read-only"
"${PY}" - "$(win_path "${REPO_ROOT}/change/src/jira.py")" <<'READONLYEOF'
import re
import sys

source = open(sys.argv[1], encoding="utf-8").read()

# Definitions matched at the START OF A LINE, not as substrings.
#
# The substring form false-positived on the docstring that EXPLAINS this
# guard: prose containing "def create_" tripped a check meant to find a
# function named create_something. A guard that fires on a comment about
# itself is one somebody works around rather than fixes, which is worse than
# not having it. Same lesson as the containment role check -- strip the prose,
# test the code.
definitions = re.findall(r"^def\s+(\w+)", source, re.M)
write_shaped = [d for d in definitions
                if d.startswith(("create_", "update_", "delete_", "transition",
                                 "add_comment", "post_", "put_"))]

# HTTP methods stay a substring test: urllib takes the method as a string
# argument, so there is no line-anchored form to match.
body = "\n".join(l for l in source.split("\n")
                 if not l.strip().startswith("#"))
methods = [m for m in ('method="POST"', "method='POST'", 'method="PUT"',
                       'method="DELETE"', 'method="PATCH"')
           if m in body]

hits = write_shaped + methods
if hits:
    sys.exit("jira.py now contains write-shaped code: " + ", ".join(hits)
             + "\n      A credential that can edit issues can manufacture the"
               "\n      approval the quorum pipeline then verifies.")
print(f"  {len(definitions)} function(s), none write-shaped")
READONLYEOF
[[ $? -eq 0 ]] || die "jira.py is not safe to publish."
ok "jira client is read-only"

# filer.py is ALLOWED to write -- that is its job. The line it may not cross is
# approving: filing a change and approving one are different acts, and if the
# platform could do both, the record it files would prove nothing.
info "Checking the filer cannot approve anything"
"${PY}" - "$(win_path "${REPO_ROOT}/change/src/filer.py")" <<'NOAPPROVEEOF'
import re
import sys

# API PATHS, not English.
#
# The first version searched for the word "approve" anywhere in the file and
# matched the docstring explaining why approving is forbidden. That is the
# third time in this session a guard has fired on prose describing itself --
# jira.py's read-only check twice, and now this. The rule that finally
# generalises: a guard must match the CONSTRUCT it forbids, never a word that
# describes it.
#
# What the filer must never do is issue a request against an approval or
# transition endpoint, so the thing to examine is the set of API paths it
# names. Every one goes to _post or _get as a literal beginning "/rest/".
nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()
paths = re.findall(r'["\'](/rest/[^"\']*)["\']', source)
#
# The first version also banned the whole servicedeskapi NAMESPACE, which
# was the same over-matching in a new costume: creating a JSM request and
# approving one both live under servicedeskapi, and banning the namespace
# banned the filer's actual job. A change record created through the plain
# issue API is not a JSM request at all -- no Request Type, so every
# servicedeskapi endpoint including approvals returns 404 for it -- which
# made the records unverifiable. That is the opposite of what this guard
# is for.
#
# Match the ACT, not the address space. Approving and transitioning are the
# forbidden acts. Creating is not.
bad = [p for p in paths
       if "approval" in p or "/transitions" in p
       or p.rstrip("/").endswith(("/approve", "/decline"))]
if bad:
    sys.exit("filer.py calls an approval or transition endpoint: "
             + ", ".join(bad) + nl
             + "      The platform may FILE a change and may not APPROVE one."
             + nl
             + "      A component that can do both proves nothing by doing"
             + nl + "      either.")
print(f"  {len(paths)} API path(s), none approval or transition")
NOAPPROVEEOF
[[ $? -eq 0 ]] || die "filer.py is not safe to publish."
ok "filer cannot approve"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="change/change-${HASH}.zip"

hr
log "Change package"
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
  --role-session-name publish-change \
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
info "Deploy reconciliation into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template change/10-reconciliation.yaml --stack platform-change \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    JiraSite=\${PLATFORM_JIRA_SITE} \\"
log "    JiraDefaultChangeProject=\${PLATFORM_JIRA_DEFAULT_CHANGE_PROJECT} \\"
log "    LogsKeyArn=<alerts key>"
log ""
info "Then publish the tenant facts the engine reads:"
log "  scripts/sync-tenant-facts.sh"
hr
