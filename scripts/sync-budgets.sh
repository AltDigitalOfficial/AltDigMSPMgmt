#!/usr/bin/env bash
#
# Create and update one AWS Budget per tenant application and tier group, from
# the budgets the client gave in onboarding (questionnaire 13.2a/b, ADR-023).
#
#   scripts/sync-budgets.sh [--dry-run]
#
# Run in the management account, where budgets over linked accounts live. Run
# after vesting (vest-client.sh calls it) and after registry.sh set-budget.
#
# ---------------------------------------------------------------------------
# What a budget is here
# ---------------------------------------------------------------------------
# Two per application, because the question asks for two:
#
#   platform.<partner>.<client>.<app>.pre-production   dev + test + uat
#   platform.<partner>.<client>.<app>.production       prod
#
# Each covers that group's ACCOUNTS (the registry's account records), monthly,
# in US dollars, at the amount the client gave. Three alerts on each, all to
# the payer's cost topic (finops/20-cost-alerts.yaml), which the bridge in
# Audit turns into a budget-breach commercial event:
#
#   actual   >= 80%    early warning, still inside the month
#   actual   >= 100%   the budget is spent
#   forecast >= 100%   on course to exceed it
#
# The name format is the bridge's contract. Change one, change both.
#
# ---------------------------------------------------------------------------
# Every path ends with a person (the else rule)
# ---------------------------------------------------------------------------
#   group has accounts and an amount   budget created or updated
#   group has accounts, no answer      digest entry: "no budget for ...", each
#                                      run, until registry.sh set-budget
#   production answered 'deferred'     the same, once a production account
#   and a production account exists    exists: deferral ends at prod
#   group has no accounts              nothing; there is nothing to watch
#
# Additive only. A budget whose accounts have gone is left in place and
# listed, never deleted (CLAUDE.md: automation does not delete).
#
# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------
# Client commercial data. Read from the registry, passed to AWS through files
# in a private temporary directory, and never printed by this script.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '3,9p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
PY="$(command -v python || command -v python3)" || die "python not found."

TOOLING="$(get_param /org/account/altdig-infra-tooling)"
AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${TOOLING}" && "${TOOLING}" != "None" ]] || die "Platform Tooling is not registered."
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit is not registered."
TOPIC="arn:aws:sns:us-east-1:${PLATFORM_MGMT_ACCOUNT_ID}:platform-cost-alerts"

WORK="$(mktemp -d)"
chmod 700 "${WORK}"
# Windows Python reads Windows paths; Git Bash hands it /tmp paths.
WPATH="$(win_path "${WORK}")"
trap 'rm -rf "${WORK}"' EXIT

in_account "${TOOLING}" aws dynamodb scan --table-name platform-registry \
  --query 'Items' --output json > "${WORK}/registry.json" 2>/dev/null \
  || die "Could not read the platform registry."

# One line per (application, tier group) that has accounts:
#   name <TAB> group <TAB> accounts,comma,separated <TAB> state
# where state is "set", "missing" or "deferred". The amount, when set, goes
# to ${WORK}/<name>.amount and nowhere else.
"${PY}" - "${WPATH}" > "${WORK}/plan.tsv" <<'PY'
import json, os, sys

work = sys.argv[1]
items = json.load(open(os.path.join(work, "registry.json"), encoding="utf-8"))

def v(item, key):
    a = item.get(key) or {}
    return a.get("S") if "S" in a else a.get("N")

clients, groups = {}, {}
for it in items:
    if v(it, "record_type") == "CLIENT":
        clients[(v(it, "partner"), v(it, "slug"))] = it
for it in items:
    pk = v(it, "PK") or ""
    if not pk.startswith("ACCOUNT#"):
        continue
    partner, client, app, env = v(it, "partner"), v(it, "client"), v(it, "app"), v(it, "env")
    # The platform's own accounts are not a tenant's and have no budget answer.
    if partner == "altdigital" or not app or (v(it, "state") or "") == "devested":
        continue
    group = "production" if env == "prod" else "pre-production"
    groups.setdefault((partner, client, app, group), []).append(pk.split("#", 1)[1])

for (partner, client, app, group), accounts in sorted(groups.items()):
    name = f"platform.{partner}.{client}.{app}.{group}"
    rec = clients.get((partner, client)) or {}
    attr = f"budget#{app}#{group.replace('-', '_')}"
    amount = v(rec, attr)
    if amount == "deferred":
        state = "deferred"
    elif amount:
        state = "set"
        with open(os.path.join(work, name + ".amount"), "w") as f:
            f.write(amount)
    else:
        state = "missing"
    print(f"{name}\t{group}\t{','.join(sorted(accounts))}\t{state}")
PY
# CLAUDE.md trap 2: Windows Python ends lines with CRLF, and the state field
# is last on its line, so "missing\r" would match no case below.
sed -i 's/\r$//' "${WORK}/plan.tsv"

hr
log "Budgets from onboarding answers (13.2a/b)"
hr

