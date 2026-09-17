#!/usr/bin/env bash
# Run cfn-lint over every CloudFormation template in the repository.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TOOL="$(find_tool cfn-lint)" \
  || die "cfn-lint not found. Run scripts/setup-tooling.sh"

mapfile -t TEMPLATES < <(find_templates)

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

info "cfn-lint over ${#TEMPLATES[@]} template(s)"
"${TOOL}" "${TEMPLATES[@]}"
ok "cfn-lint clean"
