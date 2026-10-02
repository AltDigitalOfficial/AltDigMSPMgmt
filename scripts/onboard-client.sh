#!/usr/bin/env bash
#
# Onboard a client: questionnaire in, working AWS estate out.
#
#   scripts/onboard-client.sh --questionnaire q.yaml [--roster r.yaml] \
#     [--truveon-roster t.yaml] --dry-run
#   scripts/onboard-client.sh --questionnaire q.yaml [--roster r.yaml] \
#     [--truveon-roster t.yaml] --confirm-onboard
#
# design/13-client-provisioning.md, driven from a workstation.
#
# ---------------------------------------------------------------------------
# Why this exists when provisioning/src/saga.py already does it
# ---------------------------------------------------------------------------
# It does not, yet. `provisioning/src/` has no CloudFormation template and no
# publish script: the saga is written and is not deployed, so there is no
# `platform-provisioning` function to invoke. Four of its ten steps also raise
# StepNotImplemented by design.
#
# More fundamentally, half of this work CANNOT live in a Lambda. PagerDuty is
# applied by Terraform from a working copy, and the renderer writes a file into
# the repository — design 13 requires PagerDuty to be "versioned, staged,
# drift-detected" exactly like the AWS baseline, and a Lambda calling the API
# would put every tenant service outside Terraform state and make the drift
# detector fire on our own provisioning.
#
# So this is the operator-side half, and it is deliberately the same SHAPE as
# the saga rather than a different design: numbered steps in design 13's order,
# each one idempotent, resumable with --from, and failing closed. When the saga
# is deployed the two should agree step for step, and anywhere they disagree is
# a bug in one of them.
#
# ---------------------------------------------------------------------------
# PagerDuty is applied TWICE, and that is not a mistake
# ---------------------------------------------------------------------------
# This is the one place the design's ordering is misleading, and it is worth
# reading before running anything.
#
# design 13 puts PagerDuty at step 3, before the AWS accounts, because external
# systems fail on credentials and failing early is cheap. That reasoning is
# right. But `pagerduty/modules/tenant/main.tf` builds its tier business
# services and ALL of its technical services from `var.tiers`, and
# scripts/render-pagerduty-tenants.sh derives tiers from ACCOUNT records in the
# registry. Before vesting there are no accounts, so:
#
#   pass 1 (step 3, before accounts)  one client-conversation business service.
#                                     Proves the credential and the module.
#                                     No tier services. NO ROUTING KEYS.
#   pass 2 (step 7, after accounts)   tier business services, one technical
#                                     service per tier x app plus `infra`, and
#                                     the routing keys the alarm layer needs.
#
# Pass 2 is not optional and is not a refinement. Until it runs, the tenant has
# alarms and nowhere for them to go — which is the exact failure design 13 put
# PagerDuty first to avoid.
#
# Note that provisioning/src/saga.py cannot see this: its step 3 check keys on
# `pagerduty_business_service_id`, which pass 1 sets. A saga run would mark
# step 3 done and never perform pass 2. Recorded as B-132.
#
# ---------------------------------------------------------------------------
# What this will not do
# ---------------------------------------------------------------------------
# Nothing here approves go-live, and nothing here creates a Jira project.
#
# Go-live is a human decision — three things start on the transition to live
# (SLA measurement, billing, on-call rotation) and none of them should start
# because a script reached its last line.
#
# The Jira project is a capability gap, not a policy: the platform credential
# holds no site-level ADMINISTER permission and cannot create one (B-108). The
# step says so and stops rather than pretending a label is a project.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

QUESTIONNAIRE=""; ROSTER=""; TRUVEON_ROSTER=""; CONFIRM=0; SKIP_TRUVEON=0
DEFER_JIRA=0; FROM_STEP=0; ONLY_ENVS=""

usage() {
  cat <<'USAGE'
Usage: onboard-client.sh --questionnaire <path> [options]

  --questionnaire   Completed questionnaire, YAML. Required.
  --roster          Developer roster sidecar. Without it, step 9 is skipped
                    and nobody gets access to the accounts.
  --truveon-roster  Truveon roster sidecar (questionnaire 16.5): who gets
                    accounts in the client's Truveon tenant. Required when
                    step 2 has to CREATE the tenant.
  --dry-run         Print every step and create nothing. Unlike a real run,
                    a dry run CONTINUES past a blocked step so that one
                    rehearsal reports every problem rather than the first.
  --confirm-onboard Required for a real run. Creates AWS accounts, which
                    cannot be deleted.
  --skip-truveon    Proceed without a linked Truveon tenant, recording a
                    declared override against every account. For when
                    Truveon itself is unavailable, not as the normal path.
  --defer-jira      Proceed past the Jira step, which cannot be automated
                    here. Without this a real run stops at step 4.
  --from N          Resume at step N. Every step is idempotent, so this is a
                    convenience rather than a requirement.
  --environments    Vest only these environments now (e.g. dev,test), when the
                    account quota cannot hold the whole set. Re-run without it
                    later; existing accounts are found, not recreated.
  --list            Print the steps and exit.

Two kinds of stop, and the difference matters:

  blocked   something failed or a precondition is unmet. A real run STOPS,
            because everything after it would build on something absent.
  manual    the step cannot be automated from here at all. Recorded, listed
            at the end, and the run CONTINUES — an operator does these in
            parallel, and halting on them would mean no real run ever
            reached step 5.
USAGE
}

