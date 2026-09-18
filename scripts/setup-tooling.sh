#!/usr/bin/env bash
#
# Install the local validation toolchain.
#
# Run once per workstation. Nothing here touches AWS.
#
#   cfn-lint    — pip, cross-platform
#   cfn-guard   — Rust binary, downloaded from the GitHub release page
#   terraform   — PagerDuty only (design doc 13); winget on Windows
#   pyyaml      — questionnaire parsing for the vesting pipeline
#   jsonschema  — questionnaire validation for the vesting pipeline
#
# This script PRINTS what it would install and asks for confirmation. It does
# not silently pull binaries onto the machine.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

hr
log "Local toolchain check"
hr

# find_tool, not `command -v`.
#
# find_tool is what the pre-commit hook and every other script use: it looks on
# PATH first, then in the places pip --user and winget actually install to on
# Windows, which are frequently not on PATH.
#
# This script used `command -v` and therefore disagreed with the rest of the
# repository — reporting cfn-lint and cfn-guard as "not installed" and printing
# install instructions for tools that were present and working, on the same
# machine, in the same session. A setup checker that is wrong about the setup is
# worse than none, because the natural response is to install a second copy.
need_cfn_lint=0
if LINT_PATH="$(find_tool cfn-lint)"; then
  ok "cfn-lint  $("${LINT_PATH}" --version 2>&1)"
else
  warn "cfn-lint  not installed"
  need_cfn_lint=1
fi

need_cfn_guard=0
if GUARD_PATH="$(find_tool cfn-guard)"; then
  ok "cfn-guard $("${GUARD_PATH}" --version 2>&1)"
else
  warn "cfn-guard not installed"
  need_cfn_guard=1
fi

need_terraform=0
if TF_PATH="$(find_tool terraform)"; then
  ok "terraform $("${TF_PATH}" version 2>&1 | head -1)"
else
  warn "terraform not installed"
  need_terraform=1
fi

# The vesting pipeline validates a completed questionnaire before it creates
# three AWS accounts. Both libraries are pure Python and tiny; the reason they
# are checked here rather than left to fail at run time is that the failure
# would land midway through an onboarding.
need_py=0
for mod in yaml jsonschema; do
  if python -c "import ${mod}" >/dev/null 2>&1; then
    ok "python ${mod}"
  else
    warn "python ${mod} not installed"
    need_py=1
  fi
done

if [[ ${need_cfn_lint} -eq 0 && ${need_cfn_guard} -eq 0       && ${need_terraform} -eq 0 && ${need_py} -eq 0 ]]; then
  hr; ok "Toolchain complete."; exit 0
fi

hr
log "To install:"
[[ ${need_cfn_lint} -eq 1 ]] && log "  pip install --user cfn-lint"
if [[ ${need_cfn_guard} -eq 1 ]]; then
  log ""
  log "  cfn-guard — download the Windows build from:"
  log "    https://github.com/aws-cloudformation/cloudformation-guard/releases/latest"
  log "    file: cfn-guard-v3-x86_64-windows-latest.tar.gz"
  log "    extract cfn-guard.exe somewhere on PATH"
  log ""
  log "  or, if Rust is available:  cargo install cfn-guard"
fi
if [[ ${need_terraform} -eq 1 ]]; then
  log ""
  log "  terraform — used ONLY for PagerDuty, per design doc 13."
  log "    winget install --id Hashicorp.Terraform -e"
  log "    (find_tool knows the winget install path; a new shell is not needed)"
fi
if [[ ${need_py} -eq 1 ]]; then
  log ""
  log "  python -m pip install --user pyyaml jsonschema"
fi
hr
warn "Not installing automatically. Run the commands above, then re-run this script."
