#!/usr/bin/env bash
#
# Package the evidence pipeline and publish it to the artifact bucket.
#
#   scripts/publish-evidence.sh [--dry-run]
#
# collect.py numbers, sender.py delivers, completeness.py proves nothing was
# dropped, record.py is the shape they share.

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
require_committed evidence/src

PY="$(command -v python || command -v python3)" || die "python not found."
TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling account not registered."

BUCKET="altdig-platform-artifacts-${TOOLING}"
BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
> "${BUILD}/.sources"
for MODULE in record collect sender completeness custody; do
  cp "${REPO_ROOT}/evidence/src/${MODULE}.py" "${BUILD}/${MODULE}.py"
  printf '%s\n' "$(win_path "${REPO_ROOT}/evidence/src/${MODULE}.py")" \
    >> "${BUILD}/.sources"
done

# B-060: a hand-written module list left two handlers out of the archive while
# the stack was already deployed with Lambdas pointing at them.
info "Checking every handler the templates reference is packaged"
for HANDLER in $(grep -hoE '^      Handler: [a-z]+\.handler' \
                   "${REPO_ROOT}"/evidence/*.yaml \
                 | awk '{print $2}' | cut -d. -f1 | sort -u); do
  [[ -f "${BUILD}/${HANDLER}.py" ]] \
    || die "the templates declare handler ${HANDLER}.handler but
      ${HANDLER}.py is not in the package."
  printf '  %s.py\n' "${HANDLER}"
done
ok "every referenced handler is present"

info "Checking they compile"
xargs "${PY}" -m py_compile < "${BUILD}/.sources" \
  || die "evidence sources do not compile. Nothing packaged."
rm -f "${BUILD}/.sources"
ok "compiles"

# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------
# Each protects a property whose loss would leave a pipeline that looks
# healthier than it is -- which is the exact failure design 04 names. None of
# them would fail a test or an alarm if removed.
#
# Each matches the CONSTRUCT, never a word describing it. Six guards in this
# codebase have now fired on something that resembles the offence rather than
# committing it -- prose describing the rule, an ARN that was infrastructure
# rather than a target, and below, a function DEFINITION mistaken for a call.

info "Checking the sequence is still allocated on ingest"
"${PY}" - "$(win_path "${REPO_ROOT}/evidence/src/record.py")" <<'SEQEOF'
import re
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

# `build` must allocate. If a future edit moved allocation to delivery, the
# sequence would become contiguous by construction and a gap would be
# impossible to observe -- every stream perfect, especially the lossy ones.
#
# Matched inside the function body rather than anywhere in the file, because
# the docstring discusses allocation at length.
match = re.search(r"^def build\(.*?(?=^def |\Z)", source, re.M | re.S)
if not match:
    sys.exit("record.build has gone. Nothing else numbers evidence.")
body = nl.join(l for l in match.group(0).split(nl)
               if not l.strip().startswith("#"))
if "allocate(" not in body:
    sys.exit("record.build no longer allocates a sequence number." + nl
             + "      Allocating anywhere later makes the sequence contiguous"
             + nl + "      by construction, and a dropped record leaves no"
             + nl + "      gap. The stream reads clean when it is lossy.")
print("  build() allocates")
SEQEOF
[[ $? -eq 0 ]] || die "record.py is not safe to publish."
ok "sequence allocated on ingest"

info "Checking the ledger is written before the queue"
"${PY}" - "$(win_path "${REPO_ROOT}/evidence/src/collect.py")"          "$(win_path "${REPO_ROOT}/evidence/src/custody.py")" <<'ORDEREOF'
import os
import re
import sys

nl = chr(10)
# Every module that creates evidence records, not only the collector. custody.py
# joined on 2026-09-23 and owes the same ordering for the same reason. Checked
# inside the function that does the write (custody.py's _write), because
# custody.py also calls put_item for its done-markers, which come AFTER.
for path in sys.argv[1:]:
    name = os.path.basename(path)
    source = open(path, encoding="utf-8").read()
    body = nl.join(l for l in source.split(nl) if not l.strip().startswith("#"))
    fn = re.search(r"^def _write\(.*?(?=^def |\Z)", body, re.M | re.S)
    if fn:
        body = fn.group(0)
    put = body.find("put_item(")
    send = body.find("send_message(")
    if put < 0:
        sys.exit(name + " no longer writes a ledger row. A record with no row"
                 + nl + "      cannot be missed, because nothing knows it exists.")
    if send >= 0 and send < put:
        sys.exit(name + " queues before writing the ledger." + nl
                 + "      A crash between the two would leave a record in flight"
                 + nl + "      with no row behind it: delivered or not, nothing"
                 + nl + "      would ever notice its absence.")
    print("  " + name + ": ledger write precedes the queue send")
ORDEREOF
[[ $? -eq 0 ]] || die "an evidence producer is not safe to publish."
ok "ledger precedes queue"

info "Checking the sender will not claim a delivery it did not make"
"${PY}" - "$(win_path "${REPO_ROOT}/evidence/src/sender.py")" <<'SENDEOF'
import re
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()
body = nl.join(l for l in source.split(nl) if not l.strip().startswith("#"))

problems = []

# A 200 is not enough. The response must say `accepted: true` and carry a
# `submission_id`, because a proxy or a misconfigured stub returns 200 for
# things that never reached a datastore -- and this acknowledgement is what
# stops the gap detector looking at that record forever. (Was `stored: true`
# until the contract was reconciled with Truveon's endpoint, 2026-09-23.)
for field in ("accepted", "submission_id"):
    if f'"{field}"' not in body and f"'{field}'" not in body:
        problems.append(
            f"the sender no longer checks Truveon's `{field}` before marking a "
            "record delivered. A bare 200 would mark records delivered, and a "
            "delivered row is excluded from gap detection permanently.")

# One key per tenant, chosen by the record's tenant. A sender that fell back to
# a single shared key would write every tenant's evidence into whichever tenant
# that key belongs to -- Truveon derives the tenant from the key.
if "ingest_key(tenant)" not in body:
    problems.append(
        "the sender no longer selects the ingest key by the record's tenant.")

# With no endpoint, nothing may be marked delivered: the
# `not TRUVEON_ENDPOINT` branch has to return before any _mark_delivered CALL.
#
# Checked inside send() only. The first version searched the whole file and
# matched the DEFINITION of _mark_delivered, which sits above send() as any
# helper would -- so it reported a control lost on correct code. Sixth guard in
# this codebase to match a shape instead of a construct; the definition and the
# call look identical to a substring search and mean opposite things.
send_body = re.search(r"^def send\(.*?(?=^def |\Z)", body, re.M | re.S)
send_body = send_body.group(0) if send_body else ""
no_endpoint = send_body.find("if not TRUVEON_ENDPOINT")
first_mark = send_body.find("_mark_delivered(")
if not send_body:
    problems.append("send() has gone from sender.py.")
elif no_endpoint < 0:
    problems.append(
        "the sender no longer has a no-endpoint branch. With TRUVEON_ENDPOINT "
        "unset it would attempt delivery against an empty URL and the failure "
        "path would decide what happens -- rather than the pipeline stating "
        "plainly that it is not delivering. B-088.")
elif first_mark >= 0 and first_mark < no_endpoint:
    problems.append(
        "a record can be marked delivered before the no-endpoint check.")

if problems:
    sys.exit("sender.py has lost a control:" + nl
             + nl.join("      - " + p for p in problems))
print("  storage confirmed before delivery is recorded")
SENDEOF
[[ $? -eq 0 ]] || die "sender.py is not safe to publish."
ok "delivery is only claimed when confirmed"

info "Checking an unanswerable question is not reported as a clean result"
"${PY}" - "$(win_path "${REPO_ROOT}/evidence/src/completeness.py")" <<'HONESTEOF'
import sys

nl = chr(10)
source = open(sys.argv[1], encoding="utf-8").read()

# B-042, B-054, B-080, B-086. Four times in this codebase an unreadable source
# has been reported as a negative result. The tenant list is this module's
# version of that trap: zero tenants checked must not read as zero problems.
if "TenantsUnknown" not in source:
    sys.exit("completeness.py no longer distinguishes an unknown tenant list"
             + nl + "      from a healthy one. A run over zero tenants writes"
             + nl + "      zero heartbeats and reports success, which is"
             + nl + "      indistinguishable from a perfect estate.")
if '"compared": False' not in source and "'compared': False" not in source:
    sys.exit("the count reconciliation no longer reports when it could NOT"
             + nl + "      compare. Silence from Truveon is not agreement.")
print("  unknown tenants and unreachable Truveon both report as unanswered")
HONESTEOF
[[ $? -eq 0 ]] || die "completeness.py is not safe to publish."
ok "unanswerable is not reported as clean"

info "Packaging"
zip_deterministic "${BUILD}"
HASH="$(package_hash "${BUILD}/package.zip")"
KEY="evidence/evidence-${HASH}.zip"

hr
log "Evidence package"
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
  --role-session-name publish-evidence \
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
LOGARCHIVE_ACCT="$(get_param /org/account/altdig-security-logarchive)"
ORG_ID="$(get_param /org/id)"
info "Deploy into the Audit account:"
log "  scripts/deploy-to-account.sh --account altdig-security-audit \\"
log "    --template evidence/10-evidence.yaml --stack platform-evidence \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    LogArchiveBucketName=altdig-log-archive-${LOGARCHIVE_ACCT} \\"
log "    ConfigArchiveBucketName=altdig-config-archive-${LOGARCHIVE_ACCT} \\"
log "    OrganizationId=${ORG_ID}"
log ""
log "  Log custody also needs the archive buckets to grant PlatformEvidenceCustody"
log "  s3:ListBucket: redeploy security/10-log-archive.yaml with AuditAccountId set."
log ""
info "To start delivering, add TruveonEndpoint=https://app.truveon.ai."
log "  Each tenant also needs its ingest key at platform/truveon/ingest/<tenant>"
log "  (scripts/truveon-tenant-handoff.sh). A tenant without one stays pending"
log "  and is named in a platform-evidence-no-credential finding; nothing is"
log "  marked delivered that was not. docs/truveon-ingestion-contract.md"
log ""
info "Then publish the tenant map, which is not in the repository:"
log "  scripts/sync-evidence-tenants.sh"
hr
