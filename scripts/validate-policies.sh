#!/usr/bin/env bash
#
# Validate every Organizations policy document with IAM Access Analyzer.
#
# This is NOT cfn-lint or cfn-guard territory: SCPs and RCPs are IAM policy
# documents, not CloudFormation, and neither tool understands their semantics.
# accessanalyzer validate-policy does — it knows which actions exist, which
# condition keys are valid for them, and the syntax rules specific to each
# policy type. It needs credentials but no local install.
#
# Also enforces the 5120-byte quota, which is easy to blow through with a long
# action list and produces a failure only at create-policy time otherwise.
#
# Usage: scripts/validate-policies.sh [file ...]

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cli

POLICY_MAX_BYTES=5120

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  mapfile -t FILES < <(find "${REPO_ROOT}/policies/scp" "${REPO_ROOT}/policies/rcp" \
    -name '*.json' 2>/dev/null | sort)
fi

[[ ${#FILES[@]} -gt 0 ]] || { warn "No policy documents found."; exit 0; }

FAILED=0
for f in "${FILES[@]}"; do
  name="$(basename "${f}")"

  # Policy type is derived from the directory, so a policy cannot be validated
  # against the wrong type by accident.
  case "${f}" in
    */policies/scp/*) ptype=SERVICE_CONTROL_POLICY ;;
    */policies/rcp/*) ptype=RESOURCE_CONTROL_POLICY ;;
    *) warn "${name}: cannot infer policy type from path — skipped"; continue ;;
  esac

  bytes="$(wc -c < "${f}" | tr -d ' ')"
  if [[ ${bytes} -gt ${POLICY_MAX_BYTES} ]]; then
    printf '%sFAIL%s  %-42s %s bytes — exceeds the %s-byte quota\n' \
      "${C_RED}" "${C_RESET}" "${name}" "${bytes}" "${POLICY_MAX_BYTES}"
    FAILED=1
    continue
  fi

  findings="$(aws accessanalyzer validate-policy \
    --policy-type "${ptype}" \
    --policy-document "file://$(win_path "${f}")" \
    --query 'findings[].[findingType,issueCode,findingDetails]' \
    --output text 2>&1 | no_cr)"

  if [[ -z "${findings}" ]]; then
    printf '%s  ok%s  %-42s %4s bytes  %s\n' \
      "${C_GREEN}" "${C_RESET}" "${name}" "${bytes}" "${ptype}"
  else
    # SECURITY_WARNING, ERROR and SUGGESTION are all worth seeing, but only
    # ERROR and SECURITY_WARNING fail the run.
    printf '%swarn%s  %s\n' "${C_YELLOW}" "${C_RESET}" "${name}"
    printf '%s\n' "${findings}" | sed 's/^/        /'
    if printf '%s' "${findings}" | grep -qE '^(ERROR|SECURITY_WARNING)'; then
      FAILED=1
    fi
  fi
done

[[ ${FAILED} -eq 0 ]] || die "Policy validation failed."
ok "all policy documents valid"
