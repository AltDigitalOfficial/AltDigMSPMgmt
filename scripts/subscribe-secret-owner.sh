#!/usr/bin/env bash
#
# Subscribe an owner role's mailbox to its monthly credential calendar
# (security/90-secret-age.yaml, ADR-021).
#
#   scripts/subscribe-secret-owner.sh <owner-role> [--dry-run]
#
# The calendar goes to SNS topic platform-owner-<owner-role> in Audit. This
# script puts an email subscription on it.
#
# ---------------------------------------------------------------------------
# Which address
# ---------------------------------------------------------------------------
# Jamie, 2026-11-01: platform-operations is reached at the root account's
# email address, which is aliased to a person today "but not forever". So the
# address is read from Organizations, as the management account's own email,
# at run time. It is never in the repository, never in a template, and never
# printed. When the root mailbox changes, re-running this subscribes the new
# address.
#
# Only platform-operations has an address source today. A new owner role
# needs its own topic in 90-secret-age.yaml and a source here, decided when it
# exists.
#
# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------
# SNS email subscriptions stay PendingConfirmation until the recipient clicks
# the link AWS sends. Until then the calendar is not delivered, and the
# monthly run reports that in the digest rather than publishing into nothing.
# This script reports the state on every run.
#
# A subscription for an address that is no longer the root's is listed and
# left alone. Removing it is a deliberate step, not a side effect.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

OWNER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '3,7p' "$0"; exit 0 ;;
    -*)        die "Unknown argument: $1" ;;
    *)         OWNER="$1"; shift ;;
  esac
done

[[ -n "${OWNER}" ]] || die "Usage: subscribe-secret-owner.sh <owner-role> [--dry-run]"
[[ "${OWNER}" == platform-operations ]] \
  || die "No address source is defined for ${OWNER}. Only platform-operations has one (ADR-021)."

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
AUDIT="$(get_param /org/account/altdig-security-audit)"
[[ -n "${AUDIT}" && "${AUDIT}" != "None" ]] || die "Audit account not registered."

TOPIC="arn:aws:sns:${PLATFORM_HOME_REGION}:${AUDIT}:platform-owner-${OWNER}"
in_account "${AUDIT}" aws sns get-topic-attributes --topic-arn "${TOPIC}" \
  --query Attributes.TopicArn --output text >/dev/null 2>&1 \
  || die "Topic platform-owner-${OWNER} does not exist in Audit. Deploy security/90-secret-age.yaml first."

# Held in a variable only. Never echoed.
ADDRESS="$(aws organizations describe-account --account-id "${PLATFORM_MGMT_ACCOUNT_ID}" \
  --query Account.Email --output text | no_cr)"
[[ "${ADDRESS}" == *@* ]] || die "Could not read the management account's email from Organizations."

# Endpoint and state, tab separated. Compared here, never printed.
SUBS="$(in_account "${AUDIT}" aws sns list-subscriptions-by-topic --topic-arn "${TOPIC}" \
  --query 'Subscriptions[?Protocol==`email`].[Endpoint,SubscriptionArn]' --output text | no_cr)"

STATE=""; OTHERS=0
while IFS=$'\t' read -r endpoint sub; do
  [[ -z "${endpoint}" ]] && continue
  if [[ "${endpoint,,}" == "${ADDRESS,,}" ]]; then
    [[ "${sub}" == arn:* ]] && STATE="Confirmed" || STATE="${sub}"
  else
    OTHERS=$((OTHERS + 1))
  fi
done <<<"${SUBS}"

hr
log "Credential calendar mailbox: ${OWNER}"
log "  topic   : platform-owner-${OWNER} (Audit)"
log "  address : the management account's root email (read from Organizations, not shown)"
hr

if [[ -z "${STATE}" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  would subscribe the root mailbox to platform-owner-%s\n' "${C_YELLOW}" "${C_RESET}" "${OWNER}"
  else
    in_account "${AUDIT}" aws sns subscribe --topic-arn "${TOPIC}" --protocol email \
      --notification-endpoint "${ADDRESS}" --query SubscriptionArn --output text >/dev/null \
      || die "Subscribing failed."
    STATE="PendingConfirmation"
    ok "subscribed. AWS has sent a confirmation email to the root mailbox."
  fi
fi
unset ADDRESS

case "${STATE}" in
  Confirmed)
    ok "root mailbox: Confirmed. The calendar will be delivered." ;;
  "")
    : ;;  # dry run, nothing subscribed
  *)
    warn "root mailbox: ${STATE}. Nothing is delivered until the link in AWS's email is clicked."
    warn "The monthly run reports this in the digest until then." ;;
esac

if [[ "${OTHERS}" -gt 0 ]]; then
  warn "${OTHERS} other email subscription(s) on this topic, for an address that is not the root's."
  warn "Left alone. If the root mailbox changed, remove the old one deliberately."
fi
