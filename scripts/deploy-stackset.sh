#!/usr/bin/env bash
#
# Deploy a baseline template as a service-managed StackSet targeting OUs.
#
#   scripts/deploy-stackset.sh --name platform-config \
#     --template baseline/10-config-recorder.yaml \
#     --ou sandbox --regions us-east-2 \
#     LogArchiveBucketName=... [--dry-run]
#
# ---------------------------------------------------------------------------
# Operated from Infra Tooling, as delegated administrator
# ---------------------------------------------------------------------------
# Every StackSet call below runs with Infra Tooling credentials and
# --call-as DELEGATED_ADMIN. The management account is used only to read the
# /platform/ parameters this script needs, before it switches.
#
# A service-managed StackSet's record lives in the management account however
# it was created, and the registered delegated administrator can operate every
# one of them, including the ten created from the management account before
# delegation existed. Proved 2026-09-30: a no-op update of
# platform-canary-artifacts from Tooling succeeded, and both instances stayed
# CURRENT. D-012 had assumed a StackSet could only be operated from the
# account that created it, and F-2 planned a retire-and-replace migration on
# that basis. Neither was needed. What D-012 actually objected to, the
# management account running the pipelines, is closed by where this script
# makes its calls, not by moving the StackSets.
#
# Tooling is reached through OrganizationAccountAccessRole for now, the same
# break-glass path every operator script uses. 15.3 replaces it with
# PlatformDeployer from the pipeline.
#
# ---------------------------------------------------------------------------
# Service-managed, and why that matters
# ---------------------------------------------------------------------------
# A SERVICE-MANAGED StackSet targets organizational units rather than a list of
# account ids, and AWS creates the execution role in each target account. Two
# consequences the design depends on:
#
#   * Auto-deployment. An account moved into a targeted OU receives the stack
#     without anyone running anything. That is what makes the baseline a
#     property of OU placement rather than of remembering to deploy — design
#     doc 02: "Places it in the correct OU (which triggers SCP and StackSet
#     inheritance)".
#
#   * No execution roles to create or protect. Self-managed StackSets need
#     AWSCloudFormationStackSetExecutionRole in every target account, which is
#     another Platform-adjacent role to provision and defend.
#
# The execution principal is 'stacksets-exec-<hash>', which every SCP exempts
# by that pattern (B-135).
#
# ---------------------------------------------------------------------------
# Staged rollout
# ---------------------------------------------------------------------------
# Design doc 08 and prompt 2.4 want Sandbox canary, then non-production
# Members, then production. This script deploys to ONE OU at a time by design:
# --ou members requires --confirm-production, and there is no flag that targets
# everything at once. The wave discipline is enforced by having to type it
# again.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

NAME=""; TEMPLATE=""; OU=""; REGIONS="${PLATFORM_HOME_REGION}"; PARAMS=()
MAX_CONCURRENT=1; FAILURE_TOLERANCE=0; ADD_OU=0; REGIONS_GIVEN=0

usage() {
  cat <<'USAGE'
Usage: deploy-stackset.sh --name <stackset> --template <path> --ou <target>
                          [--regions r1,r2] [Key=Value ...]
                          [--max-concurrent N] [--dry-run] [--confirm-production]
       deploy-stackset.sh --name <stackset> --ou <target> --add-ou
                          [--regions r1,r2] [--dry-run] [--confirm-production]

  --ou              sandbox | members | security | infra
  --regions         Comma-separated. Defaults to the home region; with
                    --add-ou, defaults to the regions the existing instances use.
  --max-concurrent  Accounts updated in parallel. Default 1 — a bad baseline
                    reaching one account is recoverable; reaching forty at once
                    is an incident.
  --add-ou          Add an OU to an EXISTING stack set and nothing else. No
                    template, no parameters, no update-stack-set: instances
                    for the new OU are created from the stack set as it stands.

Runs every StackSet call from Infra Tooling as delegated administrator. Start
with management-account credentials; the script switches.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)               NAME="$2"; shift 2 ;;
    --template)           TEMPLATE="$2"; shift 2 ;;
    --ou)                 OU="$2"; shift 2 ;;
    --regions)            REGIONS="$2"; REGIONS_GIVEN=1; shift 2 ;;
    --add-ou)             ADD_OU=1; shift ;;
    --max-concurrent)     MAX_CONCURRENT="$2"; shift 2 ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --confirm-production) CONFIRM_PRODUCTION=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *=*)                  PARAMS+=("$1"); shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${NAME}" ]]     || { usage; die "--name is required."; }
