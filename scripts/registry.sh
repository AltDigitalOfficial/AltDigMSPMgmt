#!/usr/bin/env bash
#
# Platform registry CLI — the authoritative record of partners, clients and
# member accounts, and the parameters each was provisioned with.
#
#   scripts/registry.sh <command> [options]
#
# Design doc 13 calls the registry "the spine": every other system is
# provisioned FROM a record here and writes back to it. Prompt 3.2 says keep it
# simple — a DynamoDB table and a query CLI, not a web application. This is the
# CLI.
#
# ---------------------------------------------------------------------------
# The two reports this exists for
# ---------------------------------------------------------------------------
# Prompt 3.2 names both, as requirements of design principle P9:
#
#   report-baseline    which accounts are behind the current baseline version
#   report-overrides   what declared local overrides exist, why, and who owns
#
# NOTE ON P9: design doc 01 (Design Principles) is NOT present in the design
# package, so P9 is cited in three places and its text exists nowhere readable.
# These two reports are built to the prompt's explicit wording. Whether they
# are the whole of what P9 asks for cannot be confirmed from the package as it
# stands. See docs/build-backlog.md B-004.
#
# ---------------------------------------------------------------------------
# Declared vs undeclared difference
# ---------------------------------------------------------------------------
# An account differing from the global baseline is not automatically a problem.
# A difference that someone wrote down, justified and put their name against is
# a local override. The same difference with nobody's name on it is drift.
#
# The registry is the record of the first kind. It is what makes the second
# kind detectable at all — without a statement of what an account was SUPPOSED
# to be, AWS can only tell you what it IS, and everything looks intentional.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

REGISTRY_ACCOUNT_KEY="altdig-infra-tooling"
TABLE="${PLATFORM_REGISTRY_TABLE:-platform-registry}"

usage() {
  cat <<'USAGE'
Usage: registry.sh <command> [options]

Partners
  put-partner    --slug S --state STATE --notify-hours N --altdig-hours N
                 [--owner NAME] [--baa STATE]
  get-partner    --slug S
  list-partners
  check-partner  --slug S        Gate used by create-client-ou.sh. Exit 0 only
                                 if the partner may have clients created.

Clients
  put-client     --partner S --slug S [--state STATE] [--ip-owner WHO]
                 [--tech-contact EMAIL] [--dev-manager EMAIL]
  list-clients   [--partner S]

Accounts
  put-account    --account-id ID --partner S --client S --env ENV
                 [--app S] [--baseline-version N]
  get-account    --account-id ID
  list-accounts

Overrides
  put-override   --account-id ID --name NAME --reason TEXT --owner NAME
                 [--review-by YYYY-MM-DD]
  drop-override  --account-id ID --name NAME

Baseline
  set-baseline   --version N     Record the current global baseline version.
  get-baseline

Reports
  report-baseline    Accounts behind the current global baseline version.
  report-overrides   Every declared override, with reason, owner and review date.

STATE is one of: provisioning | active | suspended | devested
USAGE
}

[[ $# -gt 0 ]] || { usage; exit 1; }
CMD="$1"; shift

SLUG=""; PARTNER=""; STATE=""; NOTIFY_HOURS=""; ALTDIG_HOURS=""; OWNER=""
BAA=""; IP_OWNER=""; ACCOUNT_ID=""; ENVIRONMENT=""; APP=""; VERSION=""
NAME=""; REASON=""; REVIEW_BY=""; TECH_CONTACT=""; DEV_MANAGER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)             SLUG="$2"; shift 2 ;;
    --partner)          PARTNER="$2"; shift 2 ;;
    --state)            STATE="$2"; shift 2 ;;
    --notify-hours)     NOTIFY_HOURS="$2"; shift 2 ;;
    --altdig-hours)     ALTDIG_HOURS="$2"; shift 2 ;;
    --owner)            OWNER="$2"; shift 2 ;;
    --baa)              BAA="$2"; shift 2 ;;
    --ip-owner)         IP_OWNER="$2"; shift 2 ;;
    --tech-contact)     TECH_CONTACT="$2"; shift 2 ;;
    --dev-manager)      DEV_MANAGER="$2"; shift 2 ;;
    --account-id)       ACCOUNT_ID="$2"; shift 2 ;;
    --env)              ENVIRONMENT="$2"; shift 2 ;;
    --app)              APP="$2"; shift 2 ;;
    --baseline-version) VERSION="$2"; shift 2 ;;
    --version)          VERSION="$2"; shift 2 ;;
    --name)             NAME="$2"; shift 2 ;;
    --reason)           REASON="$2"; shift 2 ;;
    --review-by)        REVIEW_BY="$2"; shift 2 ;;
    --dry-run)          DRY_RUN=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# Version padding