# ---------------------------------------------------------------------------
# The steps, in design 13's order
# ---------------------------------------------------------------------------
# Numbered separately from design 13's because this splits PagerDuty in two and
# adds developer access, which design 13 does not number at all. The mapping is
# in the third column and is checked by --list.

STEP_NAMES=(
  "0|partner-precondition|design 13 step 0"
  "1|registry-client-record|design 13 step 1"
  "2|truveon-tenant|design 13 step 2"
  "3|pagerduty-pass-1|design 13 step 3, business service only"
  "4|jira-tenant-artefacts|design 13 step 4"
  "5|aws-accounts-and-baseline|design 13 step 5"
  "6|truveon-accounts|design 13 step 2 again, accounts registered"
  "7|pagerduty-pass-2|design 13 step 3 again, tier + technical services"
  "8|monitoring-and-schedules|design 13 step 6"
  "9|developer-access|not numbered in design 13; questionnaire 3b"
  "10|cost-categories|design 13 step 7"
  "11|verification-sweep|design 13 step 8"
  "12|go-live-gate|design 13 step 9, never automated"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --questionnaire)   QUESTIONNAIRE="$2"; shift 2 ;;
    --roster)          ROSTER="$2"; shift 2 ;;
    --truveon-roster)  TRUVEON_ROSTER="$2"; shift 2 ;;
    --dry-run)         DRY_RUN=1; shift ;;
    --confirm-onboard) CONFIRM=1; shift ;;
    --skip-truveon)    SKIP_TRUVEON=1; shift ;;
    --defer-jira)      DEFER_JIRA=1; shift ;;
    --from)            FROM_STEP="$2"; shift 2 ;;
    --environments)    ONLY_ENVS="$2"; shift 2 ;;
    --list)
      printf '%-4s %-28s %s\n' "STEP" "NAME" "DESIGN 13"
      for s in "${STEP_NAMES[@]}"; do
        IFS='|' read -r n name ref <<<"${s}"
        printf '%-4s %-28s %s\n' "${n}" "${name}" "${ref}"
      done
      exit 0 ;;
    -h|--help)         usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${QUESTIONNAIRE}" ]] || { usage; die "--questionnaire is required."; }
[[ -f "${QUESTIONNAIRE}" ]] || die "Not found: ${QUESTIONNAIRE}"
[[ -z "${ROSTER}" || -f "${ROSTER}" ]] || die "Not found: ${ROSTER}"
[[ -z "${TRUVEON_ROSTER}" || -f "${TRUVEON_ROSTER}" ]] || die "Not found: ${TRUVEON_ROSTER}"

if [[ "${DRY_RUN}" != "1" && ${CONFIRM} -ne 1 ]]; then
  die "Refusing to onboard without --confirm-onboard.

      Step 5 creates AWS accounts. They cannot be deleted, only closed, and
      closure suspends them for 90 days while permanently consuming their root
      email addresses.

      Rehearse first:
        scripts/onboard-client.sh --questionnaire ${QUESTIONNAIRE} --dry-run"
fi

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

PY="$(command -v python || command -v python3)" || die "python not found."
DERIVE_PY="$(win_path "${REPO_ROOT}/scripts/derive-vesting.py")"
QPATH="$(win_path "${QUESTIONNAIRE}")"

# ---------------------------------------------------------------------------
# Identity, derived once
# ---------------------------------------------------------------------------
info "Reading the questionnaire"
"${PY}" "${DERIVE_PY}" --questionnaire "${QPATH}" >/dev/null \
  || die "The questionnaire is not vestable. Nothing was attempted.

      Run scripts/derive-vesting.py --questionnaire ${QUESTIONNAIRE} to see why."

DERIVED="$("${PY}" "${DERIVE_PY}" --questionnaire "${QPATH}" --json)" \
  || die "Derivation failed."

q_get() {
  "${PY}" -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1],encoding='utf-8'));print(eval('d'+sys.argv[2]))" \
    "${QPATH}" "$1"
}

