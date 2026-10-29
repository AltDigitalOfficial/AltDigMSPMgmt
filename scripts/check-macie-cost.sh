#!/usr/bin/env bash
#
# Is Macie automated discovery within its cost ceiling, per account? (ADR-017)
#
#   scripts/check-macie-cost.sh [--dry-run]
#
# Run from the management account. Prints WITHIN or EXCEEDS per account and
# never a figure: not the ceiling, not a cost, not a ratio (phase 15 plan F-7).
# For each account that exceeds, it raises a cost conversation:
# commercial_event cost-anomaly on a dossier, put on Audit's alert bus, where
# the commercial router sends it immediately and the router records it to the
# digest. --dry-run reports without raising.
#
# ---------------------------------------------------------------------------
# What the ceiling is, and what exceeding it does
# ---------------------------------------------------------------------------
# A ratio: Macie's automated-discovery cost over the account's whole AWS cost,
# both for the last 30 days. Discovery means object monitoring plus sampled
# inspection, the part ADR-017 governs. Bucket inventory evaluation, Macie's
# baseline charge, is not counted; it is the cost of having Macie at all.
#
# It lives in SSM at /platform/macie/discovery-cost-ceiling-ratio, set by the
# commercial owner. Absent means nobody has decided, and this refuses rather
# than assume a value.
#
# Exceeding it NEVER switches discovery off. That would remove the cardholder
# tripwire from the account, and automation does not remove controls (Jamie,
# 2026-10-28). It starts a conversation: narrow the scope (exclude buckets the
# client declares hold no customer data) or accept the cost.
#
# ---------------------------------------------------------------------------
# Why a script and not a schedule
# ---------------------------------------------------------------------------
# Cost Explorer can see every account only from the management account, and
# the management account holds nothing. 15.9's FinOps account is where this
# belongs. Until then it is run by hand, which loses nothing today: no tenant
# has data in S3, so discovery cost is effectively zero everywhere.
# Revival trigger for automating it: 15.9 landing, or the first tenant bucket
# holding objects, whichever comes first (ADR-017).
#
# No absolute floor. A dev account with a near-zero bill can exceed a ratio on
# a trivial amount; that alert is how the floor will be learned (ADR-017).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: check-macie-cost.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

CEILING_PARAM="/platform/macie/discovery-cost-ceiling-ratio"
CEILING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name "${CEILING_PARAM}" \
  --query Parameter.Value --output text 2>/dev/null | no_cr || true)"
[[ "${CEILING}" =~ ^0?\.[0-9]+$|^[0-9]+(\.[0-9]+)?$ ]] \
  || die "${CEILING_PARAM} is absent or not a number. Nobody has set the ceiling;
      refusing to guess one (phase 15 plan F-7)."

AUDIT="$(get_param /org/account/altdig-security-audit)"
IFS=',' read -ra REGIONS <<<"${PLATFORM_ALLOWED_REGIONS}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

END="$(date -u +%Y-%m-%d)"
START="$(date -u -d "${END} -30 days" +%Y-%m-%d)"

aws ce get-cost-and-usage --region us-east-1 \
  --time-period "Start=${START},End=${END}" --granularity MONTHLY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=LINKED_ACCOUNT \
  --output json > "${WORK}/ce.json" \
  || die "Cost Explorer read failed."

for r in "${REGIONS[@]}"; do
  in_account "${AUDIT}" aws macie2 get-usage-statistics --region "${r}" \
    --time-range PAST_30_DAYS --output json > "${WORK}/macie-${r}.json" \
    || die "Macie usage read failed in ${r}."
done

# Figures stay inside python. Out comes one line per account: id, verdict.
python - "$(win_path "${WORK}")" "${CEILING}" > "${WORK}/verdicts.txt" <<'PY'
import collections, glob, json, os, sys
work, ceiling = sys.argv[1], float(sys.argv[2])
DISCOVERY = {"AUTOMATED_OBJECT_MONITORING", "AUTOMATED_SENSITIVE_DATA_DISCOVERY"}
bill = collections.Counter()
for t in json.load(open(os.path.join(work, "ce.json")))["ResultsByTime"]:
    for g in t["Groups"]:
        bill[g["Keys"][0]] += float(g["Metrics"]["UnblendedCost"]["Amount"])
disc = collections.Counter()
for f in glob.glob(os.path.join(work, "macie-*.json")):
    for rec in json.load(open(f)).get("records") or []:
        for u in rec.get("usage") or []:
            if u.get("type") in DISCOVERY:
                disc[rec["accountId"]] += float(u.get("estimatedCost") or 0)
for acct in sorted(set(bill) | set(disc)):
    if disc[acct] == 0:
        verdict = "WITHIN"
    elif bill[acct] <= 0:
        verdict = "EXCEEDS"          # discovery cost with no bill to compare: say so
    else:
        verdict = "EXCEEDS" if disc[acct] / bill[acct] > ceiling else "WITHIN"
    print(acct, verdict)
PY

hr
log "Macie automated discovery against its cost ceiling (last 30 days)"
hr
EXCEEDED=0
while read -r acct verdict; do
  acct="$(printf '%s' "${acct}" | no_cr)"; verdict="$(printf '%s' "${verdict}" | no_cr)"
  if [[ "${verdict}" == "WITHIN" ]]; then
    ok "${acct} within"
    continue
  fi
  EXCEEDED=$((EXCEEDED+1))
  warn "${acct} EXCEEDS"
  detail="$(python - "${acct}" <<'PY'
import datetime, json, sys
print(json.dumps({
    "record_type": "macie-cost-watch",
    "commercial_event": "cost-anomaly",
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "routing": "digest",
    "alarm": {"name": "Macie automated discovery above its cost ceiling",
              "account_id": sys.argv[1], "resource_id": "macie automated discovery"},
    "assessment": {"probable_cause": (
        "Discovery cost over the last 30 days is above the ceiling ratio of this "
        "account's spend. Discovery stays on (ADR-017). Decide with the client: "
        "exclude buckets that hold no customer data, or accept the cost.")},
    "current_state": "discovery on, above ceiling",
}))
PY
)"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "      (dry run: no conversation raised)"
  else
    in_account "${AUDIT}" aws events put-events --region "${PLATFORM_HOME_REGION}" \
      --entries "$(python -c 'import json,sys; print(json.dumps([{"EventBusName":"platform-alerts","Source":"platform.cost","DetailType":"Platform Incident Dossier","Detail":sys.argv[1]}]))' "${detail}")" \
      >/dev/null || die "Could not raise the cost conversation for ${acct}."
    log "      cost conversation raised"
  fi
done < "${WORK}/verdicts.txt"
hr
[[ ${EXCEEDED} -eq 0 ]] && ok "Every account is within the ceiling." \
  || warn "${EXCEEDED} account(s) above the ceiling. Discovery stays on in all of them."