# ---------------------------------------------------------------------------
# baseline_version is the GSI sort key, and DynamoDB sorts string sort keys
# lexicographically. Unpadded, "10" sorts before "9" and report-baseline starts
# quietly lying at version 10 — reporting accounts as up to date when they are
# eight versions behind. Padded to six digits here, in the one place that
# writes it.
pad_version() {
  local v="$1"
  [[ "${v}" =~ ^[0-9]+$ ]] || die "Baseline version must be a non-negative integer, got '${v}'."
  printf '%06d' "${v}"
}

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
ddb_init() {
  export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
  require_cli
  require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

  REGISTRY_ACCOUNT="$(get_param "/org/account/${REGISTRY_ACCOUNT_KEY}")"
  [[ -n "${REGISTRY_ACCOUNT}" && "${REGISTRY_ACCOUNT}" != "None" ]] \
    || die "No account registered at ${PLATFORM_SSM_PREFIX}/org/account/${REGISTRY_ACCOUNT_KEY}.
      The registry lives in the Platform Tooling account. Create it with:
        scripts/create-platform-account.sh --ou infra --role tooling"

  local creds
  creds="$(aws sts assume-role \
    --role-arn "arn:aws:iam::${REGISTRY_ACCOUNT}:role/OrganizationAccountAccessRole" \
    --role-session-name platform-registry \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${creds}" ]] || die "Could not assume into ${REGISTRY_ACCOUNT}."
  local a s t; read -r a s t <<<"${creds}"
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${a}" AWS_SECRET_ACCESS_KEY="${s}" AWS_SESSION_TOKEN="${t}"
}

ddb() { MSYS_NO_PATHCONV=1 aws dynamodb "$@"; }

# ddb_str <value> -> a complete DynamoDB attribute value: {"S": "escaped"}
#
# The type wrapper is part of this, not the caller's job. The first version
# returned just the escaped string and every free-text field failed with
# "Invalid type for parameter Item.owner ... valid types: <class 'dict'>",
# which names the symptom and not the missing two characters.
#
# Escaping matters because reasons and owner names are free text and will
# eventually contain a quote, a backslash or a newline. Hand-rolled quoting
# would produce malformed JSON at exactly the moment someone writes a properly
# detailed override reason.
ddb_str() {
  printf '%s' "$1" | python -c 'import json,sys; print(json.dumps({"S": sys.stdin.read()}))'
}

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

VALID_STATES="provisioning active suspended devested"
check_state() {
  case " ${VALID_STATES} " in
    *" $1 "*) ;;
    *) die "Invalid state '$1'. Expected one of: ${VALID_STATES}" ;;
  esac
}

