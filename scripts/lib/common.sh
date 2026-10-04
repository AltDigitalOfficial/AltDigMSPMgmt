#!/usr/bin/env bash
# Shared helpers for platform scripts.
#
# Deliberately dependency-free: AWS CLI v2 and coreutils only. No jq — every
# AWS call uses --query/--output text so this runs identically on the Windows
# dev box and in CI.

set -euo pipefail

# --- Windows / Git Bash argument mangling ----------------------------------
#
# MSYS (which Git Bash runs on) rewrites arguments that look like POSIX paths
# into Windows paths before exec. A parameter name of '/platform/org/id'
# arrives at aws.exe as 'C:/Program Files/Git/platform/org/id', and AWS rejects
# it with the genuinely unhelpful "Parameter name must be a fully qualified
# name".
#
# This affects far more than SSM: IAM paths, CloudWatch log group names
# beginning /aws/, S3 keys, and any JMESPath or ARN argument starting with a
# slash. Disabling conversion once here is the only reliable fix — remembering
# to quote or double-slash at each call site is not.
#
# Harmless on Linux and macOS: both variables are simply ignored.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# --- paths -----------------------------------------------------------------

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${LIB_DIR}/../.." && pwd)"
export REPO_ROOT

# shellcheck source=/dev/null
source "${REPO_ROOT}/config/platform.env"

# Default the CLI profile, so that forgetting to export it does not silently
# run against whatever this workstation's default credentials happen to be —
# which here is an IAM user in an unrelated AWS account. See the comment on
# PLATFORM_AWS_PROFILE in config/platform.env for the near-miss that prompted
# this.
#
# An AWS_PROFILE already in the environment always wins: overriding a
# deliberate choice would be worse than the problem being solved.
if [[ -z "${AWS_PROFILE:-}" && -n "${PLATFORM_AWS_PROFILE:-}" ]]; then
  export AWS_PROFILE="${PLATFORM_AWS_PROFILE}"
fi

# --- output ----------------------------------------------------------------

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

# The AWS CLI on Windows terminates lines with CRLF. Command substitution
# strips the trailing \n but leaves the \r, so the FINAL element of any
# --output text result carries an invisible carriage return. That silently
# breaks substring tests and awk field comparisons — and only ever for the last
# item, which makes it look like eventual consistency rather than a bug.
# Pipe every --output text read through this.
no_cr() { tr -d '\r'; }

# Counterpart to MSYS_NO_PATHCONV above. Disabling automatic conversion fixes
# arguments that only LOOK like paths (SSM names, ARNs), but it also stops real
# filesystem paths being translated — aws.exe cannot open '/c/AltDigital/...'.
# So convert explicitly, and only where the argument genuinely is a file.
# 'cygpath -m' yields C:/AltDigital/... — a Windows path with forward slashes,
# which file:// URIs accept. No-op off Windows, where cygpath does not exist.
# Note the asymmetry: cygpath emits a trailing newline, printf does not. Any
# caller building a LIST of paths must strip it -- command substitution does,
# which is why find_templates below wraps this in "$(...)". Missed once, and
# the symptom was cfn-lint reporting twice as many templates as exist and
# failing on a blank filename.
win_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

# --- tool discovery --------------------------------------------------------
#
# pip --user on Windows installs console scripts to
# %APPDATA%/Python/PythonXY/Scripts, which is not on PATH by default, and a
# downloaded binary lands wherever it was put. Rather than require every
# workstation to fix its PATH — and silently skip validation wherever it has
# not been fixed — look in the usual places.
#
# find_tool <name> -> absolute path on stdout, non-zero if not found
find_tool() {
  local name="$1" c
  if command -v "${name}" >/dev/null 2>&1; then command -v "${name}"; return 0; fi
  for c in \
    "${HOME}/.local/bin/${name}.exe" \
    "${HOME}/.local/bin/${name}" \
    "${APPDATA:-${HOME}/AppData/Roaming}/Python/Python313/Scripts/${name}.exe" \
    "${APPDATA:-${HOME}/AppData/Roaming}/Python/Python312/Scripts/${name}.exe" \
    "${APPDATA:-${HOME}/AppData/Roaming}/Python/Scripts/${name}.exe"     "${LOCALAPPDATA:-${HOME}/AppData/Local}/Microsoft/WinGet/Links/${name}.exe"     "${LOCALAPPDATA:-${HOME}/AppData/Local}/Microsoft/WinGet/Packages/Hashicorp.Terraform_Microsoft.Winget.Source_8wekyb3d8bbwe/${name}.exe"
  do
    [[ -x "${c}" ]] && { printf '%s' "${c}"; return 0; }
  done
  return 1
}