PARTNER="$(q_get "['identity']['partner']")"
CLIENT="$(q_get "['identity']['client_slug']")"
APP="$(q_get "['identity']['application_code']")"
CLIENT_NAME="$(q_get "['identity']['client_name']")"
APP_OWNER="$(q_get "['identity']['partner_is_application_owner']")"
# Written onto the client record at step 1 so the digest and change
# reconciliation have a recipient from the first run. Same fields, same
# reason, as vest-client.sh step 4.
TECH_EMAIL="$(q_get "['identity']['technical_contact']['email']")"
DEV_MGR_EMAIL="$(q_get "['security']['developer_manager']['email']")"
APPROVER_EMAIL="$(q_get "['identity']['change_approver']['email']")"
BREACH_EMAIL="$(q_get "['identity']['breach_notification_contact']['email']")"
SECOPS_EMAIL="$(q_get ".get('identity',{}).get('security_contact',{}).get('email') or ''")"
# Questionnaire change.ticketing (form 12.4). No project when the system is
# 'none'; a site only when the records live on the client's own Atlassian site.
JIRA_PROJECT="$(q_get "['change']['ticketing'].get('project_key') or ''")"
JIRA_SITE_URL="$(q_get "['change']['ticketing'].get('site') or ''")"
ENVS="$(printf '%s' "${DERIVED}" \
  | "${PY}" -c "import sys,json;print(' '.join(json.load(sys.stdin)['environments']))")"
TENANT_ID="${PARTNER}-${CLIENT}-${APP}"

hr
log "Onboarding ${CLIENT_NAME}"
log "  tenant      : ${PARTNER}/${CLIENT}/${APP}"
log "  environments: ${ENVS}"
log "  roster      : ${ROSTER:-<none supplied — step 9 will be skipped>}"
log "  truveon     : $([[ ${SKIP_TRUVEON} -eq 1 ]] && echo 'SKIPPED (override recorded)' || echo "required — one tenant per client")"
log "  mode        : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo EXECUTE)"
[[ ${FROM_STEP} -gt 0 ]] && log "  resuming at : step ${FROM_STEP}"
hr

# ---------------------------------------------------------------------------
# Step bookkeeping
# ---------------------------------------------------------------------------
# A dry run continues past a block; a real run stops at the first one.
#
# The asymmetry is deliberate. A rehearsal exists to find every problem in one
# pass, and stopping at the first would mean one run per problem. A real run is
# creating things that cannot be uncreated, so continuing past a step that
# failed means building on top of something that is not there.

RESULTS=()
HALTED=""

record() { RESULTS+=("$1|$2|$3|$4"); }

should_run() {
  local n="$1"
  [[ ${n} -ge ${FROM_STEP} ]] || { record "${n}" "$2" "skipped" "before --from ${FROM_STEP}"; return 1; }
  [[ -z "${HALTED}" ]] || { record "${n}" "$2" "not-reached" "halted at step ${HALTED}"; return 1; }
  hr
  info "step ${n} — $2"
  return 0
}

# blocked <n> <name> <reason>
#
# Something failed, or a precondition is not met. On a real run this stops
# everything after it, because the later steps would be building on top of
# something that is not there.
#
# Returns 0 so that `set -e` does not turn a recorded block into an
# unexplained abort with no message.
blocked() {
  local n="$1" name="$2" reason="$3"
  record "${n}" "${name}" "blocked" "${reason}"
  warn "step ${n} (${name}) BLOCKED"
  while IFS= read -r line; do warn "    ${line}"; done <<< "${reason}"
  if [[ "${DRY_RUN}" != "1" ]]; then
    HALTED="${n}"
  else
    warn "    (dry run — continuing so this rehearsal reports every block)"
  fi
  return 0
}

# manual <n> <name> <reason>
#
# The step cannot be automated from here — no deployed function, or a
# capability the platform credential does not hold. Distinct from `blocked`,
# and the distinction is the difference between a usable script and a useless
# one.
#
# Steps 4, 11 and 12 can NEVER succeed from a workstation. If they halted the
# way a failure does, a real run would stop at step 4 every single time and
# nothing would ever reach account creation — the operator would be forced to
# pass --from 5 on every run, which trains people to skip steps.
#
# So these record, warn, and continue. They are collected into an OUTSTANDING
# list at the end, which is the thing that must be read: a tenant with manual
# steps outstanding is not finished, and the summary says so in those words
# rather than leaving it to be inferred from a table.
MANUAL=()
manual() {
  local n="$1" name="$2" reason="$3"
  record "${n}" "${name}" "manual" "${reason}"
  MANUAL+=("${n}|${name}|${reason}")
  warn "step ${n} (${name}) — MANUAL, cannot be automated from here"
  while IFS= read -r line; do warn "    ${line}"; done <<< "${reason}"
  return 0
}

# Runs a sub-script, recording pass or block rather than aborting. Output goes
# to a FILE and the file is read afterwards, never piped: CLAUDE.md trap 3 —
# a pipeline returns the last command's status, so a `set -e` abort inside the
# child would be reported as success.
attempt() {
  local n="$1" name="$2"; shift 2
  local out rc
  out="$(mktemp)"
  if "$@" >"${out}" 2>&1; then rc=0; else rc=$?; fi
  sed 's/^/    /' "${out}"
  rm -f "${out}"
  if [[ ${rc} -eq 0 ]]; then
    record "${n}" "${name}" "$([[ "${DRY_RUN}" == "1" ]] && echo dry || echo done)" ""
    ok "step ${n} (${name}) complete"
  else
    blocked "${n}" "${name}" "$* exited ${rc}. See the output above."
  fi
  return 0
}

