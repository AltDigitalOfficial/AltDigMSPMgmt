#!/usr/bin/env bash
#
# Confirm the stored PagerDuty API key authenticates, and say plainly what
# that does and does not prove.
#
#   scripts/pagerduty-check.sh [--account <alias>]
#   scripts/pagerduty-check.sh --readonly   # the drift check's key instead
#
# --readonly checks platform/pagerduty/api-token-readonly, and requires the
# write probe (below) to be REFUSED. The full key requires it not to be.
#
# ---------------------------------------------------------------------------
# What this can prove, and what it cannot
# ---------------------------------------------------------------------------
# Authentication is provable: a GET against the API either works or does not.
#
# Write access cannot be fully proven without writing. PagerDuty has no dry-run
# and no "what could this key do" endpoint, and this refuses to create
# anything as a health check, because a check that leaves objects behind in a
# paging system is worse than the uncertainty it removes.
#
# The write probe is the narrow part that CAN be proven. It POSTs /services
# with an empty body. PagerDuty checks permission before it validates input,
# so:
#   403   the key may not create services: read-only
#   400   permission passed, and the empty body was refused ("Name cannot be
#         empty"): the key may create services
# Nothing is created either way; a service without a name is invalid.
# Observed 2026-11-01: 400 for the full key, 403 for the read-only key.
#
# That proves service creation only. Escalation policies and business
# services are not probed, so the full key's report still says the first
# 'pagerduty-apply.sh --apply' is the real proof.
#
# This replaced a key-prefix test. The script used to treat a key beginning
# R0 as read-only. PagerDuty does not mark keys that way: a key the console
# shows as Read-only was refused by that rule on 2026-11-01. A key's text
# says nothing about its access; only PagerDuty's answer does.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SECRET_NAME="platform/pagerduty/api-token"
ACCOUNT_KEY="altdig-security-audit"
READONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --readonly) READONLY=1; SECRET_NAME="platform/pagerduty/api-token-readonly"; shift ;;
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--readonly] [--secret <name>] [--account <alias>]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

ACCT="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${ACCT}" && "${ACCT}" != "None" ]] \
  || die "Account ${ACCOUNT_KEY} is not registered in SSM."

RAW="$(read_secret "${ACCT}" "${SECRET_NAME}")"
[[ -n "${RAW}" && "${RAW}" != "None" ]] \
  || die "${SECRET_NAME} is not present in ${ACCOUNT_KEY} (${ACCT}). Run scripts/set-pagerduty-token.sh$([[ ${READONLY} -eq 1 ]] && echo ' --readonly')."

ok "read ${SECRET_NAME} from ${ACCOUNT_KEY} ($(printf '%s' "${RAW}" | secret_fingerprint))"

# The token is handed to python through the environment, never on a command
# line and never into a message.
RESULT="$(PD_SECRET="${RAW}" python - <<'PYEOF'
import json, os, sys, urllib.error, urllib.request

try:
    token = json.loads(os.environ["PD_SECRET"])["api_token"]
except Exception:
    print(json.dumps({"ok": False,
                      "problem": "the stored secret is not the expected JSON "
                                 '{"api_token": ...}'}))
    sys.exit(0)

out = {}
HEADERS = {"Authorization": f"Token token={token}",
           "Accept": "application/vnd.pagerduty+json;version=2"}


