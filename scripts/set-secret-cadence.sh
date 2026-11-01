#!/usr/bin/env bash
#
# Record a secret's re-issue cadence for the reminder (security/90-secret-age.
# yaml, ADR-020). Tags only; never reads or writes the value.
#
#   scripts/set-secret-cadence.sh <secret-name> <days|none> \
#     [--issued YYYY-MM-DD] [--owner <role>] [--dry-run]
#
#   <days>     re-issue every N days, counted from platform-issued
#   none       deliberately no cadence; the reminder records it and stays quiet
#   --issued   the day the credential was MINTED, not the day it was stored.
#              store_secret_everywhere stamps this itself on every new value,
#              so this flag is for secrets written some other way, and for
#              correcting a date.
#   --owner    the role that re-issues it, carried into the reminder. A role,
#              not a person's name: names do not belong in the repository or
#              in tags that land in it.
#
# Secrets live in Audit (ADR-019), so this works there only.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

NAME=""; CADENCE=""; ISSUED=""; OWNER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --issued)  ISSUED="$2"; shift 2 ;;
    --owner)   OWNER="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '3,19p' "$0"; exit 0 ;;
    -*)        die "Unknown argument: $1" ;;
    *)         if [[ -z "${NAME}" ]]; then NAME="$1"; else CADENCE="$1"; fi; shift ;;
  esac
done

[[ -n "${NAME}" && -n "${CADENCE}" ]] \
  || die "Usage: set-secret-cadence.sh <secret-name> <days|none> [--issued YYYY-MM-DD] [--owner <role>]"
[[ "${CADENCE}" == none || "${CADENCE}" =~ ^[1-9][0-9]*$ ]] \
  || die "Cadence must be a positive number of days, or none."
[[ -z "${ISSUED}" || "${ISSUED}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || die "--issued must be YYYY-MM-DD."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
AUDIT="$(get_param /org/account/altdig-security-audit)"

in_account "${AUDIT}" aws secretsmanager describe-secret --secret-id "${NAME}" \
  --query Name --output text >/dev/null 2>&1 \
  || die "${NAME} does not exist in Audit."

TAGS=("Key=platform-max-age-days,Value=${CADENCE}")
[[ -n "${ISSUED}" ]] && TAGS+=("Key=platform-issued,Value=${ISSUED}")
[[ -n "${OWNER}" ]]  && TAGS+=("Key=platform-owner,Value=${OWNER}")

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  would tag %s: %s\n' "${C_YELLOW}" "${C_RESET}" "${NAME}" "${TAGS[*]}"
  exit 0
fi

in_account "${AUDIT}" aws secretsmanager tag-resource --secret-id "${NAME}" --tags "${TAGS[@]}" \
  || die "Tagging ${NAME} failed."
ok "${NAME}: $(in_account "${AUDIT}" aws secretsmanager describe-secret --secret-id "${NAME}" \
  --query "Tags[?starts_with(Key,'platform-')].[Key,Value]" --output text | no_cr | tr '\t\n' '= ')"
