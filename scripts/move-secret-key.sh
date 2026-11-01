#!/usr/bin/env bash
#
# Move one Audit secret onto the platform secrets key, or back off it.
#
#   scripts/move-secret-key.sh <secret-name>          # onto alias/platform-secrets
#   scripts/move-secret-key.sh <secret-name> --back   # onto aws/secretsmanager
#   [--dry-run]
#
# ADR-019. Changing a secret's KmsKeyId does NOT re-encrypt the versions it
# already has: measured 2026-10-31 on a probe, both versions stayed on the old
# key after update-secret. Only versions written afterwards use the new key.
# So the move is: change the key, then write the SAME value twice, which puts
# both AWSCURRENT and AWSPREVIOUS on it. The superseded versions lose their
# labels and keep the old key until Secrets Manager prunes them.
#
# The value never reaches the terminal. It passes from get-secret-value to
# put-secret-value inside the assumed-role subshell, and the run compares
# exact fingerprints before and after to prove the value did not change.
#
# --back is the rollback, and it is the same procedure. Use it if a reader
# fails to decrypt after a move.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

NAME=""; BACK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --back)    BACK=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '3,8p' "$0"; exit 0 ;;
    -*)        die "Unknown argument: $1" ;;
    *)         NAME="$1"; shift ;;
  esac
done
[[ -n "${NAME}" ]] || die "Usage: move-secret-key.sh <secret-name> [--back] [--dry-run]"

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
AUDIT="$(get_param /org/account/altdig-security-audit)"

if [[ ${BACK} -eq 1 ]]; then
  TARGET_KEY="alias/aws/secretsmanager"
else
  TARGET_KEY="$(in_account "${AUDIT}" aws kms describe-key --key-id alias/platform-secrets \
    --query KeyMetadata.Arn --output text 2>/dev/null | no_cr)"
  [[ "${TARGET_KEY}" == arn:* ]] \
    || die "alias/platform-secrets not found in Audit. Deploy security/80-secrets-key.yaml first."
fi

versions() {
  in_account "${AUDIT}" aws secretsmanager list-secret-version-ids --secret-id "${NAME}" \
    --query 'Versions[?VersionStages].[join(`,`,VersionStages),KmsKeyIds[0]]' --output text \
    | no_cr | sed 's/^/    /'
}

hr
log "secret : ${NAME} (Audit ${AUDIT})"
log "to key : ${TARGET_KEY}"
log "before :"; versions
hr

# Fingerprints here are EXACT: sha256 of the SecretString bytes as JSON
# carries them. lib/common.sh's secret_fingerprint strips whitespace, which is
# right for comparing accounts, and wrong here, where a trailing CR or LF
# picked up in transit would be a changed secret that compares equal.
PY="$(command -v python || command -v python3)"
exact_fp() {
  in_account "${AUDIT}" aws secretsmanager get-secret-value --secret-id "${NAME}" \
    --output json 2>/dev/null | "${PY}" -c '
import hashlib, json, sys
try: s = json.load(sys.stdin)["SecretString"]
except Exception: print("absent"); sys.exit()
print(hashlib.sha256(s.encode()).hexdigest()[:12])'
}

# The value travels as a shell variable: decoded from JSON by Python, captured
# by $(...). That capture drops trailing newlines, so prove the round trip is
# exact BEFORE changing anything, and refuse a value it would alter.
ROUNDTRIP='
  v="$(aws secretsmanager get-secret-value --secret-id "$1" --output json \
       | "$2" -c "import json,sys; sys.stdout.buffer.write(json.load(sys.stdin)[\"SecretString\"].encode())")" || exit 4
'

BEFORE="$(exact_fp)"
[[ "${BEFORE}" != absent ]] || die "${NAME} has no readable value in Audit. Nothing changed."
CARRIED="$(in_account "${AUDIT}" bash -c "${ROUNDTRIP}"'
  printf %s "$v" | "$2" -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])"' \
  _ "${NAME}" "${PY}" | no_cr)"
[[ "${CARRIED}" == "${BEFORE}" ]] \
  || die "The value would not survive the rewrite exactly (${BEFORE} vs ${CARRIED}). Nothing changed."

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would re-key %s and rewrite it twice (fingerprint %s, round trip exact)\n' \
    "${C_YELLOW}" "${C_RESET}" "${NAME}" "${BEFORE}"
  exit 0
fi

in_account "${AUDIT}" aws secretsmanager update-secret --secret-id "${NAME}" \
  --kms-key-id "${TARGET_KEY}" --query Name --output text >/dev/null \
  || die "update-secret failed. Nothing changed."

# Both writes in one subshell: the value lives in a variable that dies with it.
in_account "${AUDIT}" bash -c "${ROUNDTRIP}"'
  for i in 1 2; do
    aws secretsmanager put-secret-value --secret-id "$1" --secret-string "$v" \
      --query VersionId --output text >/dev/null || exit 5
  done' _ "${NAME}" "${PY}" \
  || die "Rewrite failed after the key changed. Re-run, or run with --back."

AFTER="$(exact_fp)"
log "after  :"; versions
[[ "${AFTER}" == "${BEFORE}" ]] \
  || die "Fingerprint changed (${BEFORE} -> ${AFTER}). The value is NOT what it was. Investigate before anything reads it."
ok "${NAME}: value unchanged (${AFTER}), labelled versions on the new key"