DRY_FLAG=()
[[ "${DRY_RUN}" == "1" ]] && DRY_FLAG=(--dry-run)

# ===========================================================================
# 0 — Partner precondition
# ===========================================================================
# Blocks, does not warn. design 13: a tenant under an inactive partner has
# nobody to notify.

if should_run 0 partner-precondition; then
  if [[ "${PARTNER}" == "direct" ]]; then
    # AltDigital's own client, with no partner above them. create-client-ou.sh
    # and vest-client.sh both exempt it; this must too, or a direct client
    # could never be onboarded — a failure that only appears the first time
    # AltDigital sells to its own customer.
    record 0 partner-precondition "skipped" "'direct' has no partner above it"
    skip "'direct' has no partner above it"
  elif bash "${REPO_ROOT}/scripts/registry.sh" check-partner --slug "${PARTNER}" >/dev/null 2>&1; then
    record 0 partner-precondition "done" ""
    ok "partner ${PARTNER} is active with a notification window"
  else
    blocked 0 partner-precondition \
"Partner '${PARTNER}' is not present, not active, or has a null notification
window.

design 13 requires this to block rather than warn. The notification window is
the input to the incident clock, and a tenant beneath a partner without one has
no defensible answer to 'by when' at the moment of a breach.

  scripts/registry.sh get-partner --slug ${PARTNER}
  scripts/registry.sh put-partner --slug ${PARTNER} --state active \\
    --notify-hours <N> --altdig-hours <N>"
  fi
fi

# ===========================================================================
# 1 — Registry client record
# ===========================================================================
# The spine, created first and in `provisioning`. Everything later is
# idempotent against it, so a run that dies halfway is re-runnable rather than
# unpickable.

if should_run 1 registry-client-record; then
  IP_OWNER="$([[ "${APP_OWNER}" == "True" ]] && echo partner || echo client)"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  registry put-client %s/%s state=provisioning ip-owner=%s contacts=5 jira=%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${CLIENT}" "${IP_OWNER}" "${JIRA_PROJECT:-none}"
    record 1 registry-client-record "dry" ""
  else
    attempt 1 registry-client-record \
      bash "${REPO_ROOT}/scripts/registry.sh" put-client \
        --partner "${PARTNER}" --slug "${CLIENT}" --state provisioning \
        --ip-owner "${IP_OWNER}" \
        --tech-contact "${TECH_EMAIL}" --dev-manager "${DEV_MGR_EMAIL}" \
        --change-approver "${APPROVER_EMAIL}" --jira-project "${JIRA_PROJECT}" \
        --breach-contact "${BREACH_EMAIL}" --security-contact "${SECOPS_EMAIL}" \
        --jira-site "${JIRA_SITE_URL}"
  fi
fi

# ===========================================================================
# 2 — Truveon tenant
# ===========================================================================
# Before any account emits evidence. One tenant per CLIENT (B-023), so a second
# application for an onboarded client finds it already linked and passes.
#
# Done means LINKED: the registry names the client's Truveon tenant and the
# tenant's ingest key is stored where the evidence sender reads it. Checked by
# truveon-tenant-handoff.sh status, which is the same check an operator runs.
#
# Not done means a person runs Truveon's CLI -- Truveon cannot create a tenant
# over an API, deliberately (Truveon D-26). So this writes the handoff pack and
# BLOCKS. It blocks rather than recording a manual step because vesting (step
# 5) refuses accounts for a client with no linked tenant: carrying on here
# would only move the stop three steps later, after PagerDuty and Jira work
# that would then have to be resumed around it.

if should_run 2 truveon-tenant; then
  if [[ ${SKIP_TRUVEON} -eq 1 ]]; then
    record 2 truveon-tenant "skipped" "--skip-truveon; override recorded at step 5"
    warn "Truveon SKIPPED by --skip-truveon."
    warn "  vest-client.sh records a declared, owned, dated override against"
    warn "  every account, so this appears in registry.sh report-overrides"
    warn "  rather than being forgotten. Evidence for this tenant is buffered"
    warn "  and pending, and the pipeline names it in a no-credential finding"
    warn "  every hour until the tenant is linked."
  elif bash "${REPO_ROOT}/scripts/truveon-tenant-handoff.sh" status \
         --partner "${PARTNER}" --client "${CLIENT}" >/dev/null 2>&1; then
    record 2 truveon-tenant "done" "already linked"
    ok "Truveon tenant linked and ingest key stored for ${PARTNER}/${CLIENT}"
  else
    PACK_OUT="$(mktemp)"
    if bash "${REPO_ROOT}/scripts/truveon-tenant-handoff.sh" prepare \
         --questionnaire "${QUESTIONNAIRE}" \
         ${TRUVEON_ROSTER:+--truveon-roster "${TRUVEON_ROSTER}"} \
         "${DRY_FLAG[@]}" >"${PACK_OUT}" 2>&1; then
      sed 's/^/    /' "${PACK_OUT}"; rm -f "${PACK_OUT}"
      blocked 2 truveon-tenant \
