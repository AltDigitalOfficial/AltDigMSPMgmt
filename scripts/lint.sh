#!/usr/bin/env bash
# Run cfn-lint over every CloudFormation template in the repository.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

command -v cfn-lint >/dev/null 2>&1 \
  || die "cfn-lint not found. Run scripts/setup-tooling.sh"

mapfile -t TEMPLATES < <(find_templates)

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

info "cfn-lint over ${#TEMPLATES[@]} template(s)"
cfn-lint "${TEMPLATES[@]}"
ok "cfn-lint clean"