def get(path):
    req = urllib.request.Request("https://api.pagerduty.com" + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def write_probe():
    """HTTP status of an empty create. See the header: 403 read-only, 400 may write."""
    req = urllib.request.Request(
        "https://api.pagerduty.com/services", data=b"{}", method="POST",
        headers={**HEADERS, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


try:
    abilities = get("/abilities").get("abilities") or []
    out["ok"] = True
    out["abilities"] = len(abilities)

    # Capability is tested by USING it, not by looking for a name in the
    # abilities list. B-114: the first version of this check looked for
    # "business_services" there, did not find it, and reported False -- while
    # the account had four business services and the module that needs them
    # would have worked. "business_services" is simply not an ability name,
    # so the lookup answered a question PagerDuty does not answer, and the
    # answer looked definitive.
    #
    # Listing is the weakest call that proves the endpoint is reachable and
    # the key is scoped to it, and it creates nothing.
    listed = get("/business_services")
    out["business_services_usable"] = "_err" not in listed
    if "_err" in listed:
        out["business_services_error"] = listed["_err"]
    else:
        out["business_services_existing"] = len(
            listed.get("business_services") or [])

    out["write_probe"] = write_probe()
except urllib.error.HTTPError as exc:
    out["ok"] = False
    out["status"] = exc.code
    out["problem"] = {
        401: "the key was rejected. It is wrong, revoked, or was copied with "
             "surrounding whitespace.",
        403: "the key authenticated and is not permitted to read abilities, "
             "which usually means a scoped key with too narrow a scope.",
    }.get(exc.code, exc.read().decode()[:200])
except Exception as exc:                                  # noqa: BLE001
    out["ok"] = False
    out["problem"] = f"{type(exc).__name__}: {str(exc)[:200]}"

print(json.dumps(out))
PYEOF
)"

OK="$(printf '%s' "${RESULT}" | python -c 'import json,sys; print(json.load(sys.stdin).get("ok"))')"
PROBLEM="$(printf '%s' "${RESULT}" | python -c 'import json,sys; print(json.load(sys.stdin).get("problem") or "")')"
PROBE="$(printf '%s' "${RESULT}" | python -c 'import json,sys; print(json.load(sys.stdin).get("write_probe"))')"

if [[ "${OK}" != "True" ]]; then
  die "PagerDuty rejected the key: ${PROBLEM}"
fi

ok "authenticated against the PagerDuty REST API"

if [[ "${READONLY}" == "1" ]]; then
  [[ "${PROBE}" == "403" ]] \
    || die "The write probe was not refused (HTTP ${PROBE}), so this key may be able to write. The drift check's key must be read-only."
  ok "read-only: PagerDuty refused the write probe (HTTP 403), as the drift check requires"
  # terraform plan reads every resource type the configuration holds, so the
  # first pagerduty-drift.sh run is the full proof that the key reads enough.
  exit 0
fi

case "${PROBE}" in
  400) ok "may create services: the write probe passed permission and failed validation (HTTP 400)" ;;
  403) die "PagerDuty refused the write probe (HTTP 403). This key is read-only; it cannot create services, escalation policies or business services." ;;
  *)   warn "The write probe returned HTTP ${PROBE}, which is neither expected answer. Write access is unknown." ;;
esac

# Reported because the tenant module depends on it. design 12's
# client-conversation event class needs a business service per client, and an
# account where that endpoint is unusable would fail at apply time -- part way
# through provisioning a tenant -- rather than here.
SUMMARY="$(printf '%s' "${RESULT}" | python -c '
import json, sys
d = json.load(sys.stdin)
if d.get("business_services_usable"):
    bs = "usable ({0} already exist)".format(d.get("business_services_existing", 0))
else:
    bs = "NOT usable (HTTP {0})".format(d.get("business_services_error"))
print("{0} abilities; business services: {1}".format(d.get("abilities", 0), bs))
')"
info "${SUMMARY}"

# The tenant module creates a business service per client. If that endpoint is
# not usable, an apply fails part way through provisioning rather than here.
USABLE="$(printf '%s' "${RESULT}" | python -c 'import json,sys; print(json.load(sys.stdin).get("business_services_usable"))')"
if [[ "${USABLE}" != "True" ]]; then
  die "Business services are not usable with this key. pagerduty/modules/tenant creates one per client (design 12's client-conversation class), so an apply would fail mid-tenant."
fi

printf '\n'
# Said explicitly rather than left implied. See the header.
warn "Only service creation is proven. Escalation policies and business services are not probed."
warn "The first 'pagerduty-apply.sh --apply' is what proves the rest."