"The client's Truveon tenant is not linked yet. The handoff pack is written:

  completed_qs/${PARTNER}-${CLIENT}.truveon/

Hand it to a Claude Code session in the Truveon repository (PROMPT.md), or run
commands.sh by hand from Truveon's infra/scripts. Its last step stores the
ingest key here and links the tenant; then resume:

  scripts/onboard-client.sh --questionnaire ${QUESTIONNAIRE} \\
    ${TRUVEON_ROSTER:+--truveon-roster ${TRUVEON_ROSTER} }--from 2 --confirm-onboard

A person runs this because Truveon cannot create a tenant over an API, by
design (Truveon D-26). design 13 puts it before the accounts so that no
account exists without somewhere for its evidence to go."
    else
      sed 's/^/    /' "${PACK_OUT}"; rm -f "${PACK_OUT}"
      blocked 2 truveon-tenant \
"The Truveon handoff pack could not be prepared. See the output above.

Most often: the questionnaire has no 'truveon' section (16), or the tenant is
being created and no --truveon-roster was supplied to choose its owner."
    fi
  fi
fi

# ===========================================================================
# 3 — PagerDuty, pass 1
# ===========================================================================
# Proves the credential and creates the client-conversation business service.
# No tier services and no routing keys — see the header.

if should_run 3 pagerduty-pass-1; then
  # The credential is proved here, before any account exists -- design 13's
  # reason for putting PagerDuty first. It used to be proved by demanding
  # PAGERDUTY_TOKEN in this shell, which contradicted the rest of the
  # platform: the token lives in Secrets Manager (set-pagerduty-token.sh), and
  # pagerduty-apply.sh reads it from there precisely so nobody exports it and
  # leaves it in a scrollback. The renderer below does not use the token at
  # all. So: an exported token still wins, as it does in pagerduty-apply.sh;
  # otherwise pagerduty-check.sh confirms the STORED key authenticates, with a
  # read-only call, and reports write access as unproven.
  PD_CRED_OK=0
  PD_CHECK_OUT="$(mktemp)"
  if [[ -n "${PAGERDUTY_TOKEN:-}" ]]; then
    PD_CRED_OK=1
  elif bash "${REPO_ROOT}/scripts/pagerduty-check.sh" >"${PD_CHECK_OUT}" 2>&1; then
    PD_CRED_OK=1
    ok "stored PagerDuty API key authenticates (pagerduty-check.sh)"
  fi
  if [[ ${PD_CRED_OK} -eq 0 ]]; then
    sed 's/^/    /' "${PD_CHECK_OUT}"
    rm -f "${PD_CHECK_OUT}"
    blocked 3 pagerduty-pass-1 \
