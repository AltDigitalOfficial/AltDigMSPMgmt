#!/usr/bin/env bash
#
# Publish the evidence tenant map into the Audit account.
#
#   scripts/sync-evidence-tenants.sh [--dry-run]
#
# The evidence pipeline files every record against a tenant and a partner, and
# both travel ON the record rather than being derived at query time -- an
# account can be re-parented, and evidence about March must not change because
# an OU moved in September. D-016.
#
# Derived from the account registry, never hand-written here. B-064: a
# hand-maintained account list was missing Forensics, an entire account went
# unchecked, and nothing said so.

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

PY="$(command -v python || command -v python3)" || die "python not found."

AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] \
  || die "Audit account not registered in SSM."

# Every registered account, from the registry in the management account.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
ACCOUNTS="$(aws ssm get-parameters-by-path --path /platform/org/account/ \
  --query 'Parameters[].[Name,Value]' --output text 2>/dev/null | no_cr)"
[[ -n "${ACCOUNTS}" ]] || die "No accounts found under /platform/org/account/."

# The account list goes in a FILE, not on stdin.
#
# `python -` reads its program from stdin, so a here-string carrying the data
# has nowhere to go -- the script ran with empty input and produced a valid,
# empty tenant map. Every evidence record would then have filed as
# unattributed, which the pipeline reports but which is not what anyone
# intended. Silent, syntactically fine, and wrong.
ACCOUNTS_FILE="$(mktemp)"
trap 'rm -f "${ACCOUNTS_FILE}"' EXIT
printf "%s\n" "${ACCOUNTS}" > "${ACCOUNTS_FILE}"

# win_path on both. MSYS_NO_PATHCONV is exported above, so REPO_ROOT stays a
# POSIX path that python.exe cannot open -- it reads /c/AltDigital/... as a
# relative path and looks for C:\c\AltDigital.
PAYLOAD="$("${PY}" \
  "$(win_path "${REPO_ROOT}/scripts/lib/evidence_tenants.py")" \
  "$(win_path "${ACCOUNTS_FILE}")")"

COUNT="$("${PY}" -c "
import json,sys
d=json.loads(sys.argv[1])
print(sum(len(v['accounts']) for v in d['tenants'].values()))
" "${PAYLOAD}")"

hr
log "Evidence tenant map"
log "  account  : altdig-security-audit (${AUDIT})"
log "  tenants  : 1 (altdigital-platform — no client tenants vested yet)"
log "  accounts : ${COUNT}"
log "  parameter: /platform/evidence/tenants"
hr

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would write /platform/evidence/tenants into %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${AUDIT}"
  exit 0
fi

CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${AUDIT}:role/OrganizationAccountAccessRole" \
  --role-session-name sync-evidence-tenants \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true
[[ -n "${CREDS}" ]] || die "Could not assume into ${AUDIT}."
read -r AK SK ST <<<"${CREDS}"
(
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${AK}" AWS_SECRET_ACCESS_KEY="${SK}" \
         AWS_SESSION_TOKEN="${ST}"
  MSYS_NO_PATHCONV=1 aws ssm put-parameter \
    --name /platform/evidence/tenants \
    --value "${PAYLOAD}" \
    --type String \
    --description "Tenants, partners and their accounts for the evidence pipeline. Derived from the account registry; see scripts/sync-evidence-tenants.sh." \
    --overwrite --output text --query Version >/dev/null || exit 1
)
[[ $? -eq 0 ]] || die "Could not write the parameter into ${AUDIT}."
ok "published /platform/evidence/tenants"

hr
info "Re-run this after vesting an account, or evidence from it files under"
log "  the unattributed tenant and raises a finding until it is mapped."
hr