put_item() {
  local item="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  put-item %s\n' "${C_YELLOW}" "${C_RESET}" "${item}"
    return 0
  fi
  ddb put-item --table-name "${TABLE}" --item "${item}" >/dev/null
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_put_partner() {
  [[ -n "${SLUG}" ]]         || die "--slug is required."
  [[ -n "${STATE}" ]]        || die "--state is required."
  [[ -n "${NOTIFY_HOURS}" ]] || die "--notify-hours is required (partner's incident notification window)."
  [[ -n "${ALTDIG_HOURS}" ]] || die "--altdig-hours is required (AltDigital's window to the partner)."
  check_state "${STATE}"
  validate_slug "partner" "${SLUG}"

  [[ "${NOTIFY_HOURS}" =~ ^[0-9]+$ ]] || die "--notify-hours must be an integer."
  [[ "${ALTDIG_HOURS}" =~ ^[0-9]+$ ]] || die "--altdig-hours must be an integer."

  # AltDigital's window must be TIGHTER than the partner's, not merely present.
  #
  # The partner has promised their client notification within N hours. If
  # AltDigital tells the partner at N, the partner is already late — they have
  # zero time to act. The whole chain only holds if each link is shorter than
  # the one above it, and equal windows fail in exactly the same way as a
  # longer one while looking reasonable in a table.
  if [[ ${ALTDIG_HOURS} -ge ${NOTIFY_HOURS} ]]; then
    die "AltDigital's notification window (${ALTDIG_HOURS}h) must be STRICTLY
      TIGHTER than the partner's (${NOTIFY_HOURS}h).

      The partner owes their client notification within ${NOTIFY_HOURS}h. If we
      tell the partner at ${ALTDIG_HOURS}h they have no time left to act, so the
      chain cannot be met. Equal windows fail identically to longer ones."
  fi

  ddb_init
  put_item "$(cat <<JSON
{
  "PK":               {"S": "PARTNER#${SLUG}"},
  "SK":               {"S": "META"},
  "record_type":      {"S": "PARTNER"},
  "baseline_version": {"S": "000000"},
  "slug":             {"S": "${SLUG}"},
  "state":            {"S": "${STATE}"},
  "notify_hours":     {"N": "${NOTIFY_HOURS}"},
  "altdig_hours":     {"N": "${ALTDIG_HOURS}"},
  "baa_state":        {"S": "${BAA:-unknown}"},
  "owner":            $(ddb_str "${OWNER:-unassigned}"),
  "updated_at":       {"S": "$(now_iso)"}
}
JSON
)"
  ok "partner ${SLUG} recorded: state=${STATE} notify=${NOTIFY_HOURS}h altdig=${ALTDIG_HOURS}h baa=${BAA:-unknown}"
}

cmd_get_partner() {
  [[ -n "${SLUG}" ]] || die "--slug is required."
  ddb_init
  ddb get-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${SLUG}\"},\"SK\":{\"S\":\"META\"}}" \
    --query 'Item' --output json
}

cmd_list_partners() {
  ddb_init
  ddb query --table-name "${TABLE}" --index-name by-type \
    --key-condition-expression 'record_type = :t' \
    --expression-attribute-values '{":t":{"S":"PARTNER"}}' \
    --query 'Items[].[slug.S,state.S,notify_hours.N,altdig_hours.N,baa_state.S,owner.S]' \
    --output text | no_cr | sed 's/^/  /'
}

# The D-004 gate. Exits non-zero, with a specific reason, if a partner is not
# in a state where clients may be created beneath it.
cmd_check_partner() {
  [[ -n "${SLUG}" ]] || die "--slug is required."
  ddb_init
  local item
  item="$(ddb get-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${SLUG}\"},\"SK\":{\"S\":\"META\"}}" \
    --query 'Item' --output json 2>/dev/null)" || true

  if [[ -z "${item}" || "${item}" == "null" ]]; then
    die "Partner '${SLUG}' has no registry record.

      Design doc 13 requires the partner to exist, be active, and carry a
      non-null notification window before any client is created beneath it.
      Record it first:

        scripts/registry.sh put-partner --slug ${SLUG} --state active \\
          --notify-hours 48 --altdig-hours 24 --owner '<name>'"
  fi

  local st nh ah
  st="$(printf '%s' "${item}" | python -c 'import json,sys; print(json.load(sys.stdin)["state"]["S"])')"
  nh="$(printf '%s' "${item}" | python -c 'import json,sys; print(json.load(sys.stdin)["notify_hours"]["N"])')"
  ah="$(printf '%s' "${item}" | python -c 'import json,sys; print(json.load(sys.stdin)["altdig_hours"]["N"])')"

  [[ "${st}" == "active" ]] \
    || die "Partner '${SLUG}' is '${st}', not 'active'. No clients may be created beneath it."
  [[ "${nh}" -gt 0 ]] \
    || die "Partner '${SLUG}' has no notification window recorded."
  [[ "${ah}" -gt 0 && "${ah}" -lt "${nh}" ]] \
    || die "Partner '${SLUG}': AltDigital's window (${ah}h) is not tighter than the partner's (${nh}h)."

  ok "partner ${SLUG}: active, notify=${nh}h, altdigital=${ah}h (tighter) — clients may be created"
}

