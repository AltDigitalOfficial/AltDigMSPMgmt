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
#   tiers      ACCOUNT records with kind=tenant, grouped by client and then by
#              environment. EVERY tier, not production only -- see D-018. A dev
#              environment is the developer's own production and needs somewhere
#              for its alerts to go; the separation from production is urgency
#              and escalation policy, not absence.
#   objects    the `app` field on each account, which is the finest granularity
#              the registry currently carries. The empty string is a real value
#              meaning the account's single unnamed application.
#
# A client with no accounts renders an EMPTY tiers map. That is correct -- it
# gets the client-conversation business service and nothing else -- and it is
# not the same as the client being absent. The summary prints both counts so
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

clients, apps, skipped, unplaced = {}, {}, [], []

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

    elif kind == "CLIENT":
        # The record onboarding actually writes (scripts/registry.sh
        # put-client): PARTNER#<p> / CLIENT#<slug>. The TENANT shape above is
        # the provisioning saga's, which is written and not deployed. Until
        # 2026-10-01 only that shape was read, so a tenant onboarded through
        # onboard-client.sh was never rendered: the first real one (OEight /
        # Arc8) came out as "clients included: 0" with its accounts listed as
        # orphans, and would have had no PagerDuty services at all.
        #
        # Keyed by slug, because that is what account records carry in
        # `client` and so what the tier/object grouping below matches on.
        # Two partners with the same client slug would collide here -- noted
        # in docs/scale-watch.md territory; today there is one tenant.
        tid = s(item, "slug")
        state = s(item, "state")
        if not tid:
            continue
        if state not in LIVE_STATES:
            skipped.append({"client": tid, "state": state})
            continue
        if tid in clients and clients[tid] != s(item, "partner"):
            raise SystemExit(f"client slug '{tid}' exists under two partners "
                             f"({clients[tid]}, {s(item, 'partner')}); the tenant map "
                             "cannot tell them apart. Resolve before rendering.")
        clients[tid] = s(item, "partner")

    elif kind == "ACCOUNT":
        # Tenant accounts in every tier. A PLATFORM account is AltDigital's own
        # and is covered by the platform service, not by a tenant one -- that
        # is the only exclusion here.
        if (s(item, "kind") or "tenant") != "tenant":
            continue
        env = s(item, "environment")
        if not env:
            # An account with no environment cannot be placed in a tier, and
            # guessing one would put a production workload behind dev-tier
            # urgency. Reported rather than assigned.
            unplaced.append({"account_id": s(item, "account_id"),
                             "client": s(item, "client")})
            continue
        apps.setdefault(s(item, "client"), {}).setdefault(env, set()).add(
            s(item, "app"))

tenants = {}
for tid, partner in sorted(clients.items()):
    tiers = {}
    for env, objects in sorted((apps.get(tid) or {}).items()):
        tiers[env] = {"objects": sorted(objects)}
    tenants[tid] = {"partner": partner, "tiers": tiers}

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
        "clients_with_accounts": sum(
            1 for t in tenants.values() if t["tiers"]),
        "tiers_total": sum(len(t["tiers"]) for t in tenants.values()),
        "objects_total": sum(
            len(c["objects"]) for t in tenants.values()
            for c in t["tiers"].values()),
        "orphan_accounts": orphans,
        "unplaced_accounts": unplaced,
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
WITHPROD="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["clients_with_accounts"])')"
TIERS="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["tiers_total"])')"
OBJECTS="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(json.load(sys.stdin)["objects_total"])')"
UNPLACED="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(len(json.load(sys.stdin)["unplaced_accounts"]))')"
ORPHANS="$(printf '%s' "${SUMMARY}" | python -c 'import json,sys; print(" ".join(json.load(sys.stdin)["orphan_accounts"]) or "none")')"
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
log "  of which with accounts   : ${WITHPROD}"
log "  tiers                    : ${TIERS}"
log "  objects                  : ${OBJECTS}"
log "  accounts with no tier    : ${UNPLACED}"
log "  clients skipped          : ${SKIPPED}"
log "  orphan accounts          : ${ORPHANS}"
hr

if [[ "${ORPHANS}" != "none" ]]; then
  # Not fatal, and not silent. A production account whose client has no tenant
  # record gets no paging service, which means its alarms page nobody.
  warn "Account(s) for client(s) with no TENANT record: ${ORPHANS}"
  warn "These get no service, so their alarms would reach nobody."
fi

if [[ "${UNPLACED}" != "0" ]]; then
  # An account with no environment cannot be placed in a tier. Guessing would
  # put a production workload behind dev-tier urgency, which is the wrong
  # direction to be wrong in.
  warn "${UNPLACED} account(s) carry no environment and were not placed in any tier."
  warn "Their alarms have no service to reach. Fix the registry record."
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
