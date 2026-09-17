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
  local expected="$1"
  local actual
  actual="$(aws sts get-caller-identity --query Account --output text 2>/dev/null | no_cr)" \
    || die "Could not call sts:GetCallerIdentity. Are credentials configured?"
  if [[ "${actual}" != "${expected}" ]]; then
    die "Wrong account. Expected ${expected}, credentials resolve to ${actual}.
      Set AWS_PROFILE to a profile for ${expected} and retry."
  fi
  ok "Account ${actual} confirmed."
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
find_templates() {
  local d
  for d in org identity baseline; do
    [[ -d "${REPO_ROOT}/${d}" ]] || continue
    find "${REPO_ROOT}/${d}" \( -name '*.yaml' -o -name '*.yml' \) -print
  done | sort | while read -r t; do printf '%s
' "$(win_path "${t}")"; done
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
    --tags platform-managed=true
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
