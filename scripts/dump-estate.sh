#!/usr/bin/env bash
#
# Dump the whole platform -- live AWS estate plus the code that built it --
# to a single JSON file.
#
#   scripts/dump-estate.sh [--out <path>]
#
# Written 2026-09-22 for the Arc8 questionnaire dry run. One file that answers
# "what have we actually built" without opening eight consoles.
#
# ---------------------------------------------------------------------------
# What is deliberately NOT in the output
# ---------------------------------------------------------------------------
# Secrets Manager values, SSM SecureString values, AWS alternate contacts, root
# account emails, Identity Center membership, SNS email and SMS endpoints, and
# DynamoDB table contents. See the module docstring in lib/estate_dump.py for
# the reasoning on each.
#
# The redaction is not trusted, it is CHECKED. Before writing, the generator
# scans the finished document for every value in config/contacts.env and
# config/identity.env -- the real personal data this platform holds, gitignored
# for exactly that reason -- and refuses to write if any of it appears. If
# either file is missing the check reports that it could not run rather than
# passing, because "found nothing" and "looked at nothing" are the same output
# otherwise.
#
# ---------------------------------------------------------------------------
# It sweeps every account, so it is slow and it will outlive an SSO session
# ---------------------------------------------------------------------------
# Eight accounts across three regions. A failure in any one collector is
# recorded in `errors` and the sweep continues -- aborting would throw away
# everything already gathered for one expired token. Read `errors` before
# treating an empty section as an empty estate.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

OUT="${REPO_ROOT}/estate/estate.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) printf 'Usage: %s [--out <path>]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

PY="$(command -v python || command -v python3)" || die "python not found."
"${PY}" -c 'import boto3' 2>/dev/null \
  || die "boto3 not installed. pip install boto3"

hr
log "Estate dump"
log "  from     : management (${PLATFORM_MGMT_ACCOUNT_ID})"
log "  output   : ${OUT}"
log "  redaction: verified against config/contacts.env and config/identity.env"
hr
info "Sweeping every active account in three regions. This takes a few minutes."

# Redirected to a file rather than piped, because the exit code matters and a
# pipeline reports the LAST command's status -- CLAUDE.md, Windows trap 3.
SUMMARY_FILE="$(mktemp)"
trap 'rm -f "${SUMMARY_FILE}"' EXIT

set +e
"${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/estate_dump.py")" \
  "$(win_path "${REPO_ROOT}")" "$(win_path "${OUT}")" > "${SUMMARY_FILE}"
RC=$?
set -e

if [[ ${RC} -eq 2 ]]; then
  die "Personal data reached the dump and it was NOT written. See above."
fi
[[ ${RC} -eq 0 ]] || die "Estate dump failed (exit ${RC})."

hr
"${PY}" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print('  accounts         : %d' % d['accounts'])
print('  stacks           : %d' % d['stacks'])
print('  lambda functions : %d' % d['lambda_functions'])
print('  alarms           : %d' % d['alarms'])
print('  repository files : %d' % d['repo_files'])
print('  collector errors : %d' % d['errors'])
print('  size             : %.1f MB' % (d['bytes']/1048576.0))
" "$(win_path "${SUMMARY_FILE}")"
hr
ok "Written to ${OUT}"
info "Read the 'errors' array before treating an empty section as an empty"
log "  estate. A collector that could not run leaves the same shape as one"
log "  that found nothing."
hr
