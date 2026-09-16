#!/usr/bin/env bash
#
# Create or update Organizations policies from policies/scp/*.json and
# policies/rcp/*.json, and optionally attach them to OUs.
#
#   scripts/deploy-policies.sh --dry-run
#   scripts/deploy-policies.sh                      # create/update only
#   scripts/deploy-policies.sh --attach sandbox     # ...and attach
#   scripts/deploy-policies.sh --attach members --confirm-production
#   scripts/deploy-policies.sh --drift              # compare live vs files
#
# ---------------------------------------------------------------------------
# Create/update and attach are separate on purpose
# ---------------------------------------------------------------------------
# Updating a policy that is already attached takes effect immediately across
# every account beneath the attachment point. Attaching is the moment a policy
# starts denying things. Keeping them as separate flags means a content change
# can be staged and reviewed before it becomes live anywhere, and it makes the
# dangerous step explicit in shell history.
#
# Attaching to 'members' additionally requires --confirm-production: that OU is
# where tenant workloads live, and an over-broad deny there is an outage.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ATTACH_TARGET=""; DO_DRIFT=0

usage() {
  cat <<'USAGE'
Usage: deploy-policies.sh [--attach <sandbox|members|both>] [--drift] [--dry-run]
                          [--confirm-production]

  --attach              Attach after create/update. 'members' and 'both'
                        require --confirm-production.
  --drift               Report where live policy content differs from the
                        files in policies/. Makes no changes.
  --dry-run             Print intended actions; change nothing.
  --confirm-production  Required to attach anything to the Members OU.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --attach)             ATTACH_TARGET="$2"; shift 2 ;;
    --drift)              DO_DRIFT=1; shift ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --confirm-production) CONFIRM_PRODUCTION=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

# policy_name_for <file> -> Platform<CamelName>
policy_name_for() {
  local base; base="$(basename "$1" .json)"
  base="${base#[0-9][0-9]-}"                       # strip the ordering prefix
  printf 'Platform%s' "$(printf '%s' "${base}" | awk -F- '{for(i=1;i<=NF;i++) printf toupper(substr($i,1,1)) substr($i,2)}')"
}

find_policy_id() {
  aws organizations list-policies --filter SERVICE_CONTROL_POLICY \
    --query "Policies[?Name=='$1'].Id | [0]" --output text 2>/dev/null | no_cr | sed 's/^None$//'
}

