#!/usr/bin/env bash
#
# Install the local validation toolchain.
#
# Run once per workstation. Nothing here touches AWS.
#
#   cfn-lint   — pip, cross-platform
#   cfn-guard  — Rust binary, downloaded from the GitHub release page
#
# This script PRINTS what it would install and asks for confirmation. It does
# not silently pull binaries onto the machine.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

hr
log "Local toolchain check"
hr

need_cfn_lint=0
if command -v cfn-lint >/dev/null 2>&1; then
  ok "cfn-lint  $(cfn-lint --version 2>&1)"
else
  warn "cfn-lint  not installed"
  need_cfn_lint=1
fi

need_cfn_guard=0
if command -v cfn-guard >/dev/null 2>&1; then
  ok "cfn-guard $(cfn-guard --version 2>&1)"
else
  warn "cfn-guard not installed"
  need_cfn_guard=1
fi

if [[ ${need_cfn_lint} -eq 0 && ${need_cfn_guard} -eq 0 ]]; then
  hr; ok "Toolchain complete."; exit 0
fi

hr
log "To install:"
[[ ${need_cfn_lint} -eq 1 ]] && log "  pip install --user cfn-lint"
if [[ ${need_cfn_guard} -eq 1 ]]; then
  log ""
  log "  cfn-guard — download the Windows build from:"
  log "    https://github.com/aws-cloudformation/cloudformation-guard/releases/latest"
  log "    file: cfn-guard-v3-x86_64-pc-windows-msvc.tar.gz"
  log "    extract cfn-guard.exe somewhere on PATH"
  log ""
  log "  or, if Rust is available:  cargo install cfn-guard"
fi
hr
warn "Not installing automatically. Run the commands above, then re-run this script."