cmd_put_client() {
  [[ -n "${PARTNER}" ]] || die "--partner is required."
  [[ -n "${SLUG}" ]]    || die "--slug is required."
  validate_slug "partner" "${PARTNER}"
  validate_slug "client" "${SLUG}"
  check_state "${STATE:=provisioning}"
  ddb_init
  put_item "$(cat <<JSON
{
  "PK":               {"S": "PARTNER#${PARTNER}"},
  "SK":               {"S": "CLIENT#${SLUG}"},
  "record_type":      {"S": "CLIENT"},
  "baseline_version": {"S": "000000"},
  "partner":          {"S": "${PARTNER}"},
  "slug":             {"S": "${SLUG}"},
  "state":            {"S": "${STATE}"},
  "ip_owner":         $(ddb_str "${IP_OWNER:-unrecorded}"),
  "tech_contact":     $(ddb_str "${TECH_CONTACT}"),
  "dev_manager":      $(ddb_str "${DEV_MANAGER}"),
  "updated_at":       {"S": "$(now_iso)"}
}
JSON
)"
  ok "client ${PARTNER}/${SLUG} recorded: state=${STATE} ip-owner=${IP_OWNER:-unrecorded}"
  # Questionnaire 1.5 and 5.8. These are the ONLY personal data in the
  # registry, and they are here because the instrumentation digest has nowhere
  # else to find a recipient — design doc 06 makes that digest the documented
  # evidence that monitoring requirements were communicated, so a digest with
  # no addressee is a compliance gap rather than a missing nicety.
  #
  # DynamoDB, not the repository. Same rule as config/contacts.env: personal
  # data lives in a system with access control and an audit trail, never in
  # git.
  if [[ -z "${TECH_CONTACT}" ]]; then
    warn "No --tech-contact. The instrumentation digest for this client has no
      recipient and will be skipped, which loses the evidence that monitoring
      requirements were communicated. Questionnaire field 1.5."
  fi
}

cmd_list_clients() {
  ddb_init
  if [[ -n "${PARTNER}" ]]; then
    ddb query --table-name "${TABLE}" \
      --key-condition-expression 'PK = :p AND begins_with(SK, :c)' \
      --expression-attribute-values "{\":p\":{\"S\":\"PARTNER#${PARTNER}\"},\":c\":{\"S\":\"CLIENT#\"}}" \
      --query 'Items[].[partner.S,slug.S,state.S,ip_owner.S]' --output text | no_cr | sed 's/^/  /'
  else
    ddb query --table-name "${TABLE}" --index-name by-type \
      --key-condition-expression 'record_type = :t' \
      --expression-attribute-values '{":t":{"S":"CLIENT"}}' \
      --query 'Items[].[partner.S,slug.S,state.S,ip_owner.S]' --output text | no_cr | sed 's/^/  /'
  fi
}

