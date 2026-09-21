#!/usr/bin/env bash
#
# Store the PagerDuty REST API token in Secrets Manager, in every account that
# reads it, in one run.
#
#   scripts/set-pagerduty-token.sh              # prompts, input hidden
#   ... | scripts/set-pagerduty-token.sh --stdin
#   scripts/set-pagerduty-token.sh --propagate  # fan an existing value out
#
# ---------------------------------------------------------------------------
# An ACCOUNT key, not a personal one
# ---------------------------------------------------------------------------
# prompt 12.2: "Authenticate as a platform service account with tokens in
# Secrets Manager, rotated. Not a personal account."
#
# PagerDuty has no separate service-account object in the classic model. The
# equivalent is a **General Access REST API Key**, created under
# Integrations -> API Access Keys, which belongs to the account rather than to
# a person. That distinction is the whole requirement:
#
#   a user token     dies when the person leaves, consumes a seat, and
#                    attributes every platform action to a human who did not
#                    take it
#   an account key   survives staff changes and is revocable without
#                    disturbing anyone's login
#
# It must NOT be read-only. This creates services, escalation policies and
# business services.
#
# ---------------------------------------------------------------------------
# What reads it
# ---------------------------------------------------------------------------
#   tooling   provisioning renders a tenant's PagerDuty configuration from the
#             registry (saga step 3) and reads back what exists to confirm it
#   audit     the drift detector, alongside the rest of the detection estate
#
# And Terraform, run from a workstation by a person. `pagerduty-apply.sh`
# reads the token from Secrets Manager rather than requiring PAGERDUTY_TOKEN
# to be exported by hand — an exported credential outlives the shell it was
# meant for and ends up in a scrollback.
#
# ---------------------------------------------------------------------------
# Never on a command line
# ---------------------------------------------------------------------------
# No --token flag, deliberately. An argument is visible in the process list to
# every user on the machine and lands in shell history. Input comes from a
# hidden prompt or from stdin.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

FROM_STDIN=0
PROPAGATE=0
SECRET_NAME="platform/pagerduty/api-token"

# Overriding this narrows the fan-out, which is how a half-rotation gets done
# on purpose. Comma or space separated.
CONSUMER_KEYS="altdig-security-audit altdig-infra-tooling"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stdin)     FROM_STDIN=1; shift ;;
    --propagate) PROPAGATE=1; shift ;;
    --secret)    SECRET_NAME="$2"; shift 2 ;;
    --account)   CONSUMER_KEYS="${2//,/ }"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s [--stdin] [--propagate] [--secret <name>] [--account <alias>[,<alias>]]\n' "$0"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

mapfile -t ACCOUNTS < <(resolve_accounts ${CONSUMER_KEYS})
info "consuming accounts: ${ACCOUNTS[*]}"

if [[ "${PROPAGATE}" == "1" ]]; then
  PAYLOAD=""
  for entry in "${ACCOUNTS[@]}"; do
    candidate="$(read_secret "${entry##*:}" "${SECRET_NAME}")"
    if [[ -n "${candidate}" && "${candidate}" != "None" ]]; then
      PAYLOAD="${candidate}"
      ok "propagating the value already held by ${entry%%:*}"
      break
    fi
  done
  [[ -n "${PAYLOAD}" ]] \
    || die "--propagate found no existing token in any consuming account. Run without --propagate to store one."
else
  if [[ "${FROM_STDIN}" == "1" ]]; then
    read -r TOKEN
  else
    printf 'PagerDuty REST API key (input hidden): '
    # -s so it is never echoed to the terminal or captured by a screen recorder.
    read -rs TOKEN
    printf '\n'
  fi
  [[ -n "${TOKEN}" ]] || die "No token supplied."

  # Shape check only, and it is a warning rather than a refusal. PagerDuty's
  # general access keys have been 20 and 32 characters at different times, and
  # a length rule that hard-failed would reject a valid key the first time
  # they changed the format again. The value is never printed.
  if [[ ${#TOKEN} -lt 20 ]]; then
    warn "That key is shorter than any PagerDuty API key observed so far."
    warn "Storing it anyway; verify with scripts/pagerduty-check.sh."
  fi
  if [[ "${TOKEN}" == R0* ]]; then
    # PagerDuty prefixes read-only keys. Catching it here saves discovering it
    # as a 403 from a Terraform apply half way through creating a tenant.
    die "That looks like a READ-ONLY key (they begin R0). This must create services, escalation policies and business services, so it needs a full-access key."
  fi

  PAYLOAD="$(TOKEN="${TOKEN}" python -c '
import json, os
print(json.dumps({"api_token": os.environ["TOKEN"]}))
')"
  unset TOKEN
fi

store_secret_everywhere \
  "${SECRET_NAME}" \
  "PagerDuty REST API key for tenant provisioning (12.2) and drift detection. Account-level key, not a personal token. Written by scripts/set-pagerduty-token.sh." \
  "${PAYLOAD}" \
  "${ACCOUNTS[@]}"
unset PAYLOAD

printf '\n'
info "Next: scripts/pagerduty-check.sh confirms the key authenticates and is not read-only."