if [[ ${ADD_OU} -eq 1 ]]; then
  [[ -z "${TEMPLATE}" && ${#PARAMS[@]} -eq 0 ]] \
    || die "--add-ou takes no --template and no parameters. It deploys the stack set as it stands."
else
  [[ -n "${TEMPLATE}" ]] || { usage; die "--template is required."; }
fi
[[ -n "${OU}" ]]       || { usage; die "--ou is required."; }

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# --- everything that needs the management account, first -------------------
# The /platform/ parameters live in the management account's SSM. Read them all
# before switching, because nothing after the switch can.
case "${OU}" in
  sandbox)  TARGET_OU="$(get_param /org/ou/sandbox)" ;;
  members)  confirm_production; TARGET_OU="$(get_param /org/ou/members)" ;;
  security) TARGET_OU="$(get_param /org/ou/security)" ;;
  infra)    TARGET_OU="$(get_param /org/ou/infrastructure)" ;;
  *) die "Unknown --ou '${OU}'. Expected sandbox, members, security or infra." ;;
esac
[[ -n "${TARGET_OU}" && "${TARGET_OU}" != "None" ]] || die "Could not resolve the ${OU} OU."

TOOLING_ACCOUNT="$(get_param /org/account/altdig-infra-tooling)"
[[ "${TOOLING_ACCOUNT}" =~ ^[0-9]{12}$ ]] \
  || die "Infra Tooling is not registered in SSM (/platform/org/account/altdig-infra-tooling)."

# --- switch to Infra Tooling ------------------------------------------------
read -r T_KEY T_SECRET T_TOKEN <<<"$(aws sts assume-role \
  --role-arn "arn:aws:iam::${TOOLING_ACCOUNT}:role/OrganizationAccountAccessRole" \
  --role-session-name platform-stackset-deploy \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text 2>/dev/null | no_cr)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume OrganizationAccountAccessRole in Infra Tooling."
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" AWS_SESSION_TOKEN="${T_TOKEN}"

# Checked, not assumed. A run that silently stayed in the management account
# would still work and would re-open D-012 without anyone noticing.
NOW_IN="$(aws sts get-caller-identity --query Account --output text | no_cr)"
[[ "${NOW_IN}" == "${TOOLING_ACCOUNT}" ]] \
  || die "Expected to be operating from Infra Tooling (${TOOLING_ACCOUNT}), but credentials resolve to ${NOW_IN}."

# Every StackSet call goes through this, so none can forget the flag.
cfn_ss() { aws cloudformation "$@" --call-as DELEGATED_ADMIN; }

