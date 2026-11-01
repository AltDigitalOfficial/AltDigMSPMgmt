#!/usr/bin/env bash
#
# Has anybody changed PagerDuty outside this repository?
#
#   scripts/pagerduty-drift.sh            # report, exit 0 even on drift
#   scripts/pagerduty-drift.sh --strict   # exit 2 on drift, for a pipeline
#
# prompt 12.2: "Treat it exactly like the AWS baseline -- versioned, staged,
# drift-detected. A manual console change to a provisioned service is a
# finding; build that detection."
#
# ---------------------------------------------------------------------------
# The detector is terraform plan, and that is the whole point
# ---------------------------------------------------------------------------
# The obvious build is a Lambda that reads the PagerDuty API and compares what
# it finds against what the platform expects. It would be a second
# implementation of "what should exist", living beside the Terraform that
# already says so -- and the two would disagree the first time somebody
# changed one. Every comparison it made would be a chance to be wrong in a
# direction nobody could see.
#
# `terraform plan -detailed-exitcode` is the comparison, already written,
# already correct for every attribute of every resource type, maintained by
# the provider rather than by us. It refreshes state from the live account and
# reports what would have to change to make reality match the configuration.
# Anything it wants to change is, by definition, drift.
#
#   exit 0   no changes. Reality matches the configuration.
#   exit 1   the plan itself failed. NOT "no drift" -- see below.
#   exit 2   changes required. Something was edited outside this repository,
#            or the repository was edited and not applied.
#
# instrumentation/src/drift.py reaches the same conclusion for alarms by
# re-deriving expected state from 06a rather than storing a copy: "a separate
# record of expected state would be a second source of truth, and drift would
# correct alarms toward whichever copy was edited last."
#
# ---------------------------------------------------------------------------
# It reports; it does not correct
# ---------------------------------------------------------------------------
# No `--apply`, deliberately, and not for symmetry with the other scripts.
#
# Applying a plan to "fix" drift is applying a plan nobody read. If somebody
# added an escalation policy target by hand during an incident -- the single
# most likely console edit on this account -- an automatic correction removes
# it, and removes it at exactly the moment it is load-bearing. CLAUDE.md:
# operational automation is additive or reversible, and this would be neither.
#
# So drift is a FINDING. A human reads the plan and decides whether the
# console was wrong or the repository was.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

STRICT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --strict) STRICT=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--strict]\n' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

TF="$(find_tool terraform)" || die "terraform not found. Run scripts/setup-tooling.sh."
TFDIR="${REPO_ROOT}/pagerduty"

# The token, resolved the way pagerduty-apply.sh resolves it, but the
# READ-ONLY key (15.8). Plan only reads, so a check that runs often holds a key
# that cannot retarget pages. No fallback to the full key: a missing read-only
# key is something to fix, not something to work around with more access.
# A drift check that cannot authenticate must not report "no drift".
if [[ -z "${PAGERDUTY_TOKEN:-}" ]]; then
  AUDIT="$(get_param /org/account/altdig-security-audit)"
  if [[ -n "${AUDIT}" && "${AUDIT}" != "None" ]]; then
    FETCHED="$(read_secret "${AUDIT}" platform/pagerduty/api-token-readonly)"
    if [[ -n "${FETCHED}" && "${FETCHED}" != "None" ]]; then
      PAGERDUTY_TOKEN="$(printf '%s' "${FETCHED}" \
        | python -c 'import json,sys; print(json.load(sys.stdin)["api_token"])')"
      export PAGERDUTY_TOKEN
    fi
  fi
fi
[[ -n "${PAGERDUTY_TOKEN:-}" ]] \
  || die "No read-only PagerDuty API token available. Run scripts/set-pagerduty-token.sh --readonly.
      Refusing to report on drift without being able to look."

hr
log "PagerDuty drift check"
log "  directory : pagerduty/"
log "  mode      : $([[ ${STRICT} -eq 1 ]] && echo STRICT || echo REPORT)"
hr

(cd "${TFDIR}" && "${TF}" init -input=false >/dev/null) \
  || die "terraform init failed. No conclusion about drift."

# Redirected to a file rather than piped. CLAUDE.md trap 3: a pipeline returns
# the LAST command's status, so piping this to tee would discard the exit code
# that carries the entire result.
PLANOUT="$(mktemp)"
trap 'rm -f "${PLANOUT}"' EXIT

set +e
(cd "${TFDIR}" && "${TF}" plan -input=false -detailed-exitcode -no-color) \
  > "${PLANOUT}" 2>&1
RC=$?
set -e

case "${RC}" in
  0)
    ok "no drift. PagerDuty matches the configuration in this repository."
    # What was EXAMINED, not only the verdict. "No drift" over zero resources
    # and "no drift" over forty are the same sentence and different facts.
    RESOURCES="$(cd "${TFDIR}" && "${TF}" state list 2>/dev/null | wc -l | tr -d ' ')"
    log "  resources compared : ${RESOURCES}"
    ;;
  2)
    warn "DRIFT. PagerDuty does not match the configuration."
    printf '\n'
    # The plan itself, because a summary of a diff is a diff somebody has to
    # go and read anyway.
    sed -n '/Terraform will perform the following actions/,$p' "${PLANOUT}" \
      | head -120
    printf '\n'
    hr
    warn "This is a FINDING, not a task to auto-correct."
    log "  Decide which side is wrong:"
    log "    console edited  -> re-apply: scripts/pagerduty-apply.sh --apply"
    log "    repo out of date-> bring the change into pagerduty/ and commit it"
    log ""
    log "  An escalation policy edited by hand during an incident is the most"
    log "  likely cause, and re-applying would remove it at exactly the moment"
    log "  somebody is relying on it. Read the plan before choosing."
    hr
    [[ ${STRICT} -eq 1 ]] && exit 2
    ;;
  *)
    # NOT "no drift". A failed plan means the question was not answered, and
    # reporting a clean result here is the B-086 shape -- a check that could
    # not look, reporting that it saw nothing wrong.
    printf '\n'
    tail -30 "${PLANOUT}"
    printf '\n'
    die "terraform plan FAILED (exit ${RC}). This is not a clean result --
      nothing is known about drift. Fix the plan and re-run."
    ;;
esac
