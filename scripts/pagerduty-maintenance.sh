#!/usr/bin/env bash
#
# Put a tenant's PagerDuty services into a turnup maintenance window, or end it.
#
#   scripts/pagerduty-maintenance.sh --tenant <client> [--hours 24] [--dry-run]
#   scripts/pagerduty-maintenance.sh --tenant <client> --close [--dry-run]
#
# ---------------------------------------------------------------------------
# Why (decided 2026-10-01)
# ---------------------------------------------------------------------------
# A brand-new account is evaluated by Security Hub before its baseline has
# finished arriving, and the findings it raises in the first hour are
# transient (CloudTrail.1, Config.1). For a production tier they route
# correctly -- the account IS production -- and they would page somebody for
# nothing. The first real vesting (Arc8) produced five such pages.
#
# A maintenance window keeps the incidents (they are created and recorded) and
# wakes nobody. A window rather than a disabled service, because a window has
# an end: a disabled service that nobody re-enables is a production tenant
# that silently never pages, while a window that nobody closes simply expires
# and paging resumes. 24 hours by default: turnup takes about an hour, and the
# margin covers a slow or failed one.
#
# The window is opened when the tenant's services are created and closed by
# the verification sweep or at go-live, whichever is first; if neither
# happens, it expires.
#
# --close ends the window by moving its end time to now, rather than deleting
# it, so the record of when the tenant's paging was suppressed survives.
#
# Idempotent: an open window for this tenant is found by its description tag
# and reused, so a re-run of onboarding does not stack windows.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TENANT=""; HOURS=24; CLOSE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)  TENANT="$2"; shift 2 ;;
    --hours)   HOURS="$2"; shift 2 ;;
    --close)   CLOSE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) printf 'Usage: %s --tenant <client> [--hours N | --close] [--dry-run]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "${TENANT}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "--tenant must be a client slug."
[[ "${HOURS}" =~ ^[0-9]+$ && "${HOURS}" -ge 1 && "${HOURS}" -le 72 ]] \
  || die "--hours must be 1-72. A longer suppression is not a turnup window."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TF="$(find_tool terraform)" || die "terraform not found. Run scripts/setup-tooling.sh."
TFDIR="${REPO_ROOT}/pagerduty"

# The tenant's service ids, from Terraform's own output. Only technical
# services take a maintenance window; business services carry no incidents.
TENANTS="$(cd "${TFDIR}" && "${TF}" output -json tenants 2>/dev/null)" \
  || die "Could not read the Terraform 'tenants' output. Has the tenant been applied?"

ACCT="$(get_param /org/account/altdig-security-audit)"
RAW="$(read_secret "${ACCT}" platform/pagerduty/api-token)"
[[ -n "${RAW}" && "${RAW}" != "None" ]] || die "No PagerDuty API token stored. Run scripts/set-pagerduty-token.sh."

# A PagerDuty user to attribute the change to. Some write endpoints require a
# From header naming a real user; the platform's responders are already
# configured in the gitignored terraform.tfvars, so the first one is used and
# never printed.
FROM="$("$(command -v python || command -v python3)" - "$(win_path "${TFDIR}/terraform.tfvars")" <<'PYEOF' 2>/dev/null || true
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'responder_emails\s*=\s*\[\s*"([^"]+)"', text)
print(m.group(1) if m else "")
PYEOF
)"

PD_SECRET="${RAW}" PD_FROM="${FROM}" TENANTS_JSON="${TENANTS}" TENANT="${TENANT}" \
HOURS="${HOURS}" CLOSE="${CLOSE}" DRY="${DRY_RUN:-0}" \
"$(command -v python || command -v python3)" - <<'PYEOF'
import datetime, json, os, sys, urllib.error, urllib.parse, urllib.request

token = json.loads(os.environ["PD_SECRET"])["api_token"]
tenant, hours = os.environ["TENANT"], int(os.environ["HOURS"])
close, dry = os.environ["CLOSE"] == "1", os.environ["DRY"] == "1"
tag = f"platform-turnup:{tenant}"

tenants = json.loads(os.environ["TENANTS_JSON"])
tenants = tenants.get("value", tenants)          # -json wraps sensitive outputs
entry = tenants.get(tenant)
if not entry:
    sys.exit(f"FAIL  {tenant} is not in the Terraform tenants output. Apply it first.")
services = sorted({o["service_id"] for o in (entry.get("objects") or {}).values() if o.get("service_id")})
if not services:
    sys.exit(f"FAIL  {tenant} has no technical services in the output.")

def call(method, path, body=None):
    headers = {"Authorization": f"Token token={token}",
               "Accept": "application/vnd.pagerduty+json;version=2",
               "Content-Type": "application/json"}
    if os.environ.get("PD_FROM"):
        headers["From"] = os.environ["PD_FROM"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.pagerduty.com" + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"FAIL  PagerDuty {method} {path.split('?')[0]}: HTTP {exc.code} "
                 f"{exc.read().decode()[:300]}")

now = datetime.datetime.now(datetime.timezone.utc)
query = urllib.parse.urlencode({"filter": "open", "limit": 100,
                                "service_ids[]": services}, doseq=True)
mine = [w for w in call("GET", "/maintenance_windows?" + query).get("maintenance_windows", [])
        if (w.get("description") or "").startswith(tag)]

if close:
    if not mine:
        print(f"  ok  no open turnup window for {tenant}; paging is already normal")
        sys.exit()
    for w in mine:
        if dry:
            print(f"  DRY  would end window {w['id']} now")
            continue
        end = (now + datetime.timedelta(seconds=60)).isoformat()
        call("PUT", f"/maintenance_windows/{w['id']}",
             {"maintenance_window": {"type": "maintenance_window", "end_time": end}})
        print(f"  ok  ended turnup window {w['id']} for {tenant}; paging resumes within a minute")
    sys.exit()

if mine:
    w = mine[0]
    print(f"  ok  turnup window {w['id']} already open for {tenant}, until {w['end_time']}")
    sys.exit()

end = now + datetime.timedelta(hours=hours)
body = {"maintenance_window": {
    "type": "maintenance_window",
    "start_time": now.isoformat(),
    "end_time": end.isoformat(),
    "description": (f"{tag} -- tenant turnup. Incidents are recorded, nobody is paged. "
                    "Closed by the verification sweep or go-live "
                    "(scripts/pagerduty-maintenance.sh --close); expires on its own otherwise."),
    "services": [{"id": s, "type": "service_reference"} for s in services],
}}
if dry:
    print(f"  DRY  would open a {hours}h turnup window over {len(services)} service(s) for {tenant}")
    sys.exit()
w = call("POST", "/maintenance_windows", body).get("maintenance_window", {})
print(f"  ok  turnup window {w.get('id')} open over {len(services)} service(s) for {tenant}, "
      f"until {w.get('end_time')}")
PYEOF
