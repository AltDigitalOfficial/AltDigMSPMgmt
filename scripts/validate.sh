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
# ---------------------------------------------------------------------------
# Why discovery is by CONTENT and not by a directory list
# ---------------------------------------------------------------------------
# The first version of this script enumerated org/, baseline/ and policies/.
# security/ was added to the repository afterwards and was never added here, so
# the two templates holding the log archive — the most consequential templates
# in the repo — were excluded from the sweep that exists to stop exactly that.
# A malformed Fn::If sat in one of them until a deploy caught it.
#
# A directory list is a thing to remember. Remembering is the failure mode this
# script was written to remove, so it cannot be part of the mechanism. Anything
# carrying AWSTemplateFormatVersion is a template and gets validated, wherever
# it lives and whenever it is added.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cli

if [[ $# -gt 0 ]]; then
  TEMPLATES=("$@")
else
  mapfile -t TEMPLATES < <(
    find "${REPO_ROOT}" \
      \( -name .git -o -name node_modules -o -name .venv \) -prune -o \
      \( -name '*.yaml' -o -name '*.yml' \) -print 2>/dev/null \
    | sort \
    | while read -r f; do
        # head, not grep over the whole file: the marker is required to be the
        # first line, and this keeps a large policy document from matching on
        # an incidental mention in a comment.
        head -n 5 "${f}" | grep -q 'AWSTemplateFormatVersion' && printf '%s\n' "${f}"
      done
  )
fi

[[ ${#TEMPLATES[@]} -gt 0 ]] || { warn "No templates found."; exit 0; }

# ValidateTemplate is read-only and works from any account, so this script
# deliberately does not call require_account — it should stay usable without a
# platform session. But it must SAY which account it used: on 2026-09-17 it
# validated eleven platform templates against an unrelated AWS account and
# printed eleven green lines, and nothing in that output hinted at it.
CALLER="$(aws sts get-caller-identity --query Account --output text 2>/dev/null | no_cr)" || true
info "Validating ${#TEMPLATES[@]} template(s) against the CloudFormation API"
info "  account ${CALLER:-<unknown>} via profile ${AWS_PROFILE:-<default chain>}"
FAILED=0
for t in "${TEMPLATES[@]}"; do
  cfn_validate "${t}" || FAILED=1
done

[[ ${FAILED} -eq 0 ]] || die "One or more templates failed validation."
ok "all templates valid"
