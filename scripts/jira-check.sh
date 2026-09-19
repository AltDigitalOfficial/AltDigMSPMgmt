#!/usr/bin/env bash
#
# Verify the platform can actually read Jira.
#
#   scripts/jira-check.sh
#
# Runs the check INSIDE the reconciliation Lambda rather than from this
# workstation, for two reasons. The credential never leaves AWS -- fetching it
# here to test it would put it in a laptop's process environment, which is the
# thing Secrets Manager exists to avoid. And the path being tested is the real
# one: execution role -> Secrets Manager -> Jira, rather than a similar path
# from somewhere else with different credentials.
#
# Checks three things, because a working connection is not the same as a
# working configuration:
#
#   1. can the platform authenticate, and as whom
#   2. does the configured change project exist and can it be read
#   3. does it have the issue type reconciliation searches for
#
# Point 3 is the one that catches a silent failure. A valid token pointed at a
# project with no matching issue type finds zero change records, and an engine
# that trusted that would report every deployment as unapproved. The engine
# refuses to draw that conclusion (it reports the change source as
# unavailable), but this is where the misconfiguration is supposed to surface.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name jira-check \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into the Audit account (${AUDIT})."
read -r AK SK ST <<<"${CREDS}"
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" AWS_SESSION_TOKEN="${ST}"

OUT="$(mktemp)"; PAYLOAD="$(mktemp)"
trap 'rm -f "${OUT}" "${PAYLOAD}"' EXIT
printf '{"action":"health"}' > "${PAYLOAD}"

info "Asking the reconciliation function to talk to Jira"
MSYS_NO_PATHCONV=1 aws lambda invoke \
  --function-name platform-change-reconciliation \
  --payload "fileb://$(win_path "${PAYLOAD}")" \
  "$(win_path "${OUT}")" --query '[StatusCode,FunctionError]' --output text \
  | no_cr

# The check reports what the DEPLOYED FUNCTION is configured with, which is the
# right thing to test and a confusing thing to read. Editing config/platform.env
# does not reach a running Lambda -- the project key is a stack parameter baked
# into its environment -- so the two drift the moment somebody changes the file
# and has not redeployed yet. That happened within an hour of this script
# existing. The comparison below is what stops it happening twice.
PY="$(command -v python || command -v python3)"
LOCAL_SITE="${PLATFORM_JIRA_SITE:-}" LOCAL_PROJECT="${PLATFORM_JIRA_DEFAULT_CHANGE_PROJECT:-}" LOCAL_ISSUETYPE="${PLATFORM_JIRA_CHANGE_ISSUETYPE:-}" "${PY}" - "$(win_path "${OUT}")" <<'REPORTEOF'
import json, os, sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("errorMessage"):
    print("  FAIL  the function itself errored:", d["errorMessage"])
    sys.exit(1)

ok = d.get("connected")
detail = d.get("detail") or {}
print("")
print(f"  site            : {detail.get('site')}")
if ok:
    print(f"  authenticated as: {detail.get('display_name')} "
          f"({detail.get('account_id')})")
else:
    print(f"  FAILED          : {detail.get('error')}")

days = d.get("token_days_remaining")
print(f"  token expires   : {d.get('token_expires')}"
      + (f"  ({days:.0f} days)" if isinstance(days, (int, float)) else ""))

project = d.get("default_change_project")
print(f"  change project  : {project or '<unset>'}")
if "project_issue_types" in d:
    print(f"  issue types     : {', '.join(d['project_issue_types'])}")
    mark = "ok" if d.get("issue_type_present") else "MISSING"
    print(f"  '{d.get('expected_issue_type')}' issue type : {mark}")
if d.get("request_types"):
    print("")
    print("  JSM request types in this project:")
    for rt in d["request_types"]:
        print(f"    {rt['request_type']:<38} -> issue type: {rt['backed_by_issue_type']}")
if d.get("suggested_issue_type"):
    print("")
    print("  LOOKS LIKE YOUR CHANGE TYPE:")
    for c in d.get("change_candidates", []):
        print(f"    request type '{c['request_type']}'"
              f" is backed by issue type '{c['backed_by_issue_type']}'")
    print("")
    print("  Set in config/platform.env:")
    # QUOTED. JSM issue type names contain spaces and brackets, and
    # config/platform.env is sourced by every script in scripts/ -- an
    # unquoted value would set the variable to "[System]" and then try to
    # execute "Change".
    print(f'    PLATFORM_JIRA_CHANGE_ISSUETYPE="{d["suggested_issue_type"]}"')
if d.get("request_type_error"):
    print("")
    print("  REQUEST TYPE LOOKUP FAILED: " + d["request_type_error"][:300])
if d.get("warning"):
    print("")
    print("  WARNING: " + d["warning"])
if d.get("project_error"):
    print("")
    print("  PROJECT ERROR: " + d["project_error"])
# Deployed configuration vs config/platform.env.
drift = []
for label, local, deployed in (
        ("site", (os.environ.get("LOCAL_SITE") or "").rstrip("/"),
         (detail.get("site") or "")),
        ("change project", os.environ.get("LOCAL_PROJECT") or "",
         d.get("default_change_project") or ""),
        ("issue type", os.environ.get("LOCAL_ISSUETYPE") or "",
         d.get("expected_issue_type") or "")):
    if local and local != deployed:
        drift.append((label, local, deployed))

if drift:
    print("")
    print("  CONFIG DRIFT -- the deployed function does not match "
          "config/platform.env:")
    for label, local, deployed in drift:
        print(f"    {label:<15} file: {local!r}   deployed: {deployed!r}")
    print("")
    print("  Everything above describes the DEPLOYED value. Editing")
    print("  config/platform.env does not reach a running Lambda; the values")
    print("  are stack parameters. Redeploy to apply them:")
    print("")
    print("    source config/platform.env && scripts/deploy-to-account.sh \\")
    print("      --account altdig-security-audit \\")
    print("      --template change/10-reconciliation.yaml --stack platform-change \\")
    print("      ArtifactBucket=<bucket> ArtifactKey=<key> \\")
    print("      \"JiraSite=${PLATFORM_JIRA_SITE}\" \\")
    print("      \"JiraDefaultChangeProject=${PLATFORM_JIRA_DEFAULT_CHANGE_PROJECT}\" \\")
    print("      \"JiraChangeIssueType=${PLATFORM_JIRA_CHANGE_ISSUETYPE}\"")

print("")
sys.exit(0 if ok and not drift else 1)
REPORTEOF
