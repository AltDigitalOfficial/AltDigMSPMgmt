#!/usr/bin/env bash
#
# After a PagerDuty apply: routing keys into Secrets Manager, service ids back
# into the registry.
#
#   scripts/sync-pagerduty-tenants.sh [--dry-run]
#
# ---------------------------------------------------------------------------
# Why this is a separate step and not part of the apply
# ---------------------------------------------------------------------------
# A routing key does not exist until the integration exists. It cannot be known
# in advance, cannot be written by hand, and cannot be resolved by
# CloudFormation at deploy time. So the order is fixed:
#
#   render-pagerduty-tenants.sh   registry  -> pagerduty/tenants.json
#   pagerduty-apply.sh --apply    tenants.json -> PagerDuty
#   sync-pagerduty-tenants.sh     PagerDuty -> Secrets Manager + registry
#
# The last step is what makes the saga resumable. Provisioning step 3 does not
# call PagerDuty and does not parse Terraform output; it asks the registry
# whether this tenant has a PagerDuty footprint, and the answer got there from
# here. A saga resumed next week on a different machine reaches the same
# conclusion, because the state was never in anybody's shell.
#
# ---------------------------------------------------------------------------
# Where the keys go, and where they eventually need to go
# ---------------------------------------------------------------------------
# Written into AUDIT today, alongside the platform's own routing keys.
#
# That is NOT where they finally belong. A tenant's alarms fire in the tenant's
# own account, and the SNS subscription that carries them to PagerDuty is
# created by the baseline in that account -- so the key has to be readable
# there. Distributing it is part of provisioning step 6 (instrumentation),
# which is not built.
#
# Audit is the right interim home rather than a wrong one: it is where the
# alerting layer lives, the keys are inert until something subscribes using
# them, and leaving them unwritten would mean the apply's only durable output
# existed solely in local Terraform state. B-119 records the distribution.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACCOUNT_KEY="altdig-security-audit"
PREFIX="platform/pagerduty/tenant"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --account) ACCOUNT_KEY="$2"; shift 2 ;;
    --prefix)  PREFIX="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--dry-run] [--account <alias>] [--prefix <path>]\n' "$0"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TF="$(find_tool terraform)" || die "terraform not found. Run scripts/setup-tooling.sh."
TFDIR="${REPO_ROOT}/pagerduty"

TARGET="$(get_param "/org/account/${ACCOUNT_KEY}")"
[[ -n "${TARGET}" && "${TARGET}" != "None" ]] || die "Unknown account: ${ACCOUNT_KEY}"

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] \
  || die "Platform Tooling is not registered in SSM; the registry cannot be written."

# `terraform output -json`, not `-raw`. The tenants output is a nested map and
# is marked sensitive, so -raw cannot render it and the redaction that applies
# to console output does not apply to -json.
TENANTS="$(cd "${TFDIR}" && "${TF}" output -json tenants 2>/dev/null)" || true
[[ -n "${TENANTS}" && "${TENANTS}" != "null" ]] \
  || die "No 'tenants' output from Terraform. Run scripts/pagerduty-apply.sh --apply first."

COUNT="$(cd "${TFDIR}" && "${TF}" output -raw tenant_count 2>/dev/null | no_cr)" || COUNT=0

hr
log "PagerDuty tenant sync"
log "  clients in terraform state : ${COUNT:-0}"
log "  secrets into              : ${ACCOUNT_KEY} (${TARGET})"
log "  registry in               : altdig-infra-tooling (${TOOLING})"
hr

if [[ "${COUNT:-0}" == "0" ]]; then
  # Zero clients is a legitimate state and NOT a failure. Saying so explicitly,
  # because "wrote 0 secrets" and "could not read the output" look identical in
  # a log and mean opposite things.
  ok "no tenants under management; nothing to sync."
  exit 0
fi

# The plan is computed in python and executed in bash, so that what gets
# written is inspectable before anything is written. Routing keys are never
# printed -- only their lengths and the secret names.
PLAN="$(TENANTS="${TENANTS}" PREFIX="${PREFIX}" python - <<'PYEOF'
import json, os

tenants = json.loads(os.environ["TENANTS"])
prefix = os.environ["PREFIX"]

secrets, registry, problems = [], [], []

for client, data in sorted(tenants.items()):
    objects = data.get("objects") or {}
    counts = data.get("counts") or {}

    # Declared-versus-found, per client. counts comes from Terraform and says
    # how many services the module BUILT; objects is what came back with a
    # routing key. A mismatch means an apply half-succeeded, and reporting the
    # keys we happen to have would hide it.
    if len(objects) != counts.get("objects", len(objects)):
        problems.append({
            "client": client,
            "problem": "the module reports {0} object services and {1} came "
                       "back with routing keys".format(
                           counts.get("objects"), len(objects))})

    for key, svc in sorted(objects.items()):
        tier, obj = key.split("/", 1)
        name = "{0}/{1}/{2}/{3}".format(prefix, client, tier, obj or "app")
        routing = svc.get("routing_key")
        if not routing:
            problems.append({
                "client": client, "object": key,
                "problem": "service exists with no routing key; the "
                           "integration was not created"})
            continue
        secrets.append({"name": name, "key": routing,
                        "client": client, "tier": tier,
                        "object": obj or "app",
                        "service_id": svc.get("service_id")})

    registry.append({
        "client": client,
        "business_service_id": data.get("conversations_business_service_id"),
        "tier_services": json.dumps(data.get("tiers") or {}, sort_keys=True),
        "object_services": json.dumps(
            {k: v.get("service_id") for k, v in sorted(objects.items())},
            sort_keys=True),
    })

print(json.dumps({"secrets": secrets, "registry": registry,
                  "problems": problems}))