"No usable PagerDuty API key: none exported, and the stored key in Secrets
Manager did not authenticate (see pagerduty-check.sh's output above).

design 13 puts the external systems before the AWS accounts precisely so that a
credential problem costs nothing. This is that check working.

  scripts/set-pagerduty-token.sh        # store or rotate it for the platform"
  else
    rm -f "${PD_CHECK_OUT}"
    attempt 3 pagerduty-pass-1 \
      bash "${REPO_ROOT}/scripts/render-pagerduty-tenants.sh" "${DRY_FLAG[@]}"
    if [[ "${DRY_RUN}" != "1" && -z "${HALTED}" ]]; then
      log ""
      log "  Renderer done. Review and apply before continuing:"
      log "    scripts/pagerduty-apply.sh            # read the plan"
      log "    scripts/pagerduty-apply.sh --apply"
      log "    scripts/sync-pagerduty-tenants.sh     # keys + registry write-back"
      log ""
      log "  Not run automatically. design 13 treats PagerDuty like the AWS"
      log "  baseline — staged, reviewed, then applied — and an apply nobody"
      log "  read is the thing that makes a drift detector useless."
    fi
  fi
fi

# ===========================================================================
# 4 — Jira tenant artefacts
# ===========================================================================
# Blocks. Two separate reasons, and both are real.

if should_run 4 jira-tenant-artefacts; then
  # Blocks by DEFAULT and is deferrable with a flag, the same shape as
  # --skip-truveon. design 13 puts Jira before the accounts so reconciliation
  # has a target, and defaulting to "carry on" would quietly discard that
  # ordering. Defaulting to a stop that a flag releases keeps the decision
  # visible and deliberate, which is the same trade vest-client.sh makes.
  JIRA_REASON=\
"Jira tenant provisioning cannot run from here.

1. NOT DEPLOYED. provisioning/src/jira_tenant.py exists and has no
   CloudFormation template and no publish script, so there is no
   platform-provisioning function to invoke. It cannot be run from this
   workstation either: that would pull the Jira credential out of Secrets
   Manager into a laptop's process environment, which is what
   scripts/set-jira-token.sh exists to avoid.

2. NO PROJECT, EVEN THEN (B-108). The platform credential holds no site-level
   ADMINISTER permission, so it cannot create a project, and no
   ADMINISTER_PROJECTS on the ops project, so it cannot create a component
   either. What it can create is a LABEL, an onboarding epic and a JSM
   organization in the shared ops project — which is a naming convention
   rather than a permission boundary, and gives this tenant no confidentiality
   separation from any other. B-109.

For now, by hand:
  - create the change project from the golden scheme, or agree that this
    tenant uses the shared ops project
  - record the key in the questionnaire's change.ticketing.project_key
  - scripts/jira-check.sh   # confirms the platform can read it"

  if [[ ${DEFER_JIRA} -eq 1 ]]; then
    manual 4 jira-tenant-artefacts "${JIRA_REASON}"
    warn ""
    warn "  Deferred by --defer-jira. Change reconciliation has no target for"
    warn "  this tenant until the project exists: design 08's engine reports"
    warn "  the change source as UNAVAILABLE rather than reporting every"
    warn "  deployment as unapproved, so this fails visibly rather than"
    warn "  silently — but it does not fail here."
  else
    blocked 4 jira-tenant-artefacts \
"${JIRA_REASON}

Pass --defer-jira to continue past this step. Nothing later depends on it
technically; design 13 orders it first so that reconciliation has a target
before there is anything to reconcile."
  fi
fi

# ===========================================================================
# 5 — AWS accounts and baseline
# ===========================================================================
# The irreversible one. vest-client.sh owns it and re-does steps 0 and 1 for
# itself, which is correct — it must be safe to run on its own.

if should_run 5 aws-accounts-and-baseline; then
  # if/else rather than `test && a || b`. vest-client.sh states that there is
  # deliberately no flag doing both, and the && || form silently falls through
  # to the second branch if the first ever returns non-zero — which here would
  # mean passing --confirm-vest during a dry run.
  VEST_ARGS=(--questionnaire "${QUESTIONNAIRE}")
  if [[ "${DRY_RUN}" == "1" ]]; then
    VEST_ARGS+=(--dry-run)
  else
    VEST_ARGS+=(--confirm-vest)
  fi
  if [[ ${SKIP_TRUVEON} -eq 1 ]]; then
    VEST_ARGS+=(--defer-truveon)
  fi
  # Staged vesting. Everything after step 5 reads the accounts from the
  # registry, so a later run without --environments picks up the rest.
  if [[ -n "${ONLY_ENVS}" ]]; then
    VEST_ARGS+=(--environments "${ONLY_ENVS}")
  fi
  attempt 5 aws-accounts-and-baseline \
    bash "${REPO_ROOT}/scripts/vest-client.sh" "${VEST_ARGS[@]}"
fi

# ===========================================================================
# 6 — Truveon, accounts
# ===========================================================================
# Step 2's second pass, for the same reason PagerDuty has one: before vesting
# there were no accounts to register. The pack is regenerated with them and the
# operator re-runs only the configure step in Truveon, which is idempotent.
#
# MANUAL, not blocked. Nothing later on this side depends on it -- evidence from
# an account Truveon has not registered is stored and flagged there, not
# refused -- and it cannot be confirmed from here: whether Truveon registered
# the accounts is answered by the verification sweep from the Audit account,
# which holds the key, not from a laptop, which does not.

if should_run 6 truveon-accounts; then
  if [[ ${SKIP_TRUVEON} -eq 1 ]]; then
    record 6 truveon-accounts "skipped" "--skip-truveon"
  else
    PACK_OUT="$(mktemp)"
    if bash "${REPO_ROOT}/scripts/truveon-tenant-handoff.sh" prepare \
         --questionnaire "${QUESTIONNAIRE}" \
         ${TRUVEON_ROSTER:+--truveon-roster "${TRUVEON_ROSTER}"} \
         "${DRY_FLAG[@]}" >"${PACK_OUT}" 2>&1; then
      sed 's/^/    /' "${PACK_OUT}"; rm -f "${PACK_OUT}"
      manual 6 truveon-accounts \
"Register the new accounts in the client's Truveon tenant.

The pack in completed_qs/${PARTNER}-${CLIENT}.truveon/ now lists them. From
Truveon's infra/scripts, re-run ONLY the configure step (step 2 of
commands.sh):

  ./87-apply-msp-manifest.sh --manifest <pack>/manifest.json

Do not re-run step 1 or step 3: the tenant exists and its key is stored.
Until this is done Truveon holds this client's evidence against accounts it
cannot name, and flags every submission as from an unregistered account."
    else
      sed 's/^/    /' "${PACK_OUT}"; rm -f "${PACK_OUT}"
      blocked 6 truveon-accounts \
"The Truveon pack could not be regenerated with the new accounts. See above."
    fi
  fi
fi

# ===========================================================================
# 7 — PagerDuty, pass 2
# ===========================================================================
# The accounts now exist, so the renderer can see tiers. This is the pass that
# creates the technical services and the routing keys. See the header.

