#!/usr/bin/env bash
# Validate every CloudFormation template against policies/guard/.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TOOL="$(find_tool cfn-guard)" \
  || die "cfn-guard not found. Run scripts/setup-tooling.sh"

# win_path for the same reason as the templates -- see find_templates in
# lib/common.sh. cfn-guard reports a missing rules directory, not a path
# problem, so this looks like the rules have not been written yet.
RULES_DIR="$(win_path "${REPO_ROOT}/policies/guard")"

mapfile -t TEMPLATES < <(find_templates)

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

FAILED=0
for t in "${TEMPLATES[@]}"; do
  info "guard: $(basename "${t}")"
  if ! "${TOOL}" validate --rules "$(win_path "${RULES_DIR}")" --data "$(win_path "${t}")" --show-summary fail; then
    FAILED=1
  fi
done

[[ ${FAILED} -eq 0 ]] || die "cfn-guard found violations."
ok "cfn-guard clean"