# --- --add-ou: widen an existing stack set, and change nothing else --------
#
# The normal path below always runs update-stack-set first. That re-applies
# the REPO template to every existing instance, and any parameter not passed
# can fall back to its template default. Widening platform-network to Members
# that way would have redeployed Sandbox's network from whatever the template
# says today, possibly with a default where a real bucket name had been. That
# was found before it happened, 2026-10-01.
#
# Widening needs none of it. create-stack-instances uses the stack set's
# current template and parameters, and an OU with no accounts yet just becomes
# a target, so its accounts receive the stack when they are created.
if [[ ${ADD_OU} -eq 1 ]]; then
  cfn_ss describe-stack-set --stack-set-name "${NAME}" --query 'StackSet.StackSetId' \
    --output text >/dev/null 2>&1 || die "--add-ou: stack set ${NAME} does not exist. Create it without --add-ou."
  TARGETED="$(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
    --query 'StackSet.OrganizationalUnitIds' --output text | no_cr | tr '\t\n' '  ')"
  if [[ " ${TARGETED} " == *" ${TARGET_OU} "* ]]; then
    skip "${NAME} already targets ${OU} (${TARGET_OU}). Nothing to add."
    exit 0
  fi
  # Same regions as the instances that exist, unless told otherwise: a stack
  # set deployed to three regions in Sandbox should reach Members in all three.
  if [[ ${REGIONS_GIVEN} -eq 0 ]]; then
    REGIONS="$(cfn_ss list-stack-instances --stack-set-name "${NAME}" \
      --query 'Summaries[].Region' --output text | no_cr | tr '\t' '\n' | sort -u | paste -sd, -)"
    [[ -n "${REGIONS}" ]] || REGIONS="${PLATFORM_HOME_REGION}"
  fi
  hr
  log "StackSet widen (--add-ou)"
  log "  stackset : ${NAME}  (template and parameters untouched)"
  log "  add OU   : ${OU} (${TARGET_OU})"
  log "  regions  : ${REGIONS}"
  log "  operator : Infra Tooling ${TOOLING_ACCOUNT}, as delegated administrator"
  hr
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create stack instances for %s in %s; no update-stack-set\n' \
      "${C_YELLOW}" "${C_RESET}" "${TARGET_OU}" "${REGIONS}"
    ok "Dry run complete."; exit 0
  fi
  OP_ID="$(cfn_ss create-stack-instances --stack-set-name "${NAME}" \
    --deployment-targets "OrganizationalUnitIds=${TARGET_OU}" \
    --regions $(printf '%s' "${REGIONS}" | tr ',' ' ') \
    --operation-preferences "MaxConcurrentCount=${MAX_CONCURRENT},FailureToleranceCount=${FAILURE_TOLERANCE},RegionConcurrencyType=SEQUENTIAL" \
    --query 'OperationId' --output text | no_cr)"
  log "  operation: ${OP_ID}"
  STATUS=""
  for _ in $(seq 1 60); do
    STATUS="$(cfn_ss describe-stack-set-operation --stack-set-name "${NAME}" \
      --operation-id "${OP_ID}" --query 'StackSetOperation.Status' --output text | no_cr)"
    case "${STATUS}" in
      SUCCEEDED) ok "operation SUCCEEDED"; break ;;
      FAILED|STOPPED) break ;;
      *) sleep 15 ;;
    esac
  done
  [[ "${STATUS}" == "SUCCEEDED" ]] || die "Widening ${NAME} to ${OU} ended ${STATUS:-unknown}."
  NOW_TARGETED="$(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
    --query 'StackSet.OrganizationalUnitIds' --output text | no_cr | tr '\t\n' '  ')"
  [[ " ${NOW_TARGETED} " == *" ${TARGET_OU} "* ]] \
    || die "Operation SUCCEEDED but ${TARGET_OU} is not in the stack set's targets."
  ok "${NAME} now targets ${OU}"
  exit 0
fi

hr
log "StackSet deploy"
log "  stackset : ${NAME}"
log "  template : ${TEMPLATE}"
log "  target   : ${OU} OU (${TARGET_OU})"
log "  regions  : ${REGIONS}"
log "  parallel : ${MAX_CONCURRENT} account(s), tolerance ${FAILURE_TOLERANCE}"
log "  operator : Infra Tooling ${TOOLING_ACCOUNT}, as delegated administrator"
hr

cfn_validate "${REPO_ROOT}/${TEMPLATE}"