if should_run 7 pagerduty-pass-2; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  re-render with accounts present, then apply and sync\n' \
      "${C_YELLOW}" "${C_RESET}"
    record 7 pagerduty-pass-2 "dry" ""
    log "    Pass 1 created only the client-conversation business service."
    log "    This pass is where the tier services, the technical services"
    log "    (tier x app plus infra) and the ROUTING KEYS come from."
  else
    attempt 7 pagerduty-pass-2 \
      bash "${REPO_ROOT}/scripts/render-pagerduty-tenants.sh"
    if [[ -z "${HALTED}" ]]; then
      log ""
      log "  Now apply, then sync the keys into Secrets Manager:"
      log "    scripts/pagerduty-apply.sh            # read the plan"
      log "    scripts/pagerduty-apply.sh --apply"
      log "    scripts/sync-pagerduty-tenants.sh"
      log ""
      log "  Until sync-pagerduty-tenants.sh has run, this tenant's alarms have"
      log "  no routing key and fire into the void."
    fi
  fi
fi

# ===========================================================================
# 8 — Monitoring and schedules
# ===========================================================================
# The published maps the routing and clock layers read. Each one has the same
# failure mode if it is not re-run after vesting: the new account has no entry,
# and the consumer reports the account as unknown rather than assuming a
# default. Visible, and wrong in the direction that gets noticed.
#
# The instrumentation itself is NOT here. It arrives through the baseline
# StackSets, which service-managed auto-deploy into the Members OU — vesting
# already refuses to create accounts into an OU those StackSets do not target.

if should_run 8 monitoring-and-schedules; then
  SYNCS=(
    "sync-account-tiers.sh|the router's account -> tier map, so a CRITICAL finding in dev does not page"
    "sync-notification-windows.sh|the incident clock's per-account notification windows"
    "sync-evidence-tenants.sh|the evidence pipeline's tenant list"
    "sync-tenant-facts.sh|tenant facts published for the alarm and digest layers"
    # Not a map, but the same failure shape: assigned per account, from the
    # Organizations account list, so a new account is covered only when this
    # runs again. It said "re-run after vesting" and onboarding never did --
    # Arc8's accounts came up (2026-10-01) with no permission set assigned to
    # anyone, and the security function could not open them.
    "deploy-security-access.sh|SecurityReadOnly assigned to the new accounts, so the security function can open them"
  )
  STEP7_FAILED=""
  for entry in "${SYNCS[@]}"; do
    IFS='|' read -r script why <<<"${entry}"
    info "  ${script} — ${why}"
    out="$(mktemp)"
    if bash "${REPO_ROOT}/scripts/${script}" "${DRY_FLAG[@]}" >"${out}" 2>&1; then
      sed 's/^/      /' "${out}"
      ok "  ${script}"
    else
      sed 's/^/      /' "${out}"
      warn "  ${script} FAILED"
      STEP7_FAILED="${STEP7_FAILED} ${script}"
    fi
    rm -f "${out}"
  done

  if [[ -n "${STEP7_FAILED}" ]]; then
    blocked 8 monitoring-and-schedules \
"These publishers failed:${STEP7_FAILED}

Each publishes a map a consumer reads at alarm time. A missing entry does not
break loudly — the consumer reports the account as unknown — so this must not
be left for later."
  else
    record 8 monitoring-and-schedules "$([[ "${DRY_RUN}" == "1" ]] && echo dry || echo done)" ""
    ok "step 8 (monitoring-and-schedules) complete"
  fi
fi

# ===========================================================================
# 9 — Developer access
# ===========================================================================
# Not numbered in design 13, which covers systems rather than people. It sits
# here because it needs the accounts (step 5) and nothing later needs it.

if should_run 9 developer-access; then
  if [[ -z "${ROSTER}" ]]; then
    record 9 developer-access "skipped" "no --roster supplied"
    warn "No roster supplied, so nobody has access to these accounts."
    warn "  Questionnaire section 3b is marked Dev, not Vest, so this is a"
    warn "  legitimate state — the accounts are vested and baselined and no"
    warn "  human can sign in to them. Supply --roster when 3b is answered:"
    warn "    cp vesting/roster.example.yaml completed_qs/${CLIENT}.roster.yaml"
    warn "    scripts/onboard-client.sh --questionnaire ${QUESTIONNAIRE} \\"
    warn "      --roster completed_qs/${CLIENT}.roster.yaml --from 9 --confirm-onboard"
  else
    attempt 9 developer-access \
      bash "${REPO_ROOT}/scripts/grant-developer-access.sh" \
        --roster "${ROSTER}" --questionnaire "${QUESTIONNAIRE}" "${DRY_FLAG[@]}"
  fi
fi

# ===========================================================================
# 10 — Cost categories
# ===========================================================================
# Until this runs, the tenant's spend appears as `unregistered`. Budgets are
# design 13 step 7's other half and do not exist yet.