PYEOF
)" || die "Could not build the sync plan."

PROBLEMS="$(printf '%s' "${PLAN}" | python -c 'import json,sys; print(len(json.load(sys.stdin)["problems"]))')"
if [[ "${PROBLEMS}" != "0" ]]; then
  printf '%s' "${PLAN}" | python -c '
import json, sys
for p in json.load(sys.stdin)["problems"]:
    print("  " + json.dumps(p))
'
  die "${PROBLEMS} inconsistency(ies) between what the module built and what came back. Re-run the apply; do not sync a partial result."
fi

NSECRETS="$(printf '%s' "${PLAN}" | python -c 'import json,sys; print(len(json.load(sys.stdin)["secrets"]))')"
info "${NSECRETS} routing key(s) to write"

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s' "${PLAN}" | python -c '
import json, sys
d = json.load(sys.stdin)
for s in d["secrets"]:
    print("  DRY  would write {0} ({1} characters, not printed)".format(
        s["name"], len(s["key"])))
for r in d["registry"]:
    print("  DRY  would stamp TENANT#{0} with business_service_id={1}".format(
        r["client"], r["business_service_id"]))
'
  exit 0
fi

# --- routing keys ----------------------------------------------------------

while IFS=$'\t' read -r NAME KEYVAL; do
  [[ -n "${NAME}" ]] || continue
  PAYLOAD="$(printf '{"events_v2_routing_key":"%s"}' "${KEYVAL}")"
  if in_account "${TARGET}" aws secretsmanager describe-secret \
       --secret-id "${NAME}" >/dev/null 2>&1; then
    in_account "${TARGET}" aws secretsmanager put-secret-value \
      --secret-id "${NAME}" --secret-string "${PAYLOAD}" \
      --query VersionId --output text >/dev/null \
      && ok "rotated ${NAME} (${#KEYVAL} characters, not printed)"
  else
    in_account "${TARGET}" aws secretsmanager create-secret \
      --name "${NAME}" --secret-string "${PAYLOAD}" \
      --description "PagerDuty routing key for a tenant service. Written by scripts/sync-pagerduty-tenants.sh." \
      --tags Key=platform-managed,Value=true \
      --query ARN --output text >/dev/null \
      && ok "created ${NAME} (${#KEYVAL} characters, not printed)"
  fi
done < <(printf '%s' "${PLAN}" | python -c '
import json, sys
for s in json.load(sys.stdin)["secrets"]:
    print(s["name"] + "\t" + s["key"])
')
unset PAYLOAD

# --- registry write-back ---------------------------------------------------
#
# The saga reads THIS, not Terraform state. Stamped last, so a failure above
# leaves the registry saying the tenant has no PagerDuty footprint -- which is
# recoverable by re-running -- rather than saying it has one that is not fully
# wired.

# Which partner each client sits under, from the rendered tenant map. Needed
# because onboarding's client record is keyed PARTNER#<p> / CLIENT#<slug>, not
# TENANT#<slug> -- see the CLIENT branch in render-pagerduty-tenants.sh.
PARTNER_OF="$("$(command -v python || command -v python3)" -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for c, t in (d.get("tenants") or {}).items():
    print(c + "\t" + (t.get("partner") or ""))
' "$(win_path "${REPO_ROOT}/pagerduty/tenants.json")" 2>/dev/null || true)"

while IFS=$'\t' read -r CLIENT BSID TIERS OBJECTS; do
  [[ -n "${CLIENT}" ]] || continue
  SET_EXPR="SET pagerduty_business_service_id = :b, pagerduty_tier_services = :t, pagerduty_object_services = :o, updated_at = :u"
  VALUES="{\":b\":{\"S\":\"${BSID}\"},\":t\":{\"S\":${TIERS}},\":o\":{\"S\":${OBJECTS}},\":u\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
  PARTNER="$(printf '%s\n' "${PARTNER_OF}" | awk -F'\t' -v c="${CLIENT}" '$1==c {print $2}')"
  # The saga's record first, then onboarding's. Each update is conditional on
  # the record existing, so neither can create a stray record.
  if in_account "${TOOLING}" aws dynamodb update-item \
       --table-name platform-registry \
       --key "{\"PK\":{\"S\":\"TENANT#${CLIENT}\"},\"SK\":{\"S\":\"META\"}}" \
       --update-expression "${SET_EXPR}" --expression-attribute-values "${VALUES}" \
       --condition-expression "attribute_exists(PK)" >/dev/null 2>&1; then
    ok "stamped TENANT#${CLIENT}"
  elif [[ -n "${PARTNER}" ]] && in_account "${TOOLING}" aws dynamodb update-item \
       --table-name platform-registry \
       --key "{\"PK\":{\"S\":\"PARTNER#${PARTNER}\"},\"SK\":{\"S\":\"CLIENT#${CLIENT}\"}}" \
       --update-expression "${SET_EXPR}" --expression-attribute-values "${VALUES}" \
       --condition-expression "attribute_exists(PK)" >/dev/null 2>&1; then
    ok "stamped PARTNER#${PARTNER} / CLIENT#${CLIENT}"
  else
    warn "could not stamp ${CLIENT}: no TENANT#${CLIENT} record, and no client record under partner '${PARTNER:-unknown}'."
  fi
done < <(printf '%s' "${PLAN}" | python -c '
import json, sys
for r in json.load(sys.stdin)["registry"]:
    print("\t".join([r["client"], r["business_service_id"] or "",
                     json.dumps(r["tier_services"]),
                     json.dumps(r["object_services"])]))
')

hr
info "Next: re-run the provisioning saga. Step 3 reads the registry and will"
log "  now find pagerduty_business_service_id, so it advances rather than"
log "  blocking."
hr