log()   { printf '%s\n' "$*"; }
info()  { printf '%s==>%s %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()    { printf '%s  ok%s  %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
skip()  { printf '%sskip%s  %s\n' "${C_DIM}" "${C_RESET}" "$*"; }
warn()  { printf '%swarn%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()   { printf '%sFAIL%s  %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

hr()    { printf '%s\n' "----------------------------------------------------------------------"; }

# --- dry-run ---------------------------------------------------------------
#
# DRY_RUN=1 makes every mutating call print instead of execute. Scripts are
# expected to set it from a --dry-run flag. Read-only calls run regardless,
# so a dry run still reports real current state rather than a guess.

DRY_RUN="${DRY_RUN:-0}"

# run <description> <command...>
run() {
  local desc="$1"; shift
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  %s\n' "${C_YELLOW}" "${C_RESET}" "${desc}"
    printf '      %s$ %s%s\n' "${C_DIM}" "$*" "${C_RESET}"
    return 0
  fi
  info "${desc}"
  "$@"
}

# --- guards ----------------------------------------------------------------

require_cli() {
  command -v aws >/dev/null 2>&1 || die "AWS CLI not found on PATH."
  local major
  major="$(aws --version 2>&1 | sed -n 's|^aws-cli/\([0-9]\+\).*|\1|p')"
  [[ "${major}" == "2" ]] || die "AWS CLI v2 required; found $(aws --version 2>&1)."
}

# Refuses to continue unless the caller is in the expected account. Every
# script that mutates the Organization calls this first. Pointing a bootstrap
# script at the wrong account is the single easiest catastrophic mistake here.
require_account() {
  local expected="$1" out rc

  # stderr is captured rather than discarded. The previous version sent it to
  # /dev/null and reported "Are credentials configured?" for every failure,
  # which points at configuration — and the overwhelmingly common cause is an
  # expired SSO token, where the configuration is perfectly correct and the
  # answer is one command. PlatformBootstrapAdmin sessions last ONE HOUR by
  # design, so this is not an edge case; it is most of the failures.
  #
  # The `if` matters. Under `set -e` a failing command substitution in a plain
  # assignment terminates the shell before the next line runs, so the obvious
  #     out="$(aws ...)"; rc=$?
  # exits 255 with no message at all — worse than the message being replaced.
  # An assignment is a simple command and gets no exemption; a condition does.
  if out="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
    rc=0
  else
    rc=$?
  fi
  out="$(printf '%s' "${out}" | no_cr)"

  if [[ ${rc} -ne 0 ]]; then
    case "${out}" in
      *"Token has expired"*|*expired*|*SSO*|*sso*)
        die "SSO session expired.

      aws sso login --profile ${AWS_PROFILE:-${PLATFORM_AWS_PROFILE:-<profile>}}

      PlatformBootstrapAdmin sessions are one hour by design, so expect this
      roughly hourly during a long build session." ;;
      *"could not be found"*)
        die "AWS profile ${AWS_PROFILE:-<unset>} is not configured.
      Check ~/.aws/config, or unset AWS_PROFILE to use the default chain." ;;
      *)
        die "Could not call sts:GetCallerIdentity:
      ${out}" ;;
    esac
  fi

  if [[ "${out}" != "${expected}" ]]; then
    die "Wrong account. Expected ${expected}, credentials resolve to ${out}.
      Profile in use: ${AWS_PROFILE:-<none — using the default chain>}
      Set AWS_PROFILE to a profile for ${expected} and retry."
  fi
  ok "Account ${out} confirmed."
}

caller_arn() {
  aws sts get-caller-identity --query Arn --output text | no_cr
}

# Root credentials should be used for exactly one thing: creating the first
# federated admin path. Everything else is a finding.
warn_if_root() {
  local arn; arn="$(caller_arn)"
  if [[ "${arn}" == *":root" ]]; then
    warn "Running as the ROOT user (${arn})."
    warn "Acceptable only for initial bootstrap. Move to Identity Center as soon as it exists."
  fi
}

confirm_production() {
  if [[ "${CONFIRM_PRODUCTION:-0}" != "1" ]]; then
    die "This action targets production. Re-run with --confirm-production."
  fi
}

# --- ssm -------------------------------------------------------------------