if should_run 10 cost-categories; then
  attempt 10 cost-categories \
    bash "${REPO_ROOT}/scripts/sync-cost-categories.sh" "${DRY_FLAG[@]}"
  if [[ -z "${HALTED}" ]]; then
    warn "Budgets and anomaly detection are NOT created — design 13 step 7 asks"
    warn "  for them and nothing builds them. The Cost Category mapping is in"
    warn "  place, so spend is attributed; there is no threshold alert behind it."
    warn "  Questionnaire 13.2 asked the client for a number that nothing reads."
  fi
fi

# ===========================================================================
# 11 — Verification sweep
# ===========================================================================
# What makes provisioning a control rather than a script. Runs in the Audit
# account via the Lambda that owns it.

if should_run 11 verification-sweep; then
  manual 11 verification-sweep \
"The verification sweep cannot be invoked from here.

provisioning/src/verify.py implements design 13's checklist and, like the rest
of provisioning/src, has no template and no publish script — there is no
deployed function to call.

What CAN be run now, each covering part of the sweep:

  scripts/verify-security-coverage.sh        # detective controls per account
  scripts/test-guardrails.sh                 # guardrail harness
  scripts/test-alert-path.sh --alarm <name> --alarm-account <alias>
                                             # a synthetic alarm to PagerDuty
  scripts/pagerduty-drift.sh                 # console edits since the apply
  scripts/jira-check.sh                      # change reconciliation source

design 13 fails closed: none of these passing means the tenant is verified, and
go-live does not proceed on the ones that happen to be implemented. See
docs/verification-sweep.md."
fi

# ===========================================================================
# 12 — Go-live gate
# ===========================================================================
# Deliberately has no action and never will.

if should_run 12 go-live-gate; then
  manual 12 go-live-gate \
"Go-live is a human decision, owned by Jamie, and is not automated.

Three things start on the transition to live: SLA measurement, billing, and
the on-call rotation. None of them should start because a script reached its
last line.

The tenant stays in 'provisioning'. That is the correct state, and an
application still being migrated can sit in it indefinitely."
fi

# ===========================================================================
# Summary
# ===========================================================================

hr
log "Onboarding summary — ${PARTNER}/${CLIENT}/${APP}"
hr
printf '  %-4s %-28s %-9s %s\n' "STEP" "NAME" "STATUS" "NOTE"
BLOCKS=0
for r in "${RESULTS[@]}"; do
  IFS='|' read -r n name status note <<<"${r}"
  [[ "${status}" == "blocked" ]] && BLOCKS=$((BLOCKS + 1))
  # First line of the note, clipped. The full text was printed when it
  # happened; a summary that reflowed eight-line explanations would not be a
  # summary, and one that ran off the right edge of an 80-column terminal
  # would hide the STATUS column it exists to show.
  first="${note%%$'\n'*}"
  [[ ${#first} -gt 32 ]] && first="${first:0:29}..."
  printf '  %-4s %-28s %-9s %s\n' "${n}" "${name}" "${status}" "${first}"
done
hr

# The outstanding list, printed separately and last, because it is the part
# that must not be skimmed. A tenant with manual steps outstanding is not
# finished, however many rows above it say `done`.
if [[ ${#MANUAL[@]} -gt 0 ]]; then
  log ""
  log "OUTSTANDING — these cannot be automated and are not done:"
  for m in "${MANUAL[@]}"; do
    IFS='|' read -r n name reason <<<"${m}"
    log "  step ${n}  ${name}"
    log "      ${reason%%$'\n'*}"
  done
  hr
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  ok "Dry run complete — nothing was created."
  if [[ ${BLOCKS} -gt 0 ]]; then
    log ""
    log "${BLOCKS} step(s) would block. A real run stops at the first;"
    log "this run continued so that one rehearsal shows all of them."
  fi
  # Exits 0 even with blocks, deliberately. Steps 4, 11 and 12 can never
  # succeed from here, so a non-zero exit would be non-zero on every run ever
  # and would mean nothing. A rehearsal's exit code says the REHEARSAL
  # completed; the table says whether the tenant would be ready.
else
  if [[ -n "${HALTED}" ]]; then
    log ""
    warn "Onboarding HALTED at step ${HALTED}. Earlier steps were completed and"
    warn "  are idempotent — fix the cause and resume:"
    warn "    scripts/onboard-client.sh --questionnaire ${QUESTIONNAIRE} \\"
    warn "      ${ROSTER:+--roster ${ROSTER} }${TRUVEON_ROSTER:+--truveon-roster ${TRUVEON_ROSTER} }--from ${HALTED} --confirm-onboard"
    exit 1
  fi
  if [[ ${#MANUAL[@]} -gt 0 ]]; then
    ok "Automated steps complete for ${PARTNER}/${CLIENT}/${APP}"
    warn "${#MANUAL[@]} step(s) above are OUTSTANDING. This tenant is not onboarded."
  else
    ok "Onboarding run complete for ${PARTNER}/${CLIENT}/${APP}"
  fi
  log ""
  log "The tenant is in 'provisioning', NOT 'active'. It is not verified, it"
  log "has no SLA, it is not billed and it is not in the on-call rotation."
  log "Promoting it is the verification sweep's job and then Jamie's, in that"
  log "order."
fi
hr
