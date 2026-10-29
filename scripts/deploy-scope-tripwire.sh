#!/usr/bin/env bash
#
# Deploy the cardholder scope tripwire to Audit (ADR-017).
#
#   scripts/deploy-scope-tripwire.sh [--dry-run]
#
# Exists so that ScopeChangeTiers is never typed. It is computed from
# config/tiers.yaml (cardholder_data_is_scope_change) through scripts/lib/tiers.py,
# the same resolver everything else uses, so the template cannot quietly
# disagree with the tier model. A tier added to tiers.yaml is picked up on the
# next deploy; a tier set to false stops opening conversations, and still
# records the finding.
#
# The log group key is Audit's logs key, read from the platform-evidence stack
# in the same account rather than repeated here.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) log "Usage: deploy-scope-tripwire.sh [--dry-run]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TIERS_FILE="$(win_path "${REPO_ROOT}/config/tiers.yaml")"
TIERS_LIB="$(win_path "${REPO_ROOT}/scripts/lib/tiers.py")"

python "${TIERS_LIB}" "${TIERS_FILE}" validate >/dev/null \
  || die "config/tiers.yaml does not validate. Fix it before deploying anything that reads it."

SCOPE_TIERS="$(python - "${TIERS_LIB}" "${TIERS_FILE}" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("tiers", sys.argv[1])
tiers = importlib.util.module_from_spec(spec); spec.loader.exec_module(tiers)
model = tiers.parse(sys.argv[2])
on = [t for t in sorted(model.get("tiers") or {})
      if tiers.resolve(model, t)["settings"].get("cardholder_data_is_scope_change") is True]
print(",".join(on))
PY
)"
SCOPE_TIERS="$(printf '%s' "${SCOPE_TIERS}" | no_cr)"
[[ -n "${SCOPE_TIERS}" ]] || warn "No tier treats cardholder data as a scope change. The tripwire will record findings and open no conversations."

AUDIT="$(get_param /org/account/altdig-security-audit)"
LOGS_KEY="$(in_account "${AUDIT}" aws cloudformation describe-stacks --stack-name platform-evidence \
  --query "Stacks[0].Parameters[?ParameterKey=='LogsKeyArn'].ParameterValue" --output text 2>/dev/null | no_cr)"
[[ "${LOGS_KEY}" == arn:aws:kms:* ]] || die "Could not read Audit's logs key from the platform-evidence stack."

hr
log "Cardholder scope tripwire"
log "  account          : Audit"
log "  scope-change in  : ${SCOPE_TIERS:-(none)}   (from config/tiers.yaml)"
hr

run "deploy platform-scope-tripwire" \
  bash "${REPO_ROOT}/scripts/deploy-to-account.sh" --account altdig-security-audit \
    --template security/70-scope-tripwire.yaml --stack platform-scope-tripwire \
    ScopeChangeTiers="${SCOPE_TIERS}" LogsKeyArn="${LOGS_KEY}"