# Parameters go as JSON, not as the ParameterKey=X,ParameterValue=Y shorthand.
#
# The shorthand splits on commas, so any parameter value CONTAINING a comma is
# parsed as further key/value pairs. BlockedDomains is exactly that:
#   BlockedDomains=*.bit,*.onion,*.top
# arrives at the CLI as a list and fails with "Invalid type for parameter
# Parameters[0].ParameterValue ... valid types: <class 'str'>", which names the
# type and not the comma.
#
# JSON has no such ambiguity, and python does the escaping so a value carrying
# a quote or a space is safe too.
PARAM_JSON=""
if [[ ${#PARAMS[@]} -gt 0 ]]; then
  PARAM_JSON="$(printf '%s\n' "${PARAMS[@]}" | "$(command -v python || command -v python3)" -c '
import json,sys
out=[]
for line in sys.stdin.read().splitlines():
    if not line:
        continue
    k, _, v = line.partition("=")
    out.append({"ParameterKey": k, "ParameterValue": v})
print(json.dumps(out))
')"
fi

EXISTS="$(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
  --query 'StackSet.StackSetId' --output text 2>/dev/null | no_cr || true)"

# On UPDATE, keep every parameter the command line does not name.
#
# update-stack-set takes the parameter list as given. A template parameter the
# stack set already has but the command line omits does not keep its value: it
# falls back to the template default, or the update fails if there is none. So
# a routine redeploy of platform-network, passed only the parameter being
# changed, could reset the rest. Now each omitted parameter the stack set
# already has goes as UsePreviousValue=true, and the run says which ones it
# kept. Parameters the template no longer declares are left out, because
# CloudFormation rejects unknown keys. That is how a removed parameter
# (canary-smoke's ArtifactsBucketName, B-062) is dropped.
if [[ -n "${EXISTS}" ]]; then
  EXISTING_KEYS="$(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
    --query 'StackSet.Parameters[].ParameterKey' --output text | no_cr)"
  PARAM_JSON="$(GIVEN="${PARAM_JSON:-[]}" EXISTING="${EXISTING_KEYS}" \
    "$(command -v python || command -v python3)" - "$(win_path "${REPO_ROOT}/${TEMPLATE}")" <<'PYEOF'
import json, os, sys
import cfnlint.decode
tpl, _ = cfnlint.decode.decode(sys.argv[1])
declared = set((tpl or {}).get("Parameters", {}))
given = json.loads(os.environ["GIVEN"] or "[]")
named = {p["ParameterKey"] for p in given}
existing = set(os.environ["EXISTING"].split())
kept = sorted((declared & existing) - named)
dropped = sorted(existing - declared)
print(json.dumps(given + [{"ParameterKey": k, "UsePreviousValue": True} for k in kept]))
print("kept " + (", ".join(kept) or "none") + "; no longer in the template: "
      + (", ".join(dropped) or "none"), file=sys.stderr)
PYEOF
  )" || die "Could not build the parameter list for ${NAME}."
  [[ "${PARAM_JSON}" == "[]" ]] && PARAM_JSON=""
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  %s stack set %s (from Infra Tooling, DELEGATED_ADMIN)\n' "${C_YELLOW}" "${C_RESET}" \
    "$([[ -n "${EXISTS}" ]] && echo update || echo create)" "${NAME}"
  printf '%s DRY%s  create stack instances in OU %s across %s\n' \
    "${C_YELLOW}" "${C_RESET}" "${TARGET_OU}" "${REGIONS}"
  hr; ok "Dry run complete."; exit 0
fi

# The stack set's tags, as given to create/update-stack-set. CloudFormation
# propagates them to every taggable resource in every instance, which is how
# CLAUDE.md's rule -- everything the platform builds carries platform-layer:
# infrastructure -- holds without each template carrying it (B-157 phase B).
#
# MERGED with what the set already has, because --tags REPLACES the whole set:
# passing only platform-layer would strip platform-managed from
# platform-developer-boundary on its next deploy.
STACKSET_TAGS=(Key=platform-layer,Value=infrastructure)
if [[ -n "${EXISTS}" ]]; then
  while read -r k v; do
    [[ -n "${k}" && "${k}" != "platform-layer" ]] && STACKSET_TAGS+=("Key=${k},Value=${v}")
  done < <(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
             --query 'StackSet.Tags[].[Key,Value]' --output text | no_cr)
fi

if [[ -z "${EXISTS}" ]]; then
  info "Creating stack set ${NAME}"
  cfn_ss create-stack-set \
    --stack-set-name "${NAME}" \
    --template-body "file://$(win_path "${REPO_ROOT}/${TEMPLATE}")" \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
    --capabilities CAPABILITY_NAMED_IAM \
    --description "Platform baseline from ${TEMPLATE}" \
    --tags "${STACKSET_TAGS[@]}" \
    ${PARAM_JSON:+--parameters "${PARAM_JSON}"} \
    --query 'StackSetId' --output text | no_cr
  ok "stack set created"
else
  info "Updating stack set ${NAME}"
  cfn_ss update-stack-set \
    --stack-set-name "${NAME}" \
    --template-body "file://$(win_path "${REPO_ROOT}/${TEMPLATE}")" \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
    --capabilities CAPABILITY_NAMED_IAM \
    ${PARAM_JSON:+--parameters "${PARAM_JSON}"} \
    --tags "${STACKSET_TAGS[@]}" \
    --operation-preferences "MaxConcurrentCount=${MAX_CONCURRENT},FailureToleranceCount=${FAILURE_TOLERANCE},RegionConcurrencyType=SEQUENTIAL" \
    --query 'OperationId' --output text | no_cr
  ok "stack set updated"
