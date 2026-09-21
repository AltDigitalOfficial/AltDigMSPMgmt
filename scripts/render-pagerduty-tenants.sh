#!/usr/bin/env bash
#
# Render pagerduty/tenants.json from the registry.
#
#   scripts/render-pagerduty-tenants.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# Why this renders DATA and not Terraform
# ---------------------------------------------------------------------------
# Generated .tf is reviewable only by someone who reads Terraform, diffs
# noisily whenever the generator's formatting changes, and lets a generator bug
# create a resource nobody intended. Generated data can only populate shapes
# `pagerduty/tenants.tf` already declares, so a generator bug produces a wrong
# VALUE rather than a wrong RESOURCE.
#
# The practical consequence is that adding a client is a one-line diff in a
# JSON map, which Art or Wayne can review in a pull request without knowing
# Terraform.
#
# ---------------------------------------------------------------------------
# Why it is a script and not part of the saga Lambda
# ---------------------------------------------------------------------------
# design 13 says to treat PagerDuty "exactly like the AWS baseline: versioned,
# staged, drift-detected". The AWS baseline is applied by scripts a person
# runs, and so is this. A Lambda cannot write a file into the repository, and
# a Lambda calling the PagerDuty API directly would make every tenant service
# drift relative to Terraform state -- which would make the drift detector
# design 13 asks for fire on the platform's own work.
#
# So provisioning step 3 BLOCKS and tells the operator to run this, apply, and
# sync. That is the same shape as step 9's go-live gate: a human step inside
# the workflow rather than beside it.
#
# ---------------------------------------------------------------------------
# What is included, and what absence means
# ---------------------------------------------------------------------------
#   clients    every TENANT record not in a terminal or dormant state. Dormant
#              tenants have had their paging removed on purpose (design 14 and
#              the deprovisioning UNDO table), so re-rendering them would
#              recreate what a teardown deliberately took away.
#   apps       ACCOUNT records with environment=prod and kind=tenant, grouped
#              by client. design 13: a paging service exists "per application
#              (production only)".
#
# A client with no production account renders an EMPTY production_apps list.
# That is correct -- it gets a business service and no paging service -- and it
# is not the same as the client being absent. The summary prints both counts so
# the difference is visible rather than inferred.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

OUT="${REPO_ROOT}/pagerduty/tenants.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --out)     OUT="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--dry-run] [--out <path>]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling is not registered in SSM; the registry cannot be read."

info "reading platform-registry from tooling (${TOOLING})"

ITEMS="$(in_account "${TOOLING}" aws dynamodb scan \
  --table-name platform-registry --output json 2>/dev/null)" \
  || die "Could not scan platform-registry in ${TOOLING}."

[[ -n "${ITEMS}" ]] || die "The registry scan returned nothing at all, which is not the same as an empty registry. Refusing to render."

RENDERED="$(ITEMS="${ITEMS}" python - <<'PYEOF'
import json, os, sys

items = json.loads(os.environ["ITEMS"]).get("Items") or []


def s(item, key):
    return (item.get(key) or {}).get("S") or ""


# Dormant is excluded deliberately: a dormant tenant's paging was removed by a
# teardown, and re-rendering it here would quietly put back what that teardown
# took away. Exited tenants are excluded for the same reason, more finally.
LIVE_STATES = {"provisioning", "provisioned", "verifying", "live"}

clients, apps, skipped = {}, {}, []

for item in items:
    kind = s(item, "record_type")

    if kind == "TENANT" or (not kind and s(item, "tenant_id")):
        tid = s(item, "tenant_id")
        state = s(item, "state")
        if not tid:
            continue
        if state not in LIVE_STATES:
            skipped.append({"client": tid, "state": state})
            continue
        clients[tid] = s(item, "partner")

    elif kind == "ACCOUNT":
        # Only production, and only tenant accounts. A platform account in
        # production is AltDigital's own and is covered by the platform
        # service, not by a tenant one.
        if s(item, "environment") != "prod":
            continue
        if (s(item, "kind") or "tenant") != "tenant":
            continue
        apps.setdefault(s(item, "client"), set()).add(s(item, "app"))

tenants = {}
for tid, partner in sorted(clients.items()):
    tenants[tid] = {
        "partner": partner,
        "production_apps": sorted(apps.get(tid, set())),
    }

# Accounts whose client has no TENANT record. Reported rather than dropped:
# a production account with no registry tenant is a real inconsistency, and
# silently omitting it would mean an account whose alarms page nowhere.
orphans = sorted(set(apps) - set(clients))

print(json.dumps({
    "tenants": tenants,
    "_summary": {
        "records_scanned": len(items),
        "clients_included": len(tenants),
        "clients_skipped": skipped,
        "clients_with_production": sum(
            1 for t in tenants.values() if t["production_apps"]),
        "orphan_production_accounts": orphans,
    },
}, indent=2, sort_keys=True))
PYEOF
)" || die "Rendering failed."

SUMMARY="$(printf '%s' "${RENDERED}" | python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["_summary"]))')"
PAYLOAD="$(printf '%s' "${RENDERED}" | python -c '
import json, sys
d = json.load(sys.stdin)
d.pop("_summary", None)
print(json.dumps(d, indent=2, sort_keys=True))
')"

SCANNED="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["records_scanned"])')"
INCLUDED="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["clients_included"])')"
WITHPROD="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["clients_with_production"])')"
ORPHANS="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(" ".join(json.load(sys.stdin)["orphan_production_accounts"]) or "none")')"
SKIPPED="$(printf '%s' "${SUMMARY}" | python -c '
import json, sys
rows = json.load(sys.stdin)["clients_skipped"]
print(", ".join(r["client"] + "(" + r["state"] + ")" for r in rows) or "none")')"

hr
log "PagerDuty tenant map"
# What was EXAMINED, not only what was found. A render that emitted an empty
# map because the scan returned nothing would otherwise look identical to a
# render of an estate with no tenants in it.
log "  registry records scanned : ${SCANNED}"
log "  clients included         : ${INCLUDED}"
log "  of which with production : ${WITHPROD}"
log "  clients skipped          : ${SKIPPED}"
log "  orphan prod accounts     : ${ORPHANS}"
hr

if [[ "${ORPHANS}" != "none" ]]; then
  # Not fatal, and not silent. A production account whose client has no tenant
  # record gets no paging service, which means its alarms page nobody.
  warn "Production account(s) for client(s) with no TENANT record: ${ORPHANS}"
  warn "These get no paging service, so their alarms would page nobody."
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write %s\n' "${C_YELLOW}" "${C_RESET}" "${OUT}"
  printf '%s\n' "${PAYLOAD}"
  exit 0
fi

printf '%s\n' "${PAYLOAD}" > "${OUT}"
ok "wrote ${OUT}"

hr
info "Next:"
log "  scripts/pagerduty-apply.sh            # plan, and read it"
log "  scripts/pagerduty-apply.sh --apply"
log "  scripts/sync-pagerduty-tenants.sh     # routing keys -> Secrets Manager,"
log "                                        # service ids -> registry"
hr