cmd_put_account() {
  [[ -n "${ACCOUNT_ID}" ]]  || die "--account-id is required."
  [[ -n "${PARTNER}" ]]     || die "--partner is required."
  [[ -n "${SLUG}" ]]        || die "--client is required (pass with --slug)."
  [[ -n "${ENVIRONMENT}" ]] || die "--env is required."
  [[ "${ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] || die "--account-id must be 12 digits."
  check_state "${STATE:=provisioning}"
  local padded; padded="$(pad_version "${VERSION:-0}")"
  ddb_init
  put_item "$(cat <<JSON
{
  "PK":               {"S": "ACCOUNT#${ACCOUNT_ID}"},
  "SK":               {"S": "META"},
  "record_type":      {"S": "ACCOUNT"},
  "baseline_version": {"S": "${padded}"},
  "account_id":       {"S": "${ACCOUNT_ID}"},
  "partner":          {"S": "${PARTNER}"},
  "client":           {"S": "${SLUG}"},
  "app":              {"S": "${APP:-}"},
  "environment":      {"S": "${ENVIRONMENT}"},
  "state":            {"S": "${STATE}"},
  "updated_at":       {"S": "$(now_iso)"}
}
JSON
)"
  ok "account ${ACCOUNT_ID} recorded: ${PARTNER}/${SLUG}${APP:+/${APP}} ${ENVIRONMENT} baseline=${padded}"
}

cmd_get_account() {
  [[ -n "${ACCOUNT_ID}" ]] || die "--account-id is required."
  ddb_init
  ddb query --table-name "${TABLE}" \
    --key-condition-expression 'PK = :a' \
    --expression-attribute-values "{\":a\":{\"S\":\"ACCOUNT#${ACCOUNT_ID}\"}}" \
    --query 'Items' --output json
}

cmd_list_accounts() {
  ddb_init
  ddb query --table-name "${TABLE}" --index-name by-type \
    --key-condition-expression 'record_type = :t' \
    --expression-attribute-values '{":t":{"S":"ACCOUNT"}}' \
    --query 'Items[].[account_id.S,partner.S,client.S,environment.S,state.S,baseline_version.S]' \
    --output text | no_cr | sed 's/^/  /'
}

cmd_put_override() {
  [[ -n "${ACCOUNT_ID}" ]] || die "--account-id is required."
  [[ -n "${NAME}" ]]       || die "--name is required."
  [[ -n "${REASON}" ]]     || die "--reason is required. An override without a reason is drift with paperwork."
  [[ -n "${OWNER}" ]]      || die "--owner is required. Someone must be accountable for every difference."
  ddb_init
  put_item "$(cat <<JSON
{
  "PK":          {"S": "ACCOUNT#${ACCOUNT_ID}"},
  "SK":          {"S": "OVERRIDE#${NAME}"},
  "record_type": {"S": "OVERRIDE"},
  "baseline_version": {"S": "000000"},
  "account_id":  {"S": "${ACCOUNT_ID}"},
  "name":        {"S": "${NAME}"},
  "reason":      $(ddb_str "${REASON}"),
  "owner":       $(ddb_str "${OWNER}"),
  "review_by":   {"S": "${REVIEW_BY:-unset}"},
  "updated_at":  {"S": "$(now_iso)"}
}
JSON
)"
  ok "override ${NAME} on ${ACCOUNT_ID} declared, owned by ${OWNER}"
  [[ -n "${REVIEW_BY}" ]] || warn "No --review-by date. An override with no review date is permanent by default."
}

cmd_drop_override() {
  [[ -n "${ACCOUNT_ID}" ]] || die "--account-id is required."
  [[ -n "${NAME}" ]]       || die "--name is required."
  ddb_init
  ddb delete-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"ACCOUNT#${ACCOUNT_ID}\"},\"SK\":{\"S\":\"OVERRIDE#${NAME}\"}}"
  ok "override ${NAME} on ${ACCOUNT_ID} withdrawn"
  warn "The account's actual configuration has NOT changed. Withdrawing the
      declaration turns a declared override into undeclared difference, which
      is drift. Reconcile the account, or this will surface as a finding."
}

cmd_set_baseline() {
  [[ -n "${VERSION}" ]] || die "--version is required."
  local padded; padded="$(pad_version "${VERSION}")"
  ddb_init
  put_item "$(cat <<JSON
{
  "PK":               {"S": "CONFIG"},
  "SK":               {"S": "BASELINE"},
  "record_type":      {"S": "CONFIG"},
  "baseline_version": {"S": "${padded}"},
  "updated_at":       {"S": "$(now_iso)"}
}
JSON
)"
  ok "current global baseline version = ${padded}"
}

current_baseline() {
  ddb get-item --table-name "${TABLE}" \
    --key '{"PK":{"S":"CONFIG"},"SK":{"S":"BASELINE"}}' \
    --query 'Item.baseline_version.S' --output text 2>/dev/null | no_cr
}

