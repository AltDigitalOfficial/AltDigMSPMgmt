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

[[ -n "${PAGERDUTY_TOKEN:-}" ]] || die "PAGERDUTY_TOKEN is not set.

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
