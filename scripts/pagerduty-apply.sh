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
TENANT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    # One tenant's module only, and only if every change is a creation. For
    # onboarding, which must not apply anything it did not cause -- see the
    # tenant section at the end of this script.
    --tenant) TENANT="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: PAGERDUTY_TOKEN=... %s [--apply] [--tenant <client>]\n' "$0"; exit 0 ;;
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
  # ONE copy, in Audit (ADR-019, Jamie 2026-10-31). This read Tooling's
  # copy until then. read_secret assumes into the account in a subshell and
  # unsets AWS_PROFILE there: an empty AWS_PROFILE is read as a profile named
  # "" and fails, and an inline prefix cannot unset it. That trap is what
  # once made this script report a readable secret as missing.
  if require_cli 2>/dev/null && AUDIT="$(get_param /org/account/altdig-security-audit)" \
       && [[ -n "${AUDIT}" && "${AUDIT}" != "None" ]]; then
    info "Reading the API token from Secrets Manager (${AUDIT})"
    FETCHED="$(read_secret "${AUDIT}" platform/pagerduty/api-token)"
    if [[ -n "${FETCHED}" ]]; then
      PAGERDUTY_TOKEN="$(printf '%s' "${FETCHED}" \
        | "$(command -v python || command -v python3)" -c \
            'import json,sys; print(json.load(sys.stdin)["api_token"])')"
      export PAGERDUTY_TOKEN
      ok "token retrieved (${#PAGERDUTY_TOKEN} characters, not printed)"
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

# ---------------------------------------------------------------------------
# One tenant, creations only (decided 2026-10-01)
# ---------------------------------------------------------------------------
# Onboarding applies a new tenant's services itself instead of stopping for a
# person -- but only what that onboarding caused. The plan is limited to the
# tenant's module, saved, and checked: every change must be a CREATE inside
# module.tenant["<client>"]. Anything else -- an update, a destroy, a change
# outside the tenant -- stops for review, because that is a change to
# something people already depend on.
#
# The first real run showed why. The full plan for Arc8 also carried an
# in-place change to the platform on-call schedule: someone had added it to a
# PagerDuty team in the web console, and an unattended full apply would have
# quietly removed that. A targeted plan does not touch it, and still says so.
#
# With --apply, the SAVED plan is applied, so what was checked is exactly what
# happens; nothing can change between the check and the apply.
if [[ -n "${TENANT}" ]]; then
  [[ "${TENANT}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "--tenant must be a client slug."
  PLAN_FILE="tenant-${TENANT}.tfplan"
  TARGET="module.tenant[\"${TENANT}\"]"
  info "terraform plan, ${TARGET} only"
  (cd "${TFDIR}" && "${TF}" plan -input=false -target="${TARGET}" -out="${PLAN_FILE}") \
    || die "terraform plan failed for ${TARGET}."
  VERDICT="$(cd "${TFDIR}" && "${TF}" show -json "${PLAN_FILE}" | "$(command -v python || command -v python3)" -c '
import json, sys
prefix = sys.argv[1]
plan = json.load(sys.stdin)
creates, other = 0, []
for rc in plan.get("resource_changes") or []:
    actions = (rc.get("change") or {}).get("actions") or []
    if actions in (["no-op"], ["read"]):
        continue
    if actions == ["create"] and rc["address"].startswith(prefix):
        creates += 1
    else:
        other.append(rc["address"] + ": " + "/".join(actions))
print("creates=" + str(creates))
for o in other:
    print("other " + o)
' "${TARGET}")" || die "Could not read the saved plan."
  CREATES="$(printf '%s\n' "${VERDICT}" | sed -n 's/^creates=//p')"
  OTHER="$(printf '%s\n' "${VERDICT}" | sed -n 's/^other //p')"
  if [[ -n "${OTHER}" ]]; then
    rm -f "${TFDIR}/${PLAN_FILE}"
    die "The plan for ${TARGET} changes more than it creates:
$(printf '%s\n' "${OTHER}" | sed 's/^/        /')
      Not applied. Review with scripts/pagerduty-apply.sh and apply by hand."
  fi
  if [[ "${CREATES}" == "0" ]]; then
    rm -f "${TFDIR}/${PLAN_FILE}"
    ok "${TARGET}: nothing to create; already in place"
    exit 0
  fi
  ok "${TARGET}: ${CREATES} creation(s), nothing else"
  if [[ ${APPLY} -eq 1 ]]; then
    (cd "${TFDIR}" && "${TF}" apply -input=false "${PLAN_FILE}") || {
      rm -f "${TFDIR}/${PLAN_FILE}"; die "terraform apply failed for ${TARGET}."; }
    ok "applied ${CREATES} creation(s) for ${TENANT}"
  else
    info "Plan only. Re-run with --apply to create them."
  fi
  rm -f "${TFDIR}/${PLAN_FILE}"
  exit 0
fi

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