cmd_get_baseline() {
  ddb_init
  local v; v="$(current_baseline)"
  [[ -n "${v}" && "${v}" != "None" ]] \
    || die "No baseline version recorded. Set one: scripts/registry.sh set-baseline --version 1"
  log "${v}"
}

# Report 1 — accounts behind the current global baseline version.
cmd_report_baseline() {
  ddb_init
  local cur; cur="$(current_baseline)"
  [[ -n "${cur}" && "${cur}" != "None" ]] \
    || die "No baseline version recorded. Set one: scripts/registry.sh set-baseline --version 1"

  hr
  log "Accounts behind the current baseline"
  log "  current version : ${cur}"
  hr

  # Range query on the GSI sort key. This is the reason baseline_version is
  # zero-padded: an unpadded "9" would sort ABOVE "10" and be reported as up to
  # date.
  local out
  out="$(ddb query --table-name "${TABLE}" --index-name by-type \
    --key-condition-expression 'record_type = :t AND baseline_version < :v' \
    --expression-attribute-values "{\":t\":{\"S\":\"ACCOUNT\"},\":v\":{\"S\":\"${cur}\"}}" \
    --query 'Items[].[account_id.S,partner.S,client.S,environment.S,baseline_version.S]' \
    --output text | no_cr)"

  if [[ -z "${out}" ]]; then
    ok "every account is at ${cur}"
  else
    printf '%s\n' "${out}" | sed 's/^/  /'
    printf '\n'
    warn "$(printf '%s\n' "${out}" | wc -l | tr -d ' ') account(s) behind ${cur}"
  fi
  hr
}

# Report 2 — declared local overrides: what, why, who owns each.
cmd_report_overrides() {
  ddb_init
  hr
  log "Declared local overrides"
  hr
  local out
  out="$(ddb query --table-name "${TABLE}" --index-name by-type \
    --key-condition-expression 'record_type = :t' \
    --expression-attribute-values '{":t":{"S":"OVERRIDE"}}' \
    --query 'Items[].[account_id.S,name.S,owner.S,review_by.S,reason.S]' \
    --output text | no_cr)"

  if [[ -z "${out}" ]]; then
    ok "no declared overrides"
    log ""
    log "  Note that this is NOT the same as 'no differences'. It means nobody"
    log "  has declared one. An undeclared difference is drift, and this report"
    log "  cannot see it — comparing recorded parameters against actual AWS"
    log "  configuration is a separate job. See B-022."
  else
    printf '%s\n' "${out}" | sed 's/^/  /'
    printf '\n'
    local stale
    stale="$(printf '%s\n' "${out}" | awk -F'\t' -v today="$(date -u +%Y-%m-%d)" \
      '$4 != "unset" && $4 < today {print}' | wc -l | tr -d ' ')"
    [[ "${stale}" == "0" ]] || warn "${stale} override(s) past their review date"
    local unset_count
    unset_count="$(printf '%s\n' "${out}" | awk -F'\t' '$4 == "unset" {print}' | wc -l | tr -d ' ')"
    [[ "${unset_count}" == "0" ]] || warn "${unset_count} override(s) have no review date and are permanent by default"
  fi
  hr
}

case "${CMD}" in
  put-partner)      cmd_put_partner ;;
  get-partner)      cmd_get_partner ;;
  list-partners)    cmd_list_partners ;;
  check-partner)    cmd_check_partner ;;
  put-client)       cmd_put_client ;;
  list-clients)     cmd_list_clients ;;
  put-account)      cmd_put_account ;;
  get-account)      cmd_get_account ;;
  list-accounts)    cmd_list_accounts ;;
  put-override)     cmd_put_override ;;
  drop-override)    cmd_drop_override ;;
  set-baseline)     cmd_set_baseline ;;
  get-baseline)     cmd_get_baseline ;;
  report-baseline)  cmd_report_baseline ;;
  report-overrides) cmd_report_overrides ;;
  -h|--help|help)   usage ;;
  *) usage; die "Unknown command: ${CMD}" ;;
esac
