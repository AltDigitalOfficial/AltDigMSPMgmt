#!/usr/bin/env bash
#
# Plan or apply the PagerDuty configuration.
#
#   PAGERDUTY_TOKEN=... scripts/pagerduty-apply.sh [--apply]
#
# Defaults to PLAN. Applying requires --apply, because this configuration owns
# an escalation policy and a schedule: a careless apply during an incident can
# retarget where pages go while someone is depending on them.
#
# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------
# Read from the environment, never from a file and never from an argument. An
# argument is visible in the process list to every user on the machine and in
# shell history; a file gets committed.
#
# Per open item B10 this should be a PagerDuty SERVICE ACCOUNT token. A
# personal token ties platform automation to one person's employment and
# carries their permissions rather than the automation's, so every apply is
# implicitly authorised as them. Acceptable to bootstrap with, not to stay on.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help)
      printf 'Usage: PAGERDUTY_TOKEN=... %s [--apply]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

TF="$(find_tool terraform)" || die "terraform not found. Run scripts/setup-tooling.sh."
TFDIR="${REPO_ROOT}/pagerduty"

# ---------------------------------------------------------------------------
# Token resolution: environment first, then Secrets Manager
# ---------------------------------------------------------------------------
# An explicit PAGERDUTY_TOKEN in the environment always wins. That is not just
# politeness about overrides — it is the path for running as a different
# identity without rewriting the stored secret, which matters when someone is
# testing a restricted token.
#
# Otherwise it is read from Platform Tooling. Two things that buys and one it
# does not:
#
#   rotation  one place, and nothing downstream needs redeploying, because the
#             token is read fresh on every run. Contrast the ROUTING keys,
#             which CloudFormation bakes into an SNS subscription endpoint at
#             deploy time and never re-resolves.
#   audit     every retrieval is a CloudTrail event. This token can delete
#             escalation policies — it can switch off every page the platform
#             sends — so knowing when it was used matters more for this
#             credential than for any other one here.
#   NOT       use-time secrecy. Terraform reads the token from its environment,
#             so it ends up in a process environment either way.
if [[ -z "${PAGERDUTY_TOKEN:-}" ]]; then
  export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
  if require_cli 2>/dev/null && TOOLING="$(get_param /org/account/altdig-infra-tooling)"      && [[ -n "${TOOLING}" && "${TOOLING}" != "None" ]]; then
    info "Reading the API token from Secrets Manager (${TOOLING})"
    TCREDS="$(aws sts assume-role       --role-arn "arn:aws:iam::${TOOLING}:role/OrganizationAccountAccessRole"       --role-session-name platform-pd-read       --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]'       --output text 2>/dev/null | no_cr)" || true
    if [[ -n "${TCREDS}" ]]; then
      read -r TAK TSK TST <<<"${TCREDS}"
      # A SUBSHELL with `unset`, not an inline AWS_PROFILE='' prefix.
      #
      # An empty AWS_PROFILE is not an absent one: the CLI reads it as a
      # profile literally named "" and fails with "The config profile () could
      # not be found". Inline prefixes cannot unset a variable, only set it —
      # so the only way to remove it for one command is a subshell.
      #
      # This exact trap is documented in scripts/test-alert-path.sh, and this
      # script walked into it anyway. The symptom was the worst kind: the
      # retrieval failed, 2>/dev/null swallowed the reason, and the script
      # reported "No PagerDuty API token available" — pointing at a secret that
      # existed and was perfectly readable.
      FETCHED="$( (unset AWS_PROFILE
        AWS_ACCESS_KEY_ID="${TAK}" AWS_SECRET_ACCESS_KEY="${TSK}"         AWS_SESSION_TOKEN="${TST}" MSYS_NO_PATHCONV=1         aws secretsmanager get-secret-value           --secret-id platform/pagerduty/api-token           --query SecretString --output text 2>/dev/null) | no_cr)" || true
      if [[ -n "${FETCHED}" ]]; then
        PAGERDUTY_TOKEN="$(printf '%s' "${FETCHED}"           | "$(command -v python || command -v python3)" -c             'import json,sys; print(json.load(sys.stdin)["api_token"])')"
        export PAGERDUTY_TOKEN
        ok "token retrieved (${#PAGERDUTY_TOKEN} characters, not printed)"
      fi
    fi
  fi
fi

[[ -n "${PAGERDUTY_TOKEN:-}" ]] || die "No PagerDuty API token available.

      Store one once and this script finds it from then on:
        scripts/set-pagerduty-token.sh

      Or export PAGERDUTY_TOKEN for a single run.

      PagerDuty -> Integrations -> API Access Keys -> Create New API Key.
      Leave 'Read-only API Key' UNCHECKED: this configuration creates a
      schedule, an escalation policy, a service and an integration.

      Create a GENERAL ACCESS key, not a User token (My Profile -> User
      Settings -> API Access). A General Access key belongs to the account
      rather than to a person, so platform automation does not stop working
      when someone leaves and does not silently inherit their permissions.
      That is open item B10, and this is what closes it for PagerDuty.

      PagerDuty displays the key once. There is no way to read it back."

[[ -f "${TFDIR}/terraform.tfvars" ]] || die "pagerduty/terraform.tfvars does not exist.
      cp pagerduty/terraform.tfvars.example pagerduty/terraform.tfvars
      then set responder_emails to the PagerDuty login email addresses of
      whoever should be paged. The users must already exist in PagerDuty —
      this configuration looks them up rather than creating them.
      The file is gitignored and blocked by the pre-commit hook: it holds
      direct email addresses, which are personal data under CLAUDE.md."

hr
log "PagerDuty configuration"
log "  directory : pagerduty/"
log "  mode      : $([[ ${APPLY} -eq 1 ]] && echo APPLY || echo PLAN)"
hr

info "terraform init"
(cd "${TFDIR}" && "${TF}" init -input=false)

info "terraform validate"
(cd "${TFDIR}" && "${TF}" validate)

if [[ ${APPLY} -eq 1 ]]; then
  info "terraform apply"
  (cd "${TFDIR}" && "${TF}" apply -input=false -auto-approve)
  ok "applied"
  hr
  info "Next: write the routing key into Secrets Manager"
  log "  scripts/sync-pagerduty-secrets.sh"
  log ""
  log "  Then redeploy the alerting stack so it creates the subscription:"
  log "    scripts/deploy-to-account.sh --account altdig-security-audit \\"
  log "      --template alerting/10-alert-topic.yaml --stack platform-alerting \\"
  log "      OrganizationId=\$(...) PagerDutySecretName=platform/pagerduty/routing-key"
  hr
else
  info "terraform plan"
  (cd "${TFDIR}" && "${TF}" plan -input=false)
  hr
  info "Plan only. Re-run with --apply to make these changes."
  hr
fi