# put_param <name-suffix> <value> <description>
# Writes under ${PLATFORM_SSM_PREFIX}. Overwrites: these are derived facts,
# not secrets, and re-running bootstrap must be idempotent.
#
# Handles dry-run explicitly rather than going through run(). Passing this
# through run() with a trailing >/dev/null redirects run()'s own output as well
# as the command's, which silently swallowed the entire dry-run report.
put_param() {
  local suffix="$1" value="$2" desc="$3"
  local name="${PLATFORM_SSM_PREFIX}${suffix}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  SSM %s = %s\n' "${C_YELLOW}" "${C_RESET}" "${name}" "${value}"
    return 0
  fi
  aws ssm put-parameter \
    --name "${name}" \
    --value "${value}" \
    --type String \
    --description "${desc}" \
    --overwrite \
    --output text --query Version >/dev/null
  ok "SSM ${name} = ${value}"
}

get_param() {
  local suffix="$1"
  aws ssm get-parameter --name "${PLATFORM_SSM_PREFIX}${suffix}" \
    --query Parameter.Value --output text 2>/dev/null | no_cr || true
}

# --- lambda packaging ------------------------------------------------------
#
# publish_artifact <build_dir> <key_prefix> <bucket> -> prints the object key
#
# Zips with python rather than zip(1): Git for Windows does not ship zip, and
# more importantly the archive must be DETERMINISTIC. The object key is a hash
# of the package so that identical input produces an identical key — which is
# what stops CloudFormation missing an update and stops a no-op publish
# creating a new object. A zip stores each entry's mtime, so the same source
# zipped twice hashes differently and content-addressing degrades into "a new
# key every run". Fixed date_time and sorted entries remove both.
#
# require_committed <dir...> — refuse to package sources that are not committed.
#
# Every publish-*.sh packages what is ON DISK. On 2026-10-01 a publish of
# change/src picked up another session's uncommitted, in-progress edits to
# reconcile.py and jira.py, and deployed them as the live change-reconciliation
# control in Audit. The package hash matched neither the commit nor the later
# working tree, so the deployed code was a state that never existed in
# history. It was rolled back to the previous package. Parallel sessions on one
# working tree make this the normal case, not an edge.
#
# A package deployed as a platform control should always be a commit someone
# can name, so there is no override flag. Commit or stash first. Untracked
# files count: a new module nobody has committed is just as unreviewed.
require_committed() {
  local dirty
  # win_path: with MSYS_NO_PATHCONV set, a Windows git.exe receives /c/... as
  # a path that does not exist and exits 128. Under set -e that killed the
  # caller silently, before this check could say anything (trap 1 in
  # CLAUDE.md). And a check that cannot run must stop the publish loudly,
  # never wave it through.
  if ! dirty="$(git -C "$(win_path "${REPO_ROOT}")" status --porcelain -- "$@" 2>&1)"; then
    die "Could not check that the sources are committed:
        ${dirty}
      Refusing to package unchecked sources."
  fi
  dirty="$(printf '%s' "${dirty}" | no_cr)"
  [[ -z "${dirty}" ]] || die "Uncommitted changes in sources this would package:
$(printf '%s\n' "${dirty}" | sed 's/^/        /')
      A publish deploys what is on disk. Commit (or stash) first, so the
      package is a commit someone can name. If another session owns these
      edits, wait for it to commit."
}

# Shared rather than copied. Two publish scripts existed with the same thirty
# lines, which is the arrangement where one gets a fix and the other does not.
zip_deterministic() {
  local build="$1"
  local py; py="$(command -v python || command -v python3)"
  "${py}" - "$(win_path "${build}")" <<'ZIPEOF'
import sys, zipfile, pathlib
build = pathlib.Path(sys.argv[1])
# Recursive. CloudWatch Synthetics requires a Python canary at python/<name>.py
# inside the zip, so a flat walk silently produced a package the runtime could
# not find a handler in. Sorted relative POSIX paths keep the archive
# byte-identical across runs and across Windows and Linux, which is what makes
# the content hash a usable cache key.
names = sorted(
    f.relative_to(build).as_posix()
    for f in build.rglob("*")
    if f.is_file() and f.name != "package.zip"
)
with zipfile.ZipFile(build / "package.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name in names:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, (build / name).read_bytes())
print(f"  {len(names)} file(s): {', '.join(names)}")
ZIPEOF
}

package_hash() {
  local py; py="$(command -v python || command -v python3)"
  "${py}" -c "
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:16])
" "$(win_path "$1")"
}

# --- organizations ---------------------------------------------------------

org_exists() {
  aws organizations describe-organization --query Organization.Id --output text 2>/dev/null | no_cr
}

org_root_id() {
  aws organizations list-roots --query 'Roots[0].Id' --output text 2>/dev/null | no_cr
}

