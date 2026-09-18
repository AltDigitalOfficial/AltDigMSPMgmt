#!/usr/bin/env bash
#
# Create one client OU beneath an existing partner OU.
#
# Part of step 5 of the provisioning saga (design doc 13). The step 0 partner
# precondition — partner exists, is active, notification window non-null — is
# fully enforced as of 2026-09-17 and BLOCKS, which is what doc 13 requires.
#
# It warned rather than blocked for most of this build, because 'active' and
# the notification windows had nowhere to live until the platform registry
# landed. That was deviation D-004, now closed.
#
# The check itself is in scripts/registry.sh check-partner, not duplicated
# here.
#
# Usage:
#   scripts/create-client-ou.sh --partner oeight --slug arc8 \
#     --legal-name "Arc8" --app-owner partner --app-builder partner [--dry-run]

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

PARTNER=""; SLUG=""; LEGAL_NAME=""; APP_OWNER="client"; APP_BUILDER="client"

usage() {
  cat <<'USAGE'
Usage: create-client-ou.sh --partner <slug> --slug <slug> --legal-name <name>
                           [--app-owner <who>] [--app-builder <who>] [--dry-run]

  --partner                Partner slug this client sits beneath, or 'direct'
                           for an AltDigital client with no partner.
  --slug                   Machine-safe client id, 1-18 chars.
  --legal-name             Client legal or trading name.
  --app-owner              Who owns the application IP: client | partner.
                           Default client. 'partner' means doc 10's "the
                           customer owns their application" does not hold here,
                           and the exit clause must say what they leave with.
  --app-builder            Who writes and maintains it: client | partner |
                           third-party. Default client. Independent of
                           ownership: a partner can build under contract while
                           the client keeps the IP.
  --dry-run                Produce a changeset without executing it.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --partner)              PARTNER="$2"; shift 2 ;;
    --slug)                 SLUG="$2"; shift 2 ;;
    --legal-name)           LEGAL_NAME="$2"; shift 2 ;;
    --app-owner)            APP_OWNER="$2"; shift 2 ;;
    --app-builder)          APP_BUILDER="$2"; shift 2 ;;
    --dry-run)              DRY_RUN=1; shift ;;
    -h|--help)              usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${PARTNER}" ]]    || { usage; die "--partner is required."; }
[[ -n "${SLUG}" ]]       || { usage; die "--slug is required."; }
[[ -n "${LEGAL_NAME}" ]] || { usage; die "--legal-name is required."; }

validate_slug partner "${PARTNER}"
validate_slug client  "${SLUG}"

case "${APP_OWNER}" in client|partner) ;; *) die "--app-owner must be client or partner." ;; esac
case "${APP_BUILDER}" in client|partner|third-party) ;; *) die "--app-builder must be client, partner or third-party." ;; esac

# Worth surfacing at creation rather than at contract-review time.
if [[ "${APP_OWNER}" == "partner" ]]; then
  warn "ApplicationOwner=partner: design doc 10 states the customer owns their"
  warn "application and data. That does not hold for this client. The exit"
  warn "clause must state what ${SLUG} leaves with. Owned by Art and Wayne (X2)."
fi

# Account alias length check, done here at the point the segments are chosen
# rather than at account creation where a failure is far more expensive to
# unwind. Worst case is a client with multiple applications:
# altdig-<partner>-<client>-<app>-<env> with a full-length 10-char app code.
# Chained through account_email because the 64-octet local part is the TIGHTER
# of the two limits; both functions die on overflow, so this is the whole check.
account_email "${PARTNER}" "${SLUG}" "xxxxxxxxxx" "prod" >/dev/null
account_alias "${PARTNER}" "${SLUG}" "xxxxxxxxxx" "prod" >/dev/null

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# --- partner precondition, as far as it can be checked today ---------------

if [[ "${PARTNER}" == "direct" ]]; then
  PARTNER_OU="$(get_param /org/ou/members/direct)"
else
  PARTNER_OU="$(get_param "/org/ou/members/${PARTNER}")"
fi

[[ -n "${PARTNER_OU}" && "${PARTNER_OU}" != "None" ]] \
  || die "Partner '${PARTNER}' does not exist.
      A client cannot be onboarded beneath a partner that does not exist.
      Run: scripts/create-partner-ou.sh --slug ${PARTNER} --legal-name '<name>'"

EXISTING="$(find_ou "${PARTNER_OU}" "${SLUG}")"
STACK_NAME="platform-client-${PARTNER}-${SLUG}"
if [[ -n "${EXISTING}" ]]; then
  STACK_EXISTS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].StackName' --output text 2>/dev/null || true)"
  if [[ -z "${STACK_EXISTS}" || "${STACK_EXISTS}" == "None" ]]; then
    die "An OU named '${SLUG}' already exists under partner '${PARTNER}'
      (${EXISTING}) but is not managed by stack ${STACK_NAME}.
      Resolve by hand — deploying would create a duplicate and split billing."
  fi
  skip "Client OU '${SLUG}' exists (${EXISTING}); updating."
fi

hr
log "Client OU"
log "  partner      : ${PARTNER} (${PARTNER_OU})"
log "  slug         : ${SLUG}"
log "  legal name   : ${LEGAL_NAME}"
log "  app owner    : ${APP_OWNER}"
log "  app builder  : ${APP_BUILDER}"
log "  alias stem   : ${PLATFORM_ACCOUNT_PREFIX}-${PARTNER}-${SLUG}"
log "  root emails  : $(account_email "${PARTNER}" "${SLUG}" '' 'dev')"
log "                 $(account_email "${PARTNER}" "${SLUG}" '' 'test')"
log "                 $(account_email "${PARTNER}" "${SLUG}" '' 'uat')  (opt-in)"
log "                 $(account_email "${PARTNER}" "${SLUG}" '' 'prod')"
hr

# --- partner precondition, fully enforced (D-004) --------------------------
#
# This BLOCKS. Design doc 13 says it must, and for most of this build it
# warned instead because 'active' and the notification windows lived in a
# registry that did not exist. It exists now.
#
# The check is delegated to scripts/registry.sh rather than reimplemented,
# so there is exactly one definition of "may this partner have clients" —
# including the assertion that AltDigital's notification window is strictly
# TIGHTER than the partner's, not merely present. A second copy of that rule
# would drift from the first, and the failure would be a notification chain
# that cannot be met rather than a wrong number on a screen.
#
# 'direct' is exempt: an AltDigital client with no partner has no partner
# notification chain to be tighter than.
if [[ "${PARTNER}" != "direct" ]]; then
  info "Checking partner precondition in the registry"
  bash "${REPO_ROOT}/scripts/registry.sh" check-partner --slug "${PARTNER}"     || die "Partner precondition failed for '${PARTNER}'. No client OU created.

      Design doc 13: the partner must exist, be active, and carry a non-null
      notification window before any client is onboarded beneath it. This is a
      block, not a warning — a client under an incomplete partner has no
      defensible incident notification path."
fi
hr

cfn_deploy "${STACK_NAME}" "${REPO_ROOT}/org/20-client-ou.yaml" \
  "PartnerSlug=${PARTNER}" \
  "ClientSlug=${SLUG}" \
  "ClientLegalName=${LEGAL_NAME}" \
  "ApplicationOwner=${APP_OWNER}" \
  "ApplicationBuilder=${APP_BUILDER}" \
  "PartnerOuId=${PARTNER_OU}" \
  "SsmPrefix=${PLATFORM_SSM_PREFIX}"

if [[ "${DRY_RUN}" != "1" ]]; then
  hr
  cfn_outputs "${STACK_NAME}" | sed 's/^/  /'
fi