raise_missing() {
  local name="$1" group="$2" why="$3" detail
  detail="$("${PY}" - "${name}" "${group}" "${why}" <<'PY'
import datetime, json, sys
name, group, why = sys.argv[1:4]
print(json.dumps({
    "record_type": "budget-missing",
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "routing": "digest",
    "alarm": {"name": f"No {group} budget: {name}", "resource_id": name},
    "assessment": {"probable_cause": (
        f"{why} No AWS Budget watches this spend. Ask the client for the "
        "13.2a/b answer and record it with scripts/registry.sh set-budget, "
        "then run scripts/sync-budgets.sh.")},
    "current_state": "no budget",
}))
PY
)"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  digest: no %s budget for %s\n' "${C_YELLOW}" "${C_RESET}" "${group}" "${name}"
    return 0
  fi
  in_account "${AUDIT}" aws events put-events --region "${PLATFORM_HOME_REGION}" \
    --entries "$("${PY}" -c 'import json,sys; print(json.dumps([{"EventBusName":"platform-alerts","Source":"platform.cost","DetailType":"Platform Incident Dossier","Detail":sys.argv[1]}]))' "${detail}")" \
    >/dev/null || die "Could not raise the missing-budget notice for ${name}."
}

# The three alerts every budget carries. Thresholds are percentages of the
# client's own amount, so no figure is set here.
"${PY}" - "${TOPIC}" > "${WORK}/notifications.json" <<'PY'
import json, sys
topic = sys.argv[1]
print(json.dumps([
    {"Notification": {"NotificationType": t, "ComparisonOperator": "GREATER_THAN",
                      "Threshold": p, "ThresholdType": "PERCENTAGE"},
     "Subscribers": [{"SubscriptionType": "SNS", "Address": topic}]}
    for t, p in (("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))]))
PY

SET=0; MISSING=0
while IFS=$'\t' read -r NAME GROUP ACCOUNTS STATE; do
  [[ -n "${NAME}" ]] || continue
  case "${STATE}" in
    missing)
      MISSING=$((MISSING + 1))
      warn "${NAME}: no answer; reported in the digest"
      raise_missing "${NAME}" "${GROUP}" "The client has not given a ${GROUP} budget."
      continue ;;
    deferred)
      MISSING=$((MISSING + 1))
      warn "${NAME}: deferred, but a production account exists; reported in the digest"
      raise_missing "${NAME}" "${GROUP}" "The production budget was deferred, and production is now vested, which ends the deferral."
      continue ;;
  esac

  "${PY}" - "${NAME}" "${ACCOUNTS}" "${WPATH}/${NAME}.amount" > "${WORK}/budget.json" <<'PY'
import json, sys
name, accounts, amount_file = sys.argv[1:4]
print(json.dumps({
    "BudgetName": name, "BudgetType": "COST", "TimeUnit": "MONTHLY",
    "BudgetLimit": {"Amount": open(amount_file).read().strip(), "Unit": "USD"},
    "CostFilters": {"LinkedAccount": accounts.split(",")},
}))
PY

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  budget %s over %s account(s), three alerts\n' \
      "${C_YELLOW}" "${C_RESET}" "${NAME}" "$(tr ',' '\n' <<<"${ACCOUNTS}" | wc -l | tr -d ' ')"
    SET=$((SET + 1))
    continue
  fi

  if aws budgets describe-budget --region us-east-1 --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" \
       --budget-name "${NAME}" --query 'Budget.BudgetName' --output text >/dev/null 2>&1; then
    aws budgets update-budget --region us-east-1 --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" \
      --new-budget "file://$(win_path "${WORK}/budget.json")" >/dev/null \
      || die "Could not update budget ${NAME}."
    # Alerts are added if absent, never removed or changed.
    HAVE="$(aws budgets describe-notifications-for-budget --region us-east-1 \
      --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" --budget-name "${NAME}" \
      --query 'Notifications[].[NotificationType,Threshold]' --output text | no_cr)"
    "${PY}" - "${WPATH}/notifications.json" "${HAVE}" > "${WORK}/missing-notifications.json" <<'PY'
import json, sys
want = json.load(open(sys.argv[1]))
have = {(l.split()[0], float(l.split()[1])) for l in sys.argv[2].splitlines() if l.strip()}
print(json.dumps([n for n in want if (n["Notification"]["NotificationType"],
                                      float(n["Notification"]["Threshold"])) not in have]))
PY
    while IFS= read -r N; do
      [[ -n "${N}" ]] || continue
      printf '%s' "${N}" > "${WORK}/one.json"
      "${PY}" -c 'import json,sys; n=json.load(open(sys.argv[1])); json.dump(n["Notification"], open(sys.argv[2],"w")); json.dump(n["Subscribers"], open(sys.argv[3],"w"))' \
        "${WPATH}/one.json" "${WPATH}/n.json" "${WPATH}/s.json"
      aws budgets create-notification --region us-east-1 --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" \
        --budget-name "${NAME}" --notification "file://$(win_path "${WORK}/n.json")" \
        --subscribers "file://$(win_path "${WORK}/s.json")" >/dev/null \
        || die "Could not add an alert to ${NAME}."
    done < <("${PY}" -c 'import json,sys; [print(json.dumps(n)) for n in json.load(open(sys.argv[1]))]' "${WPATH}/missing-notifications.json")
    ok "${NAME}: updated"
  else
    aws budgets create-budget --region us-east-1 --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" \
      --budget "file://$(win_path "${WORK}/budget.json")" \
      --notifications-with-subscribers "file://$(win_path "${WORK}/notifications.json")" \
      --resource-tags Key=platform-layer,Value=infrastructure Key=platform-managed,Value=true \
      >/dev/null || die "Could not create budget ${NAME}."
    ok "${NAME}: created"
  fi
  SET=$((SET + 1))
done < "${WORK}/plan.tsv"

hr
log "  budgets in place : ${SET}"
log "  without an answer: ${MISSING}$([[ ${MISSING} -gt 0 ]] && echo ' (each reported in the digest)')"
hr
