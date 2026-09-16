#!/usr/bin/env bash
#
# Create one platform (non-member) AWS account and place it in its OU.
#
#   scripts/create-platform-account.sh --ou sandbox --role canary [--dry-run]
#
# Produces:
#   name    AltDigital Sandbox Canary
#   alias   ad-sandbox-canary
#   email   mspr+sandbox-canary@altdigital.ai
#   OU      Sandbox
#
# ---------------------------------------------------------------------------
# Account creation is effectively one-way
# ---------------------------------------------------------------------------
# An AWS account cannot be deleted, only closed, and closure leaves it
# suspended for 90 days. The root email address is consumed permanently — AWS
# will not allow it to be reused for another account, ever. So this script is
# deliberately loud, idempotent on email, and supports --dry-run.
#
# This is NOT the member account vesting pipeline. Member accounts carry a
# questionnaire, a registry record, a Truveon tenant, PagerDuty and Jira, and
# come through the provisioning saga (design doc 13). Platform accounts have
# none of that; they are infrastructure.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

OU=""; ROLE=""; DISPLAY_NAME=""

usage() {
  cat <<'USAGE'
Usage: create-platform-account.sh --ou <ou> --role <role> [--name "Display Name"] [--dry-run]

  --ou       Owning OU scope: security | infra | sandbox
  --role     Account role: canary, logarchive, audit, forensics, tooling, shared, backup
  --name     Human-readable Organizations account name. Defaulted if omitted.
  --dry-run  Print what would happen; create nothing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ou)      OU="$2"; shift 2 ;;
    --role)    ROLE="$2"; shift 2 ;;
    --name)    DISPLAY_NAME="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${OU}" ]]   || { usage; die "--ou is required."; }
[[ -n "${ROLE}" ]] || { usage; die "--role is required."; }

case " ${PLATFORM_OUS} " in
  *" ${OU} "*) ;;
  *) die "Unknown OU scope '${OU}'. Expected one of: ${PLATFORM_OUS}" ;;
esac

ALIAS="$(platform_account_alias "${OU}" "${ROLE}")"
EMAIL="$(platform_account_email "${OU}" "${ROLE}")"
[[ -n "${DISPLAY_NAME}" ]] || DISPLAY_NAME="AltDigital ${OU^} ${ROLE^}"

# Map the scope segment to the actual OU name in the tree.
case "${OU}" in
  security) OU_PARAM=/org/ou/security ;;
  infra)    OU_PARAM=/org/ou/infrastructure ;;
  sandbox)  OU_PARAM=/org/ou/sandbox ;;
esac

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TARGET_OU="$(get_param "${OU_PARAM}")"
[[ -n "${TARGET_OU}" && "${TARGET_OU}" != "None" ]] \
  || die "OU id not found at ${PLATFORM_SSM_PREFIX}${OU_PARAM}. Run deploy-org-structure.sh first."

hr
log "Platform account"
log "  alias   : ${ALIAS}"
log "  name    : ${DISPLAY_NAME}"
log "  email   : ${EMAIL}"
log "  OU      : ${OU} (${TARGET_OU})"
hr

# --- idempotency on email --------------------------------------------------
# The email is the natural key: AWS enforces global uniqueness on it, so an
# account already holding this address is the account we would have created.
EXISTING="$(aws organizations list-accounts \
  --query "Accounts[?Email=='${EMAIL}'].Id | [0]" --output text 2>/dev/null | no_cr | sed 's/^None$//')"

if [[ -n "${EXISTING}" ]]; then
  skip "Account ${EXISTING} already holds ${EMAIL} — not creating."
  ACCOUNT_ID="${EXISTING}"
else
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create-account %s <%s>\n' "${C_YELLOW}" "${C_RESET}" "${DISPLAY_NAME}" "${EMAIL}"
    printf '%s DRY%s  move to OU %s\n' "${C_YELLOW}" "${C_RESET}" "${TARGET_OU}"
    printf '%s DRY%s  set alias %s\n' "${C_YELLOW}" "${C_RESET}" "${ALIAS}"
    printf '%s DRY%s  SSM %s/org/account/%s\n' "${C_YELLOW}" "${C_RESET}" "${PLATFORM_SSM_PREFIX}" "${ALIAS}"
    hr; ok "Dry run complete — nothing created."; exit 0
  fi

  info "Requesting account creation (this takes a minute or two)"
  REQ_ID="$(aws organizations create-account \
    --email "${EMAIL}" \
    --account-name "${DISPLAY_NAME}" \
    --iam-user-access-to-billing ALLOW \
    --query 'CreateAccountStatus.Id' --output text | no_cr)"
  log "  request: ${REQ_ID}"

  # Poll. create-account is asynchronous and the only failure signal is in the
  # status record — there is no exception to catch.
  for _ in $(seq 1 60); do
    read -r STATE ACCOUNT_ID REASON <<<"$(aws organizations describe-create-account-status \
      --create-account-request-id "${REQ_ID}" \
      --query 'CreateAccountStatus.[State,AccountId,FailureReason]' --output text | no_cr)"
    case "${STATE}" in
      SUCCEEDED) ok "Account ${ACCOUNT_ID} created"; break ;;
      FAILED)    die "Account creation FAILED: ${REASON}
      EMAIL_ALREADY_EXISTS means the address is consumed permanently — pick another." ;;
      *)         printf '  %s ...\n' "${STATE}" ;;
    esac
    sleep 10
  done
  [[ "${STATE}" == "SUCCEEDED" ]] || die "Timed out waiting for account creation. Request: ${REQ_ID}"
fi

# --- place in the OU -------------------------------------------------------
CURRENT_PARENT="$(aws organizations list-parents --child-id "${ACCOUNT_ID}" \
  --query 'Parents[0].Id' --output text | no_cr)"
if [[ "${CURRENT_PARENT}" == "${TARGET_OU}" ]]; then
  skip "Already in the target OU."
else
  run "Move ${ACCOUNT_ID} into ${OU} OU" \
    aws organizations move-account --account-id "${ACCOUNT_ID}" \
      --source-parent-id "${CURRENT_PARENT}" --destination-parent-id "${TARGET_OU}"
fi

# --- set the account alias -------------------------------------------------
# Requires assuming into the account; the alias is account-local, not an
# Organizations attribute.
info "Setting account alias inside ${ACCOUNT_ID}"
CREDS="$(aws sts assume-role \
  --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-account-setup \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)" || true

if [[ -n "${CREDS}" ]]; then
  read -r AK SK ST <<<"${CREDS}"
  CUR="$(AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
    aws iam list-account-aliases --query 'AccountAliases[0]' --output text 2>/dev/null | no_cr | sed 's/^None$//')"
  if [[ -z "${CUR}" ]]; then
    AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
      aws iam create-account-alias --account-alias "${ALIAS}" \
      && ok "alias ${ALIAS}" \
      || warn "Could not set alias — it may be taken globally by another AWS customer."
  else
    skip "alias already ${CUR}"
  fi
else
  warn "Could not assume OrganizationAccountAccessRole yet — alias not set."
  warn "IAM is eventually consistent after account creation; re-run to finish."
fi

put_param "/org/account/${ALIAS}" "${ACCOUNT_ID}" "Platform account ${ALIAS} (${OU} OU)"

hr
ok "${ALIAS} = ${ACCOUNT_ID}"
log ""
log "AWS sends a welcome message to ${EMAIL}."
log "Confirm it arrives — that is the genuine end-to-end test of the"
log "plus-addressed mail path, and it closes deviation D-008."