mapfile -t FILES < <(find "${REPO_ROOT}/policies/scp" -name '*.json' 2>/dev/null | sort)
[[ ${#FILES[@]} -gt 0 ]] || die "No policy documents found."

# --- drift mode ------------------------------------------------------------
if [[ ${DO_DRIFT} -eq 1 ]]; then
  hr; log "Policy drift"; hr
  DRIFTED=0
  for f in "${FILES[@]}"; do
    name="$(policy_name_for "${f}")"
    pid="$(find_policy_id "${name}")"
    if [[ -z "${pid}" ]]; then
      warn "${name}: not present in the Organization"; DRIFTED=1; continue
    fi
    live="$(aws organizations describe-policy --policy-id "${pid}" \
      --query 'Policy.Content' --output text | no_cr)"
    # Compare canonicalised JSON: whitespace is not drift.
    if diff -q <(printf '%s' "${live}" | python -c 'import json,sys;print(json.dumps(json.load(sys.stdin),sort_keys=True))') \
               <(python -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))' "${f}") >/dev/null 2>&1; then
      ok "${name} in sync"
    else
      printf '%sDRIFT%s %s — live content differs from %s\n' "${C_RED}" "${C_RESET}" "${name}" "$(basename "${f}")"
      DRIFTED=1
    fi
  done
  hr
  [[ ${DRIFTED} -eq 0 ]] || die "Policy drift detected."
  ok "No drift."
  exit 0
fi

# --- validate before touching anything -------------------------------------
info "Validating policy documents first"
bash "${REPO_ROOT}/scripts/validate-policies.sh" >/dev/null \
  || die "Validation failed — refusing to deploy. Run scripts/validate-policies.sh"
ok "documents valid"
hr

# --- create or update ------------------------------------------------------
declare -a POLICY_IDS=() POLICY_NAMES=()
for f in "${FILES[@]}"; do
  name="$(policy_name_for "${f}")"
  pid="$(find_policy_id "${name}")"
  content="$(cat "${f}")"

  if [[ -n "${pid}" ]]; then
    # Not via run(): its dry-run mode echoes the full command, which here would
    # scroll 4KB of policy JSON past per document. The content is on disk and
    # reviewable with git diff; the useful line is which policy changed.
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  update policy %s (%s) from %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${name}" "${pid}" "$(basename "${f}")"
    else
      aws organizations update-policy --policy-id "${pid}" \
        --content "${content}" --output text --query 'Policy.PolicySummary.Id' >/dev/null
      ok "Updated ${name} (${pid})"
    fi
  else
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  create policy %s from %s\n' "${C_YELLOW}" "${C_RESET}" "${name}" "$(basename "${f}")"
      pid="p-dryrun"
    else
      pid="$(aws organizations create-policy \
        --name "${name}" \
        --description "Platform control-protection SCP, managed from $(basename "${f}")" \
        --type SERVICE_CONTROL_POLICY \
        --content "${content}" \
        --query 'Policy.PolicySummary.Id' --output text | no_cr)"
      ok "Created ${name} (${pid})"
    fi
  fi
  POLICY_IDS+=("${pid}"); POLICY_NAMES+=("${name}")
done
hr

# --- attach ----------------------------------------------------------------
[[ -n "${ATTACH_TARGET}" ]] || { ok "Policies created/updated. Nothing attached."; \
  log ""; log "To attach: scripts/deploy-policies.sh --attach sandbox"; exit 0; }

declare -a TARGETS=() TARGET_LABELS=()
case "${ATTACH_TARGET}" in
  sandbox) TARGETS+=("$(get_param /org/ou/sandbox)"); TARGET_LABELS+=("Sandbox") ;;
  members) confirm_production; TARGETS+=("$(get_param /org/ou/members)"); TARGET_LABELS+=("Members") ;;
  both)    confirm_production
           TARGETS+=("$(get_param /org/ou/sandbox)" "$(get_param /org/ou/members)")
           TARGET_LABELS+=("Sandbox" "Members") ;;
  *) die "Unknown --attach target '${ATTACH_TARGET}'. Expected sandbox, members or both." ;;
esac

for i in "${!TARGETS[@]}"; do
  tgt="${TARGETS[$i]}"; label="${TARGET_LABELS[$i]}"
  [[ -n "${tgt}" && "${tgt}" != "None" ]] || die "Could not resolve the ${label} OU id."
  info "Attaching to ${label} OU (${tgt})"

  attached="$(aws organizations list-policies-for-target --target-id "${tgt}" \
    --filter SERVICE_CONTROL_POLICY --query 'Policies[].Name' --output text 2>/dev/null | no_cr | tr '\t\n' '  ')"

  for j in "${!POLICY_IDS[@]}"; do
    if [[ " ${attached} " == *" ${POLICY_NAMES[$j]} "* ]]; then
      skip "${POLICY_NAMES[$j]} already attached to ${label}"
    else
      run "attach ${POLICY_NAMES[$j]} -> ${label}" \
        aws organizations attach-policy --policy-id "${POLICY_IDS[$j]}" --target-id "${tgt}"
    fi
  done
done
hr

warn "FullAWSAccess remains attached. These are Deny policies and rely on it:"
warn "  removing FullAWSAccess without an explicit Allow denies everything."
hr
ok "Done. Now prove it: scripts/test-guardrails.sh --account <member account id>"