fi

# Instances are separate from the set. Creating the set defines the template;
# creating instances is what actually deploys anything.
#
# But update-stack-set ALSO redeploys to every existing instance. Calling
# create-stack-instances straight afterwards collides with that operation —
# OperationInProgressException — and the collision looks like a failure when
# the update is in fact doing the work. So skip instance creation only when
# THIS OU is already a target.
#
# The test used to be "does this stack set have any instances at all", which
# broke the one thing this script exists to do. A stack set deployed to Sandbox
# has instances, so every later `--ou members` printed "1 instance(s) already
# exist", reported SUCCEEDED, and added nothing. The staged rollout — Sandbox,
# then Members — could never reach its second stage, and the output claimed it
# had.
#
# StackSet.OrganizationalUnitIds is the authoritative target list.
# list-stack-instances is not: it shows OUs that HAVE ACCOUNTS, so a correctly
# targeted but currently empty OU is indistinguishable from one never targeted.
# tr, because --output text separates a list with TABS. The containment test
# below pads with spaces, so without this it matches only the FIRST OU in the
# list — a stack set already targeting sandbox and members would report members
# as untargeted and try to create instances again, colliding with the update
# that is still running.
TARGETED_OUS="$(cfn_ss describe-stack-set --stack-set-name "${NAME}" \
  --query 'StackSet.OrganizationalUnitIds' --output text 2>/dev/null \
  | no_cr | tr '\t\n' '  ' || true)"

if [[ " ${TARGETED_OUS} " == *" ${TARGET_OU} "* && -n "${EXISTS}" ]]; then
  skip "${TARGET_OU} is already a target; the stack set update redeploys to it"
  info "Waiting for the update to complete"
  for _ in $(seq 1 80); do
    STATUS="$(cfn_ss list-stack-set-operations --stack-set-name "${NAME}" \
      --query 'Summaries[0].Status' --output text 2>/dev/null | no_cr)"
    case "${STATUS}" in
      SUCCEEDED)      ok "operation SUCCEEDED"; break ;;
      FAILED|STOPPED) warn "operation ${STATUS}"; break ;;
      *)              printf '  %s ...\n' "${STATUS}"; sleep 15 ;;
    esac
  done
  hr
  log "Stack instances:"
  cfn_ss list-stack-instances --stack-set-name "${NAME}" \
    --query 'Summaries[].[Account,Region,Status,StatusReason]' --output text 2>/dev/null \
    | no_cr | cut -c1-140 | sed 's/^/  /'
  hr
  exit 0
fi

info "Creating stack instances in ${OU}"
OP_ID="$(cfn_ss create-stack-instances \
  --stack-set-name "${NAME}" \
  --deployment-targets "OrganizationalUnitIds=${TARGET_OU}" \
  --regions "$(printf '%s' "${REGIONS}" | tr ',' ' ')" \
  --operation-preferences "MaxConcurrentCount=${MAX_CONCURRENT},FailureToleranceCount=${FAILURE_TOLERANCE},RegionConcurrencyType=SEQUENTIAL" \
  --query 'OperationId' --output text 2>&1 | no_cr)" || true

if printf '%s' "${OP_ID}" | grep -qi 'error'; then
  printf '%s' "${OP_ID}" | sed 's/^/  /'
  warn "If this reports instances already exist, the set is already deployed there."
else
  log "  operation: ${OP_ID}"
  info "Waiting for the operation to complete"
  for _ in $(seq 1 60); do
    STATUS="$(cfn_ss describe-stack-set-operation \
      --stack-set-name "${NAME}" --operation-id "${OP_ID}" \
      --query 'StackSetOperation.Status' --output text 2>/dev/null | no_cr)"
    case "${STATUS}" in
      SUCCEEDED) ok "operation SUCCEEDED"; break ;;
      FAILED|STOPPED) warn "operation ${STATUS}"; break ;;
      *) printf '  %s ...\n' "${STATUS}"; sleep 15 ;;
    esac
  done
fi

hr
log "Stack instances:"
cfn_ss list-stack-instances --stack-set-name "${NAME}" \
  --query 'Summaries[].[Account,Region,Status,StatusReason]' --output text 2>/dev/null \
  | no_cr | cut -c1-140 | sed 's/^/  /'
hr
