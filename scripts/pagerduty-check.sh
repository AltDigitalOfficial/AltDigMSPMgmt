#!/usr/bin/env bash
#
# Confirm the stored PagerDuty API key authenticates, and say plainly what
# that does and does not prove.
#
#   scripts/pagerduty-check.sh [--account <alias>]
#
# ---------------------------------------------------------------------------
# What this can prove, and what it cannot
# ---------------------------------------------------------------------------
# Authentication is provable: a GET against the API either works or does not.
#
# WRITE access is not, without writing. PagerDuty has no dry-run and no
# "what could this key do" endpoint, so the only honest way to prove the key
# can create a service is to create one — which this refuses to do as a health
# check, because a check that leaves objects behind in a paging system is
# worse than the uncertainty it removes.
#
# So this reports authentication as verified and write access as UNPROVEN, and
# says which one it checked. The alternative — inferring write access from a
# successful GET — is the "checked and fine" versus "did not check" confusion
# that this codebase keeps rediscovering, and here it would surface as a
# Terraform apply failing half way through provisioning a tenant.
#
# The read-only prefix check is a cheap partial: PagerDuty prefixes read-only
# general access keys with `R0`, so a key that carries it is definitely wrong.
# A key that does not carry it is merely not-definitely-wrong.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SECRET_NAME="platform/pagerduty/api-token"
ACCOUNT_KEY="altdig-security-audit"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--secret <name>] [--account <alias>]\n' "$0"; exit 0 ;;
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
  || die "${SECRET_NAME} is not present in ${ACCOUNT_KEY} (${ACCT}). Run scripts/set-pagerduty-token.sh."

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

out = {"read_only_prefix": token.startswith("R0")}


def get(path):
    req = urllib.request.Request(
        "https://api.pagerduty.com" + path,
        headers={"Authorization": f"Token token={token}",
                 "Accept": "application/vnd.pagerduty+json;version=2"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


try:
    abilities = get("/abilities").get("abilities") or []
    out["ok"] = True
    out["abilities"] = len(abilities)
    # Useful signals for what the tenant module can rely on later.
    for want in ("teams", "service_dependencies", "business_services"):
        out[want] = want in abilities
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
READONLY="$(printf '%s' "${RESULT}" | python -c 'import json,sys; print(json.load(sys.stdin).get("read_only_prefix"))')"

if [[ "${READONLY}" == "True" ]]; then
  die "This key begins R0, which is PagerDuty's read-only prefix. It cannot create services, escalation policies or business services."
fi

if [[ "${OK}" != "True" ]]; then
  die "PagerDuty rejected the key: ${PROBLEM}"
fi

ok "authenticated against the PagerDuty REST API"

# Reported because the tenant module depends on them. `business_services` in
# particular: design 12's client-conversation event class needs a business
# service, and an account without that ability would fail at apply time rather
# than here.
SUMMARY="$(printf '%s' "${RESULT}" | python -c '
import json, sys
d = json.load(sys.stdin)
print("{0} abilities; business_services={1}, teams={2}".format(
    d.get("abilities", 0), d.get("business_services"), d.get("teams")))
')"
info "${SUMMARY}"

printf '\n'
# Said explicitly rather than left implied. See the header.
warn "WRITE access is NOT proven by this check, and cannot be without writing."
warn "The first 'pagerduty-apply.sh --apply' is what proves it."
