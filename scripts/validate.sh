#!/usr/bin/env bash
#
# Validate every CloudFormation template against the AWS ValidateTemplate API.
#
# Distinct from lint.sh and guard.sh, and complementary to both:
#
#   validate.sh  server-side, authoritative, needs credentials, no local install
#   lint.sh      cfn-lint     — richer static analysis, needs pip install
#   guard.sh     cfn-guard    — policy compliance, needs a binary install
#
# This exists because local YAML parsing is NOT the same as CloudFormation
# accepting a template. Two templates in this repo sat in the repository for a
# day with an error only the API catches — an intrinsic function in an Outputs
# Description, which is legal YAML and legal in a resource property but
# rejected in an Output. They were never caught because only the template that
# happened to be deployed was ever validated.
#
# Run this over ALL templates, not just the one being deployed.
#
# Usage: scripts/validate.sh [path ...]

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cli

if [[ $# -gt 0 ]]; then
  TEMPLATES=("$@")
else
  mapfile -t TEMPLATES < <(find "${REPO_ROOT}/org" "${REPO_ROOT}/baseline" \
    "${REPO_ROOT}/policies" -name '*.yaml' -o -name '*.yml' 2>/dev/null | sort)
fi

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

info "Validating ${#TEMPLATES[@]} template(s) against the CloudFormation API"
FAILED=0
for t in "${TEMPLATES[@]}"; do
  cfn_validate "${t}" || FAILED=1
done

[[ ${FAILED} -eq 0 ]] || die "One or more templates failed validation."
ok "all templates valid"
