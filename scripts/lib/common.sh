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

# Length budget. TWO limits apply, and the email is the tighter of the two —
# which is easy to miss, because the alias is the more obvious constraint.
#
#   AWS account alias, max 63:
#     ad-  partner  -  client  -  app  -  env   = total
#      3  +   18   + 1 +  18  + 1 + 10 + 1 + 4  =  56
#
#   Email local part, max 64 octets (RFC 5321 section 4.5.3.1.1):
#     msp-mgmt  +  partner  -  client  -  app  -  env   = total
#        8     + 1 +  18   + 1 +  18  + 1 + 10 + 1 + 4  =  62
#
# Sizing the slugs against the alias alone produced a 71-octet local part,
# i.e. invalid addresses for any client with long slugs — discovered only at
# vesting, mid-saga. Both limits are now enforced in code, at the point the
# slugs are chosen.
#
# Note the email drops the 'ad-' prefix. That prefix exists because account
# aliases are globally unique across all of AWS; email addresses only need to
# be unique within the domain, so it is three wasted octets against the
# tighter limit.
#
# These caps must not be raised independently of each other.
ACCOUNT_ALIAS_MAX=63
EMAIL_LOCAL_MAX=64
SLUG_RE='^[a-z0-9]([a-z0-9-]{0,16}[a-z0-9])?$'      # 1-18 chars
APP_SLUG_RE='^[a-z0-9]([a-z0-9-]{0,8}[a-z0-9])?$'   # 1-10 chars

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

# account_email <alias> -> plus-addressed root email
#
# Takes the account alias and strips the platform prefix: the alias needs
# 'ad-' for global AWS uniqueness, the address does not, and those three
# octets matter against the 64-octet local-part limit.
#
# Enforces that limit rather than trusting the slug caps, so raising a cap
# without re-reading the budget above fails loudly here instead of producing
# an invalid root address that AWS rejects part-way through account vesting.
account_email() {
  local alias="$1"
  local tag="${alias#${PLATFORM_ACCOUNT_PREFIX}-}"
  local local_part="${PLATFORM_EMAIL_LOCAL}+${tag}"
  if [[ ${#local_part} -gt ${EMAIL_LOCAL_MAX} ]]; then
    die "Root email local part '${local_part}' is ${#local_part} octets; RFC 5321
      allows ${EMAIL_LOCAL_MAX}. Shorten the partner, client or application slug.
      Note the email is a TIGHTER constraint than the ${ACCOUNT_ALIAS_MAX}-character
      account alias — an alias that fits can still yield an invalid address."
  fi
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
