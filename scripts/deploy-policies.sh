#!/usr/bin/env bash
#
# Create or update Organizations policies from policies/scp/*.json and
# policies/rcp/*.json, and optionally attach them to OUs.
#
#   scripts/deploy-policies.sh --dry-run
#   scripts/deploy-policies.sh                      # create/update only
#   scripts/deploy-policies.sh --attach             # ...and attach per manifest
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
# Where each policy goes is declared in policies/scp/attachments.tsv, not on the
# command line. Any policy targeting 'members' or 'root' requires
# --confirm-production: those reach tenant accounts, and an over-broad deny
# there is an outage.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

DO_ATTACH=0; DO_DRIFT=0

usage() {
  cat <<'USAGE'
Usage: deploy-policies.sh [--attach] [--drift] [--dry-run] [--confirm-production]

  --attach              Attach each policy to the targets declared in
                        policies/scp/attachments.tsv. Targets reaching tenant
                        accounts (members, root) require --confirm-production.
  --drift               Report where live policy content differs from the
                        files in policies/. Makes no changes.
  --dry-run             Print intended actions; change nothing.
  --confirm-production  Required to attach anything at root or to Members.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --attach)             DO_ATTACH=1; shift ;;
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
    # Compare canonicalised JSON: whitespace and key order are not drift.
    #
    # Note win_path on the file argument. python.exe is a Windows binary and
    # cannot open '/c/AltDigital/...'; with MSYS_NO_PATHCONV set, nothing
    # converts it on our behalf. The same trap as the CloudFormation
    # --template-file argument, in a different disguise.
    if diff -q <(printf '%s' "${live}" | python -c 'import json,sys;print(json.dumps(json.load(sys.stdin),sort_keys=True))') \
               <(python -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))' "$(win_path "${f}")") >/dev/null 2>&1; then
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
if [[ ${DO_ATTACH} -ne 1 ]]; then
  ok "Policies created/updated. Nothing attached."
  log ""
  log "To attach per policies/scp/attachments.tsv:"
  log "  scripts/deploy-policies.sh --attach --confirm-production"
  exit 0
fi

MANIFEST="${REPO_ROOT}/policies/scp/attachments.tsv"
[[ -f "${MANIFEST}" ]] || die "Attachment manifest not found: ${MANIFEST}"

# resolve_target <name> -> organization target id
resolve_target() {
  case "$1" in
    root)    get_param /org/root-id ;;
    sandbox) get_param /org/ou/sandbox ;;
    members) get_param /org/ou/members ;;
    security) get_param /org/ou/security ;;
    infra)   get_param /org/ou/infrastructure ;;
    *) die "Unknown attachment target '$1' in $(basename "${MANIFEST}").
      Expected root, sandbox, members, security or infra." ;;
  esac
}

# Any target that reaches tenant accounts needs explicit confirmation. Checked
# once up front rather than mid-loop, so the run either proceeds fully or not
# at all — a half-applied attachment set is a confusing state to diagnose.
NEEDS_CONFIRM=0
while IFS=$'\t' read -r pfile ptargets; do
  [[ "${pfile}" =~ ^#.*$ || -z "${pfile}" ]] && continue
  [[ ",${ptargets}," == *",members,"* || ",${ptargets}," == *",root,"* ]] && NEEDS_CONFIRM=1
done < "${MANIFEST}"
[[ ${NEEDS_CONFIRM} -eq 0 ]] || confirm_production

while IFS=$'\t' read -r pfile ptargets; do
  # Strip before testing, not after. A trailing carriage return makes an
  # otherwise-blank line non-empty, so the skip never fired and the loop
  # reported a phantom manifest entry with no policy document.
  pfile="$(printf '%s' "${pfile}" | tr -d ' \r')"
  ptargets="$(printf '%s' "${ptargets}" | tr -d ' \r')"
  [[ "${pfile}" =~ ^#.*$ || -z "${pfile}" ]] && continue

  # Find the id computed during create/update, by file.
  pid=""; pname=""
  for j in "${!POLICY_IDS[@]}"; do
    if [[ "$(basename "${FILES[$j]}")" == "${pfile}" ]]; then
      pid="${POLICY_IDS[$j]}"; pname="${POLICY_NAMES[$j]}"; break
    fi
  done
  [[ -n "${pid}" ]] || { warn "${pfile} is in the manifest but has no policy document — skipped"; continue; }

  IFS=',' read -ra tlist <<<"${ptargets}"
  for t in "${tlist[@]}"; do
    tgt="$(resolve_target "${t}")"
    [[ -n "${tgt}" && "${tgt}" != "None" ]] || die "Could not resolve target '${t}'."

    attached="$(aws organizations list-policies-for-target --target-id "${tgt}" \
      --filter SERVICE_CONTROL_POLICY --query 'Policies[].Name' --output text 2>/dev/null \
      | no_cr | tr '\t\n' '  ')"

    if [[ " ${attached} " == *" ${pname} "* ]]; then
      skip "${pname} already attached to ${t}"
    else
      run "attach ${pname} -> ${t} (${tgt})" \
        aws organizations attach-policy --policy-id "${pid}" --target-id "${tgt}"
    fi
  done
done < "${MANIFEST}"
hr

warn "FullAWSAccess remains attached. These are Deny policies and rely on it:"
warn "  removing FullAWSAccess without an explicit Allow denies everything."
hr
ok "Done. Now prove it: scripts/test-guardrails.sh --account <member account id>"
