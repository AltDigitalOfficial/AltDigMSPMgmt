#!/usr/bin/env bash
#
# Package the instrumentation Lambda and publish it to the artifact bucket.
#
#   scripts/publish-instrumentation.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# What this does that a plain zip does not
# ---------------------------------------------------------------------------
# 1. Converts alarm-sets.yaml to alarm-sets.json. The Lambda runtime has boto3
#    but not PyYAML, and vendoring PyYAML to call safe_load once would add a
#    third-party dependency to audit and patch inside a function that creates
#    alarms in every member account. Authoring stays YAML so the reasoning in
#    the comments survives; only the data ships.
#
# 2. Publishes under a CONTENT-ADDRESSED key. The object key contains a hash of
#    the package, so a code change produces a new key rather than overwriting
#    one. That matters because CloudFormation decides whether to update a
#    Lambda by comparing S3Key and S3ObjectVersion — overwrite the same key and
#    the stack sees no change, so member accounts keep running the old code
#    while the console shows the stack as up to date.
#
# 3. Prints the key to pass to the baseline StackSet, because that is the step
#    that actually makes the new code live.

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
  || die "Platform Tooling account not registered. Run:
      scripts/create-platform-account.sh --ou infra --role tooling"

BUCKET="altdig-platform-artifacts-${TOOLING}"
SRC="${REPO_ROOT}/instrumentation/src"
RULES="${REPO_ROOT}/instrumentation/alarm-sets.yaml"

BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT

cp "${SRC}/handler.py" "${BUILD}/handler.py"

# YAML -> JSON. Fails loudly on a malformed rules file rather than shipping a
# package whose config cannot be parsed at cold start — a failure that would
# otherwise appear as every instrumentation invocation erroring, in every
# member account, with a stack trace instead of a reason.
"${PY}" - "$(win_path "${RULES}")" "$(win_path "${BUILD}/alarm-sets.json")" <<'PYEOF'
import json, sys, yaml
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)

for key in ("resource_types", "ignored_resource_types", "agent_required_metrics"):
    if key not in cfg:
        sys.exit(f"alarm-sets.yaml is missing required key: {key}")

required = {"name_suffix", "metric", "statistic", "period", "evaluation_periods",
            "threshold", "comparison", "treat_missing_data", "tiers"}
valid_tmd = {"missing", "ignore", "breaching", "notBreaching"}
problems = []
for rtype, rcfg in (cfg["resource_types"] or {}).items():
    for field in ("namespace", "dimension", "alarms"):
        if field not in rcfg:
            problems.append(f"{rtype}: missing '{field}'")
    for spec in rcfg.get("alarms", []):
        missing = required - set(spec)
        if missing:
            problems.append(f"{rtype}/{spec.get('name_suffix','?')}: missing {sorted(missing)}")
        tmd = spec.get("treat_missing_data")
        if tmd and tmd not in valid_tmd:
            problems.append(f"{rtype}/{spec.get('name_suffix')}: treat_missing_data '{tmd}' invalid")
        if not spec.get("tiers"):
            problems.append(f"{rtype}/{spec.get('name_suffix')}: no tiers, so it applies nowhere")
if problems:
    sys.exit("alarm-sets.yaml is invalid:\n  " + "\n  ".join(problems))

with open(dst, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
print(f"  {len(cfg['resource_types'])} resource type(s), "
      f"{sum(len(v['alarms']) for v in cfg['resource_types'].values())} alarm spec(s)")
PYEOF

# Zipped with python, not the zip(1) utility.
#
# Two reasons, and the second is the important one:
#
#   Git for Windows does not ship zip. Requiring it would make publishing
#   depend on a tool this workstation does not have.
#
#   The archive must be DETERMINISTIC. This script names the object by a hash
#   of the package so that identical code produces an identical key — that is
#   what stops CloudFormation from missing an update and what stops a
#   no-op publish creating a new object. A zip stores each entry's mtime, so
#   the same source zipped twice hashes differently and the content-addressing
#   silently degrades into "new key every time".
#
#   Fixing date_time and sorting the entries removes both sources of variance.
info "Packaging"
"${PY}" - "$(win_path "${BUILD}")" <<'PYEOF'
import sys, zipfile, pathlib
build = pathlib.Path(sys.argv[1])
names = sorted(["handler.py", "alarm-sets.json"])
with zipfile.ZipFile(build / "package.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name in names:
        data = (build / name).read_bytes()
        # Fixed timestamp. The value is arbitrary; that it never varies is the
        # whole point. 1980-01-01 is the earliest a zip can represent.
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, data)
print(f"  {len(names)} file(s)")
PYEOF

HASH="$("${PY}" -c "
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:16])
" "$(win_path "${BUILD}/package.zip")")"
KEY="instrumentation/handler-${HASH}.zip"
SIZE="$(wc -c < "${BUILD}/package.zip" | tr -d ' ')"

hr
log "Instrumentation package"
log "  account : altdig-infra-tooling (${TOOLING})"
log "  bucket  : ${BUCKET}"
log "  key     : ${KEY}"
log "  size    : ${SIZE} bytes"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would upload s3://%s/%s\n' "${C_YELLOW}" "${C_RESET}" "${BUCKET}" "${KEY}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole" \
  --role-session-name publish-instrumentation \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${TOOLING}."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

if MSYS_NO_PATHCONV=1 aws s3api head-object --bucket "${BUCKET}" --key "${KEY}" \
     >/dev/null 2>&1; then
  skip "identical package already published (content-addressed key)"
else
  MSYS_NO_PATHCONV=1 aws s3api put-object --bucket "${BUCKET}" --key "${KEY}" \
    --body "$(win_path "${BUILD}/package.zip")" >/dev/null
  ok "uploaded s3://${BUCKET}/${KEY}"
fi

hr
info "Deploy it with:"
log "  scripts/deploy-stackset.sh --name platform-instrumentation \\"
log "    --template baseline/50-instrumentation.yaml --ou sandbox \\"
log "    ArtifactBucket=${BUCKET} ArtifactKey=${KEY} \\"
log "    MonitoringTier=reduced AlertTopicArn=<audit topic arn>"
log ""
log "The key changes whenever the code or the rules change. A stack set still"
log "running an old key is running old code, however current it looks."
hr
