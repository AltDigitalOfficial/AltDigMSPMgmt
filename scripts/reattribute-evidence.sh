#!/usr/bin/env bash
#
# Move evidence filed under the unattributed tenant to the tenant its account
# now maps to. B-156 item 8; the reasoning is in evidence/src/reattribute.py.
#
#   scripts/reattribute-evidence.sh --dry-run   # what would move; writes nothing
#   scripts/reattribute-evidence.sh             # move it
#
# Run after onboarding publishes a client's accounts to the evidence tenant map
# (sync-evidence-tenants.sh). Until B-157 makes it automatic, every newly vested
# account leaves its first evidence stranded and this is how it is recovered.
#
# Runs the committed module, in the Audit account, with the collector's own
# configuration: the same ledger, sequence table, queue and tenant map the
# pipeline uses, read from the deployed function rather than restated here.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '3,15p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
# It writes the evidence ledger. What writes it must be a commit someone can name.
require_committed evidence/src

PY="$(command -v python || command -v python3)" || die "python not found."
AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered in SSM."

run_in_audit() {
  local env_json
  env_json="$(aws lambda get-function-configuration \
                --function-name platform-evidence-collect \
                --query 'Environment.Variables' --output json)" \
    || { printf 'could not read platform-evidence-collect configuration\n' >&2; return 4; }
  # The collector's variables, exported for the module to read at import.
  eval "$(printf '%s' "${env_json}" | "${PY}" -c "
import json, shlex, sys
for k, v in json.load(sys.stdin).items():
    print(f'export {k}={shlex.quote(v)}')
")"
  PYTHONPATH="$(win_path "${REPO_ROOT}/evidence/src")" "${PY}" -c "
import json, sys
import reattribute
reattribute.reattribute({'dry_run': sys.argv[1] == '1'})
" "${DRY_RUN}"
}

hr
if [[ "${DRY_RUN}" == "1" ]]; then
  info "Dry run: reporting what would move. Nothing is written."
else
  info "Moving stranded evidence in ${AUDIT}"
fi
OUT="$(in_account "${AUDIT}" run_in_audit)"
rc=$?
[[ ${rc} -eq 3 ]] && die "Could not assume into the Audit account."
[[ ${rc} -eq 0 ]] || die "Re-attribution failed (exit ${rc}). Re-run with --dry-run to see the state."

printf '%s\n' "${OUT}" | "${PY}" -c "
import json, sys
s = json.loads(sys.stdin.read().strip().splitlines()[-1])
key = 'would_move' if s['dry_run'] else 'moved'
print(f\"  {key:<16}: {s[key]}\")
for stream, n in (s.get('moved_to') or {}).items():
    print(f'    -> {stream}: {n}')
for acct, n in (s.get('unmapped_by_account') or {}).items():
    print(f'  still unmapped  : {acct} ({n} record(s)) -- publish it to the tenant map')
if s.get('stuck'):
    print(f\"  STUCK MID-MOVE  : {len(s['stuck'])} -- see reattribute.py, 'What a move is'\")
if s.get('lost_claim'):
    print(f\"  claimed elsewhere: {s['lost_claim']}\")
"
hr
