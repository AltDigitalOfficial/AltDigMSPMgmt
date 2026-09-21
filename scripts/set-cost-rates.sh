#!/usr/bin/env bash
#
# Publish the support pricing rates to SSM.
#
#   scripts/set-cost-rates.sh [--dry-run]
#
# Reads config/rates.env, which is gitignored. Rates are commercial terms: they
# do not belong in the repository, they are not printed by this script, and the
# monthly report reads them from Parameter Store rather than carrying them.
#
# ---------------------------------------------------------------------------
# Why this refuses to default to design 10's numbers
# ---------------------------------------------------------------------------
# design/10-commercial-model.md gives ~$1,000/account/month and 15-20%, and
# immediately says:
#
#     Model A is materially less work than Model B. The flat fee and percentage
#     should differ accordingly. Worth pricing before the first contract rather
#     than after.
#
# They are a starting point for a conversation, not a price list. A report that
# quietly used them would put a number in front of a customer that no human
# chose, and an indicative figure that reaches a customer becomes the price.
#
# So cost/src/report.py refuses to produce anything until this has run, and
# marks every output as an estimate until RATES_ARE_CONTRACTUAL is set.

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

RATES_FILE="${REPO_ROOT}/config/rates.env"
[[ -f "${RATES_FILE}" ]] || die "config/rates.env not found.
      Copy config/rates.env.example and fill in the agreed figures.
      That file is gitignored: pricing is a commercial term, not source."

# shellcheck disable=SC1090
source "${RATES_FILE}"

PY="$(command -v python || command -v python3)" || die "python not found."

PAYLOAD="$("${PY}" - "$@" <<'PYEOF'
import json
import os
import sys

models = {}
for key, value in os.environ.items():
    # RATE_<MODEL>_FLAT and RATE_<MODEL>_PERCENT
    if not key.startswith("RATE_"):
        continue
    parts = key.split("_")
    if len(parts) < 3:
        continue
    model = "_".join(parts[1:-1]).lower()
    field = parts[-1].lower()
    entry = models.setdefault(model, {})
    try:
        entry[field] = float(value)
    except ValueError:
        sys.exit(f"{key} is not a number.")

out = {}
for model, entry in models.items():
    if "flat" not in entry or "percent" not in entry:
        sys.exit(
            f"service model '{model}' needs both RATE_{model.upper()}_FLAT and "
            f"RATE_{model.upper()}_PERCENT. A model with only one of them "
            "would bill a flat fee with no uplift, or the reverse, and the "
            "report would look correct.")
    out[model] = {
        "flat_fee_per_account_month": entry["flat"],
        "uplift_percent": entry["percent"],
    }

if not out:
    sys.exit("config/rates.env defines no RATE_<MODEL>_FLAT / _PERCENT pairs.")

print(json.dumps(
    {"service_models": out,
     "rates_are_contractual":
         os.environ.get("RATES_ARE_CONTRACTUAL", "").lower() == "true"},
    separators=(",", ":")))
PYEOF
)" || die "Could not build the rates document."

MODELS="$("${PY}" -c "
import json,sys
print(' '.join(sorted(json.loads(sys.argv[1])['service_models'])))
" "${PAYLOAD}")"
CONTRACTUAL="$("${PY}" -c "
import json,sys
print(json.loads(sys.argv[1])['rates_are_contractual'])
" "${PAYLOAD}")"

hr
log "Support pricing rates"
log "  account      : management (${PLATFORM_MGMT_ACCOUNT_ID})"
log "  service models: ${MODELS}"
log "  contractual  : ${CONTRACTUAL}"
log "  parameter    : /platform/cost/rates"
hr
# The figures themselves are NOT printed. This output lands in terminals, CI
# logs and screenshares, and a rate is a commercial term.

if [[ "${CONTRACTUAL}" != "True" ]]; then
  warn "Rates are not marked contractual. Every report will carry a warning"
  warn "that it is an estimate and must not be sent to a customer. Set"
  warn "RATES_ARE_CONTRACTUAL=true once the figures are agreed."
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write /platform/cost/rates\n' "${C_YELLOW}" "${C_RESET}"
  exit 0
fi

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
aws ssm put-parameter \
  --name /platform/cost/rates \
  --value "${PAYLOAD}" \
  --type String \
  --description "Support pricing per service model. design 10. Set by scripts/set-cost-rates.sh from gitignored config/rates.env." \
  --overwrite --output text --query Version >/dev/null \
  || die "Could not write /platform/cost/rates."
ok "published /platform/cost/rates"

hr
info "The monthly report reads this. It refuses to run without it, rather"
log "  than falling back to design 10's indicative figures — those are a"
log "  starting point for pricing, not a price list."
hr
