#!/usr/bin/env bash
# Validate every CloudFormation template against policies/guard/.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

command -v cfn-guard >/dev/null 2>&1 \
  || die "cfn-guard not found. Run scripts/setup-tooling.sh"

RULES_DIR="${REPO_ROOT}/policies/guard"

mapfile -t TEMPLATES < <(find "${REPO_ROOT}/org" "${REPO_ROOT}/identity" "${REPO_ROOT}/baseline" \
  -name '*.yaml' -o -name '*.yml' 2>/dev/null | sort)

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

FAILED=0
for t in "${TEMPLATES[@]}"; do
  info "guard: $(basename "${t}")"
  if ! cfn-guard validate --rules "${RULES_DIR}" --data "${t}" --show-summary fail; then
    FAILED=1
  fi
done

[[ ${FAILED} -eq 0 ]] || die "cfn-guard found violations."
ok "cfn-guard clean"