# find_ou <parent-id> <name> -> ou id, or empty
find_ou() {
  local parent="$1" name="$2"
  aws organizations list-organizational-units-for-parent \
    --parent-id "${parent}" \
    --query "OrganizationalUnits[?Name=='${name}'].Id | [0]" \
    --output text 2>/dev/null | no_cr | sed 's/^None$//'
}

# --- naming ----------------------------------------------------------------
#
# Naming rules from design doc 15. Enforced in code, not just documented,
# because account aliases are globally unique and immutable in practice, and
# because every billing dimension is derived from the account name.

# Length budget. TWO limits apply, and the EMAIL is the tighter of the two —
# easy to miss, because the alias is the more obvious constraint. Sizing the
# slugs against the alias alone once produced a 71-octet local part: invalid
# addresses for any client with long slugs, and only discovered at vesting,
# mid-saga.
#
#   AWS account alias, max 63 — keeps the full environment word:
#     altdig-  partner  -  client  -  app  - env    = total
#        7    +   18   + 1 +  18  + 1 + 10 + 1 + 4  =  60
#
#   Email local part, max 64 octets (RFC 5321 s4.5.3.1.1) — short base, no
#   'ad-' prefix, one-character tier:
#     mspr  +  partner  -  client  -  app  - tier  = total
#      4   + 1 +  18   + 1 +  18  + 1 + 10 + 1 + 1 =  55
#
# The ALIAS is now the binding constraint, not the email. The 'altdig' prefix
# costs four characters over 'ad' and buys collision resistance that 'ad' did
# not provide — see config/platform.env. The email carries no prefix at all, so
# it has 9 characters of slack.
#
# These caps must not be raised independently of each other, and account_email
# enforces the limit rather than trusting them.
ACCOUNT_ALIAS_MAX=63
EMAIL_LOCAL_MAX=64
SLUG_RE='^[a-z0-9]([a-z0-9-]{0,16}[a-z0-9])?$'       # 1-18 chars
APP_SLUG_RE='^[a-z0-9]([a-z0-9-]{0,8}[a-z0-9])?$'    # 1-10 chars

validate_slug() {
  local kind="$1" value="$2"
  [[ "${value}" =~ ${SLUG_RE} ]] \
    || die "Invalid ${kind} slug '${value}'. Lowercase alphanumeric and hyphens,
      must start and end alphanumeric, 1-20 characters. No dots (oeight.io -> oeight)."
}

validate_app_slug() {
  local value="$1"
  [[ "${value}" =~ ${APP_SLUG_RE} ]] \
    || die "Invalid application short code '${value}'. Lowercase alphanumeric and
      hyphens, must start and end alphanumeric, 1-12 characters."
}

# account_alias <partner> <client> <app|""> <env>
#
# Argument order matches output order exactly, and <app> is a required
# positional that may be empty. An optional trailing argument reads more nicely
# but puts <env> in the third slot while it appears fourth in the output, which
# is a genuine footgun — pass "" rather than omitting it.
account_alias() {
  local partner="$1" client="$2" app="$3" env="$4"
  local alias
  if [[ -n "${app}" ]]; then
    alias="${PLATFORM_ACCOUNT_PREFIX}-${partner}-${client}-${app}-${env}"
  else
    alias="${PLATFORM_ACCOUNT_PREFIX}-${partner}-${client}-${env}"
  fi
  if [[ ${#alias} -gt ${ACCOUNT_ALIAS_MAX} ]]; then
    die "Account alias '${alias}' is ${#alias} characters; the AWS limit is ${ACCOUNT_ALIAS_MAX}.
      Shorten the partner, client or application slug."
  fi
  printf '%s' "${alias}"
}

# env_tier <env> -> single character
#
# Used only in the email. The account alias keeps the full word. No two
# environment names share a first letter, so this is unambiguous — but it is a
# case statement rather than a substring so that adding a fifth environment
# cannot silently collide.
env_tier() {
  case "$1" in
    dev)  printf 'd' ;;
    test) printf 't' ;;
    uat)  printf 'u' ;;
    prod) printf 'p' ;;
    *) die "Unknown environment '$1'. Expected dev, test, uat or prod." ;;
  esac
}

# account_email <partner> <client> <app|""> <env> -> member account root email
#
#   mspr+oeight-arc8-p@altdigital.ai
#   mspr+oeight-avergent-cms-u@altdigital.ai
#
# Same argument order as account_alias deliberately, so the two cannot be
# called inconsistently. This is the MEMBER account address; the management
# account uses PLATFORM_ROOT_EMAIL and a different mailbox entirely.
#
# Enforces the 64-octet limit rather than trusting the slug caps, so raising a
# cap without re-reading the budget above fails loudly here instead of
# producing an invalid root address that AWS rejects part-way through vesting.
account_email() {
  local partner="$1" client="$2" app="$3" env="$4"
  local tier; tier="$(env_tier "${env}")"
  local tag
  if [[ -n "${app}" ]]; then
    tag="${partner}-${client}-${app}-${tier}"
  else
    tag="${partner}-${client}-${tier}"
  fi
  local local_part="${PLATFORM_EMAIL_LOCAL}+${tag}"
  if [[ ${#local_part} -gt ${EMAIL_LOCAL_MAX} ]]; then
    die "Root email local part '${local_part}' is ${#local_part} octets; RFC 5321
      allows ${EMAIL_LOCAL_MAX}. Shorten the partner, client or application slug.
      The email is a TIGHTER constraint than the ${ACCOUNT_ALIAS_MAX}-character
      account alias — an alias that fits can still yield an invalid address."
  fi
  printf '%s@%s' "${local_part}" "${PLATFORM_EMAIL_DOMAIN}"
}

# --- platform (non-member) accounts ----------------------------------------
#
# The design package names member accounts (ad-<partner>-<client>-<env>) but is
# silent on the seven platform accounts. Convention adopted here:
#
#   ad-<ou>-<role>      ad-security-logarchive, ad-infra-tooling, ad-sandbox-canary
#
# OU placement is readable from the account name, which is what you have in a
# support ticket or a Cost Explorer row. 'platform' is not used as the scope
# segment because it would collide conceptually with a partner slug of the same
# name, and because it discards the OU hint for no gain.

PLATFORM_OUS="security infra sandbox"

platform_account_alias() {
  local ou="$1" role="$2"
  validate_slug "platform-ou" "${ou}"
  validate_slug "platform-role" "${role}"
  local alias="${PLATFORM_ACCOUNT_PREFIX}-${ou}-${role}"
  [[ ${#alias} -le ${ACCOUNT_ALIAS_MAX} ]]     || die "Platform account alias '${alias}' is ${#alias} chars; limit ${ACCOUNT_ALIAS_MAX}."
  printf '%s' "${alias}"
}

platform_account_email() {
  local ou="$1" role="$2"
  local local_part="${PLATFORM_EMAIL_LOCAL}+${ou}-${role}"
  [[ ${#local_part} -le ${EMAIL_LOCAL_MAX} ]]     || die "Platform root email local part '${local_part}' is ${#local_part} octets; limit ${EMAIL_LOCAL_MAX}."
  printf '%s@%s' "${local_part}" "${PLATFORM_EMAIL_DOMAIN}"
}

# --- contact details -------------------------------------------------------
#
# normalise_phone <value> -> E.164, or empty on failure
#
# AWS PutAlternateContact is permissive: ^[\s0-9()+-]+$ at 1-25 characters, so
# "(612) 555-0123" is accepted as-is. E.164 is a house standard rather than an
# API requirement — these contacts are later propagated to every member
# account, and "+16125550123" is unambiguous where a bare ten digits requires
# the reader to already know it is US.
#
# So this normalises rather than rejects. It fails only on input that cannot be
# resolved to a single unambiguous number, and it never echoes the value.
normalise_phone() {
  local raw="$1" digits

  # Note the trailing hyphen in the tr set. Written as ' ()-.' the sequence
  # ')-.' is a character RANGE (0x29-0x2E) which also deletes '+', ',' and '*'
  # — that silently mangles any international number into an unparseable one.
  # Keeping '-' last makes it a literal.
  digits="$(printf '%s' "${raw}" | tr -d ' ().-')"

  # Regex throughout rather than case globs: '??????????' matches any ten
  # characters, so "not-a-number" would normalise to "+1notanumber".
  if [[ "${digits}" =~ ^\+[1-9][0-9]{7,14}$ ]]; then
    printf '%s' "${digits}"; return 0        # already international
  fi
  if [[ "${digits}" =~ ^1[0-9]{10}$ ]]; then
    printf '+%s' "${digits}"; return 0       # 11 digits, NANP country code
  fi
  if [[ "${digits}" =~ ^[0-9]{10}$ ]]; then
    printf '+1%s' "${digits}"; return 0      # bare 10-digit NANP
  fi
  return 1
}

# --- cloudformation --------------------------------------------------------
#
# All stack deploys go through here so behaviour is uniform: validated before
# submission, idempotent, and a real changeset on --dry-run rather than a
# narrated guess.

# find_templates -> every CloudFormation template, one per line, in a form the
# linters can actually open.
#
# win_path is not optional here. common.sh exports MSYS_NO_PATHCONV=1 so that
# SSM paths and ARNs survive the trip to aws.exe, and the side effect is that
# REAL paths stop being converted too: cfn-lint.exe and cfn-guard.exe are
# Windows binaries and cannot open '/c/AltDigital/...'. The failure is
# 'could not be processed by glob.glob', which reads like a bad pattern rather
# than a bad path, and cfn-lint still exits 0 on it under some invocations —
# so a lint run could report success having linted nothing at all.
# Discovery is by CONTENT, not by a directory list.
#
# It was `for d in org identity baseline` until 2026-09-18, which meant
# security/, alerting/, registry/, reporting/ and vesting/ had never been linted
# or guarded — eleven of sixteen templates checked, and the pre-commit hook
# reporting clean the whole time. The same fault was found and fixed in
# validate.sh a day earlier and left here, which is the argument for one
# implementation rather than two: fixing a bug in a copy leaves the bug.
#
# A directory list is a thing to remember, and remembering is the failure mode
# these scripts exist to remove, so it cannot be part of the mechanism.
# design/ is excluded because it holds source material, including a YAML alarm
# specification that is not a template.
find_templates() {
  find "${REPO_ROOT}"     -type d \( -name .git -o -name design -o -name node_modules \) -prune -o     -type f \( -name '*.yaml' -o -name '*.yml' \) -print   | sort | while read -r t; do
      # head, not grep over the whole file: the marker is required to be on the
      # first line, so this cannot match an incidental mention in a comment.
      head -n 5 "${t}" | grep -q 'AWSTemplateFormatVersion' || continue
      printf '%s
' "$(win_path "${t}")"
    done
}

cfn_validate() {
  local template="$1"
  [[ -f "${template}" ]] || die "Template not found: ${template}"
  aws cloudformation validate-template \
    --template-body "file://$(win_path "${template}")" \
    --query 'Description' --output text >/dev/null \
    || die "Template failed validation: ${template}"
  ok "validated $(basename "${template}")"
}

# cfn_deploy <stack-name> <template> [Key=Value ...]
cfn_deploy() {
  local stack="$1" template="$2"; shift 2
  local params=("$@")

  cfn_validate "${template}"

  local args=(
    cloudformation deploy
    --stack-name "${stack}"
    --template-file "$(win_path "${template}")"
    --capabilities CAPABILITY_NAMED_IAM
    --no-fail-on-empty-changeset
    # platform-layer on every resource via the stack (B-157 phase B).
    --tags platform-managed=true platform-layer=infrastructure
  )
  if [[ ${#params[@]} -gt 0 ]]; then
    args+=(--parameter-overrides "${params[@]}")
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    # --no-execute-changeset produces a real changeset AWS will describe,
    # then leaves it unexecuted. This is the difference between "what I think
    # would happen" and "what CloudFormation says will happen".
    printf '%s DRY%s  changeset only for stack %s\n' "${C_YELLOW}" "${C_RESET}" "${stack}"
    aws "${args[@]}" --no-execute-changeset || true
    log ""
    log "  Review with: aws cloudformation describe-change-set --change-set-name <arn>"
    return 0
  fi

  info "Deploying stack ${stack}"
  aws "${args[@]}"
  ok "stack ${stack} deployed"
}

cfn_outputs() {
  local stack="$1"
  aws cloudformation describe-stacks --stack-name "${stack}" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text 2>/dev/null || true
}

# --- secrets that more than one account reads ---------------------------------
#
# B-110. A secret is only useful in the account that reads it, and several of
# them are now read from two: reconciliation and quorum run in Audit, while
# provisioning runs in Tooling alongside the registry. Cross-account reads are
# not available for these — they are encrypted under the AWS-managed
# `aws/secretsmanager` key, which cannot be shared between accounts.
#
# Two copies of a credential is a rotation hazard: update one, miss the other,
# and the missed account keeps working on the old value until that value is
# revoked, at which point it fails in a way that looks like an outage rather
# than a half-finished rotation.
#
# So the fan-out lives HERE rather than in each script. Two copies of this
# logic would drift exactly the way the two copies of the credential would,
# which would be a poor joke.

resolve_accounts() {
  # resolve_accounts <alias> [<alias>...] -> prints "alias:account-id" per line
  #
  # ALL of them, before any caller writes anything. An alias that does not
  # resolve must stop the run rather than be discovered after the first
  # account has already been rotated.
  local key acct
  for key in "$@"; do
    acct="$(get_param "/org/account/${key}")"
    [[ -n "${acct}" && "${acct}" != "None" ]] \
      || die "Account ${key} is not registered in SSM. Nothing has been written."
    printf '%s:%s\n' "${key}" "${acct}"
  done
}

in_account() (
  # in_account <account-id> <command...>
  #
  # A subshell, so the assumed-role credentials cannot leak into the next
  # iteration. Getting that wrong would write the second account's secret
  # using the first account's credentials — which either fails confusingly or,
  # worse, writes the same account twice and reports two successes.
  local acct="$1"; shift
  local creds ak sk st
  creds="$(aws sts assume-role \
    --role-arn "arn:aws:iam::${acct}:role/OrganizationAccountAccessRole" \
    --role-session-name platform-secret-write \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${creds}" ]] || return 3
  read -r ak sk st <<<"${creds}"
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${ak}" AWS_SECRET_ACCESS_KEY="${sk}" \
         AWS_SESSION_TOKEN="${st}"
  MSYS_NO_PATHCONV=1 "$@"
)

read_secret() {
  # read_secret <account-id> <secret-name> -> prints the value, or nothing.
  # Never echoed to a terminal by anything here; used for propagation and for
  # the read-back check.
  in_account "$1" aws secretsmanager get-secret-value \
    --secret-id "$2" --query SecretString --output text 2>/dev/null | no_cr || true
}

secret_fingerprint() {
  # Twelve hex characters of sha256, read from stdin. Enough to tell two
  # accounts apart without displaying either value to whoever is watching the
  # terminal.
  python -c '
import hashlib, sys
raw = sys.stdin.buffer.read().strip()
print(hashlib.sha256(raw).hexdigest()[:12] if raw else "absent")
'
}

store_secret_everywhere() {
  # store_secret_everywhere <secret-name> <description> <payload> <alias:id>...
  #
  # Writes every account, THEN verifies every account by reading back and
  # comparing fingerprints. The verification is a read-back rather than the
  # absence of an error: an API that accepted a put-secret-value has told you
  # it accepted a request, not what is stored.
  local name="$1" description="$2" payload="$3"; shift 3
  local want entry key acct have wrote="" failed="" mismatch=""

  want="$(printf '%s' "${payload}" | secret_fingerprint)"

  if [[ "${DRY_RUN}" == "1" ]]; then
    for entry in "$@"; do
      key="${entry%%:*}"; acct="${entry##*:}"
      have="$(read_secret "${acct}" "${name}" | secret_fingerprint)"
      printf '%s DRY%s  would store %s in %s (%s): %s -> %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${name}" "${key}" "${acct}" "${have}" "${want}"
    done
    return 0
  fi

  for entry in "$@"; do
    key="${entry%%:*}"; acct="${entry##*:}"
    if in_account "${acct}" aws secretsmanager describe-secret \
         --secret-id "${name}" >/dev/null 2>&1; then
      if in_account "${acct}" aws secretsmanager put-secret-value \
           --secret-id "${name}" --secret-string "${payload}" \
           --query VersionId --output text >/dev/null 2>&1; then
        ok "rotated ${name} in ${key} (${acct})"; wrote="${wrote}${key} "
      else
        failed="${failed}${key} "
      fi
    else
      if in_account "${acct}" aws secretsmanager create-secret \
           --name "${name}" --secret-string "${payload}" \
           --description "${description}" --query ARN --output text \
           >/dev/null 2>&1; then
        ok "stored ${name} in ${key} (${acct})"; wrote="${wrote}${key} "
      else
        failed="${failed}${key} "
      fi
    fi
  done

  printf '\n'
  info "verifying by read-back"
  for entry in "$@"; do
    key="${entry%%:*}"; acct="${entry##*:}"
    have="$(read_secret "${acct}" "${name}" | secret_fingerprint)"
    if [[ "${have}" == "${want}" ]]; then
      ok "${key} holds ${have}"
    else
      warn "${key} holds ${have}, expected ${want}"
      mismatch="${mismatch}${key} "
    fi
  done

  if [[ -n "${mismatch}" ]]; then
    printf '\n'
    # Named explicitly. "Rotation failed" is the one message that would leave
    # somebody guessing which half of the estate is stale — and the accounts
    # that DID take the new value are named too, because they are the ones
    # that break when the old credential is revoked.
    warn "PARTIAL ROTATION. Consuming accounts do not agree."
    warn "  updated:  ${wrote:-none}"
    warn "  stale:    ${mismatch}"
    [[ -n "${failed}" ]] && warn "  failed:   ${failed}"
    die "Re-run to finish. Do NOT revoke the old credential until every account above reports the same fingerprint."
  fi

  ok "all consuming accounts hold the same credential (${want})"
}

# --- IAM Identity Center ----------------------------------------------------
#
# Lifted out of scripts/deploy-commercial-access.sh when a second script needed
# the same five lookups. They were not made shared pre-emptively; they were
# made shared the moment there were two callers, which is the point at which
# two copies start to drift. B-104 is the same lesson from the publish scripts.
#
# Every function here depends on IDENTITY_STORE_ID, which identity_center_init
# sets. Passing it to each call would be tidier and would also mean a caller
# could pass a different store to two adjacent lookups, which is not a mistake
# worth leaving available.

identity_center_init() {
  # Sets INSTANCE_ARN and IDENTITY_STORE_ID, and publishes both to SSM.
  #
  # Resolved LIVE rather than configured. There can be only one instance per
  # Organization and its ARN is generated at enablement, so reading it is
  # strictly better than recording it in platform.env where it could drift
  # away from reality without anything noticing.
  read -r INSTANCE_ARN IDENTITY_STORE_ID < <(
    aws sso-admin list-instances \
      --query 'Instances[0].[InstanceArn,IdentityStoreId]' \
      --output text 2>/dev/null | no_cr
  ) || true

  [[ -n "${INSTANCE_ARN:-}" && "${INSTANCE_ARN}" != "None" ]] \
    || die "No IAM Identity Center instance found in ${AWS_DEFAULT_REGION}.
      Identity Center must be enabled in the home region before this runs.
      See docs/bootstrap-runbook.md step 4."

  export INSTANCE_ARN IDENTITY_STORE_ID
  put_param /identity/instance-arn "${INSTANCE_ARN}" \
    "IAM Identity Center instance ARN"
  put_param /identity/identity-store-id "${IDENTITY_STORE_ID}" \
    "IAM Identity Center identity store id"
}

# alternate_identifier <attribute-path> <value> -> JSON for --alternate-identifier
#
# Must be JSON, not AWS CLI shorthand: AttributeValue is a document type and
# shorthand fails with "Shorthand syntax does not support document types".
#
# The failure mode is what makes this worth a comment. The lookups below are
# wrapped in `2>/dev/null || true` because "not found" is a legitimate answer,
# and that swallows the shorthand parse error too — so the call returns empty
# on EVERY invocation, an existing group reads as absent, and the next run
# creates a duplicate instead of reusing it. Cost one deploy to find.
#
# Built with printf rather than inline escaped quotes: the shell-quoted form is
# unreadable and one deleted backslash away from being silently wrong.
alternate_identifier() {
  printf '{"UniqueAttribute":{"AttributePath":"%s","AttributeValue":"%s"}}' "$1" "$2"
}

# group_id_by_name <display-name> -> group id, or empty
group_id_by_name() {
  aws identitystore get-group-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --alternate-identifier "$(alternate_identifier displayName "$1")" \
    --query GroupId --output text 2>/dev/null | no_cr || true
}

# user_id_by_name <user-name> -> user id, or empty
user_id_by_name() {
  aws identitystore get-user-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --alternate-identifier "$(alternate_identifier userName "$1")" \
    --query UserId --output text 2>/dev/null | no_cr || true
}

# is_member <group-id> <user-id> -> 0 if already a member
is_member() {
  aws identitystore get-group-membership-id \
    --identity-store-id "${IDENTITY_STORE_ID}" \
    --group-id "$1" --member-id "UserId=$2" \
    --query MembershipId --output text >/dev/null 2>&1
}

# group_member_usernames <group-id> -> the sign-in name of each member
#
# Used for the leaver report. Nobody is ever REMOVED by a script here — see
# the header of deploy-commercial-access.sh — so this only ever feeds a
# warning.
group_member_usernames() {
  aws identitystore list-group-memberships \
    --identity-store-id "${IDENTITY_STORE_ID}" --group-id "$1" \
    --query 'GroupMemberships[].MemberId.UserId' --output text 2>/dev/null \
    | no_cr | tr '\t' '\n' \
    | while read -r u; do
        [[ -n "${u}" ]] || continue
        aws identitystore describe-user \
          --identity-store-id "${IDENTITY_STORE_ID}" --user-id "${u}" \
          --query UserName --output text 2>/dev/null | no_cr
      done
}
