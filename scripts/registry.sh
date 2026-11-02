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
                 [--downstream-sla PCT] [--altdig-sla PCT]
                 [--owner NAME] [--baa STATE]
  get-partner    --slug S
  list-partners
  check-partner  --slug S        Gate used by create-client-ou.sh. Exit 0 only
                                 if the partner may have clients created.

Clients
  put-client     --partner S --slug S [--state STATE] [--ip-owner WHO]
                 [--tech-contact EMAIL] [--dev-manager EMAIL]
                 [--change-approver EMAIL] [--jira-project KEY] [--jira-site URL]
                 [--evidence-destination truveon|external|archive]
                 [--breach-contact EMAIL] [--security-contact EMAIL]
                 [--timezone IANA_ZONE]
                 Updates only the fields passed; never erases the rest.
  get-client     --partner S --slug S
  set-budget     --partner S --slug S --app S [--pre-production USD]
                 [--production USD|deferred]
                 Questionnaire 13.2a/b (ADR-023). Touches only the budget
                 attributes; never state. Amounts are never printed.
  link-truveon   --partner S --slug S --name TRUVEON_TENANT_SLUG
                 One Truveon tenant per client (B-023).
  list-clients   [--partner S]

Accounts
  put-account    --account-id ID --partner S --client S --env ENV
                 [--app S] [--baseline-version N]
                 [--service-model A|B|C] [--compliance hipaa_soc2]
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
CHANGE_APPROVER=""; JIRA_PROJECT=""; SERVICE_MODEL=""; COMPLIANCE=""; KIND=""
BREACH_CONTACT=""; SECURITY_CONTACT=""; JIRA_SITE_URL=""; EVIDENCE_DESTINATION=""
TIMEZONE=""; BUDGET_PRE=""; BUDGET_PROD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)             SLUG="$2"; shift 2 ;;
    --partner)          PARTNER="$2"; shift 2 ;;
    --state)            STATE="$2"; shift 2 ;;
    --notify-hours)     NOTIFY_HOURS="$2"; shift 2 ;;
    --altdig-hours)     ALTDIG_HOURS="$2"; shift 2 ;;
    --downstream-sla)   DOWNSTREAM_SLA="$2"; shift 2 ;;
    --altdig-sla)       ALTDIG_SLA="$2"; shift 2 ;;
    --owner)            OWNER="$2"; shift 2 ;;
    --baa)              BAA="$2"; shift 2 ;;
    --ip-owner)         IP_OWNER="$2"; shift 2 ;;
    --tech-contact)     TECH_CONTACT="$2"; shift 2 ;;
    --dev-manager)      DEV_MANAGER="$2"; shift 2 ;;
    --change-approver)  CHANGE_APPROVER="$2"; shift 2 ;;
    --jira-project)     JIRA_PROJECT="$2"; shift 2 ;;
    --jira-site)        JIRA_SITE_URL="$2"; shift 2 ;;
    --evidence-destination) EVIDENCE_DESTINATION="$2"; shift 2 ;;
    --breach-contact)   BREACH_CONTACT="$2"; shift 2 ;;
    --security-contact) SECURITY_CONTACT="$2"; shift 2 ;;
    --timezone)         TIMEZONE="$2"; shift 2 ;;
    --pre-production)   BUDGET_PRE="$2"; shift 2 ;;
    --production)       BUDGET_PROD="$2"; shift 2 ;;
    --account-id)       ACCOUNT_ID="$2"; shift 2 ;;
    --env)              ENVIRONMENT="$2"; shift 2 ;;
    --app)              APP="$2"; shift 2 ;;
    --service-model)    SERVICE_MODEL="$2"; shift 2 ;;
    --compliance)       COMPLIANCE="$2"; shift 2 ;;
    --kind)             KIND="$2"; shift 2 ;;
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

  # downstream_sla_pct / altdig_sla_pct: design 15's back-to-back pair.
  # Captured at partner onboarding precisely so AltDigital's number can be
  # compared with the partner's BEFORE signing -- partners/src/backtoback.py
  # does the comparison. Absent is a finding, not a default: an unrecorded
  # downstream commitment cannot be checked against.
  #
  # This comment lives HERE, not inside the heredoc. It used to sit between the
  # JSON fields, where a heredoc passes it through verbatim, so every
  # put-partner sent invalid JSON and failed. Found 2026-09-30 recording the
  # first partner; no partner had ever been written.
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
  "downstream_sla_pct": $(ddb_str "${DOWNSTREAM_SLA:-}"),
  "altdig_sla_pct":     $(ddb_str "${ALTDIG_SLA:-}"),
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

# update-item, not put-item, and only the fields actually passed.
#
# put-item replaced the whole record, and onboard-client.sh step 1 calls this on
# every run and every --from resume. So a resume silently erased anything else
# written to the record since -- the Truveon link (link-truveon, below), and the
# tech contact and dev manager, which step 1 never passes and which were
# overwritten with blanks each time. A record that forgets on re-run is not a
# spine.
cmd_set_budget() {
  # Questionnaire 13.2a and 13.2b, per application (ADR-023). The registry has
  # no application record, so budgets sit on the client record, one attribute
  # per application and tier group: budget#<app>#pre_production and
  # budget#<app>#production. Read by scripts/sync-budgets.sh.
  #
  # A command of its own, not put-client flags: put-client always writes
  # state (defaulting to provisioning), so recording a budget through it would
  # quietly demote a live client. This writes the budget attributes and
  # updated_at, and requires the client to exist already.
  #
  # Amounts are client commercial data. They are held here, never printed by
  # this script, and never written to the repository.
  [[ -n "${PARTNER}" ]] || die "--partner is required."
  [[ -n "${SLUG}" ]]    || die "--slug is required."
  [[ -n "${APP}" ]]     || die "--app is required."
  validate_slug "partner" "${PARTNER}"
  validate_slug "client" "${SLUG}"
  validate_app_slug "${APP}"
  [[ -n "${BUDGET_PRE}" || -n "${BUDGET_PROD}" ]] \
    || die "Give --pre-production, --production, or both."

  local set="updated_at = :u" vals names="{"
  vals="\":u\":{\"S\":\"$(now_iso)\"}"
  if [[ -n "${BUDGET_PRE}" ]]; then
    [[ "${BUDGET_PRE}" =~ ^[0-9]+(\.[0-9]{1,2})?$ && ! "${BUDGET_PRE}" =~ ^0+(\.0+)?$ ]] \
      || die "--pre-production must be a positive amount in US dollars."
    set+=", #bpre = :bpre"; vals+=",\":bpre\":{\"N\":\"${BUDGET_PRE}\"}"
    names+="\"#bpre\":\"budget#${APP}#pre_production\","
  fi
  if [[ -n "${BUDGET_PROD}" ]]; then
    if [[ "${BUDGET_PROD}" == "deferred" ]]; then
      vals+=",\":bprod\":{\"S\":\"deferred\"}"
    else
      [[ "${BUDGET_PROD}" =~ ^[0-9]+(\.[0-9]{1,2})?$ && ! "${BUDGET_PROD}" =~ ^0+(\.0+)?$ ]] \
        || die "--production must be a positive amount in US dollars, or deferred."
      vals+=",\":bprod\":{\"N\":\"${BUDGET_PROD}\"}"
    fi
    set+=", #bprod = :bprod"
    names+="\"#bprod\":\"budget#${APP}#production\","
  fi
  names="${names%,}}"

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  update-item CLIENT %s/%s budgets for %s:%s%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${SLUG}" "${APP}" \
      "${BUDGET_PRE:+ pre-production}" "${BUDGET_PROD:+ production}"
    return 0
  fi
  ddb_init
  ddb update-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${PARTNER}\"},\"SK\":{\"S\":\"CLIENT#${SLUG}\"}}" \
    --condition-expression "attribute_exists(PK)" \
    --update-expression "SET ${set}" \
    --expression-attribute-names "${names}" \
    --expression-attribute-values "{${vals}}" >/dev/null \
    || die "Could not record the budget. Does client ${PARTNER}/${SLUG} exist?"
  ok "budgets recorded for ${PARTNER}/${SLUG}/${APP}:${BUDGET_PRE:+ pre-production}${BUDGET_PROD:+ production$([[ "${BUDGET_PROD}" == deferred ]] && echo ' (deferred)')}"
}

cmd_put_client() {
  [[ -n "${PARTNER}" ]] || die "--partner is required."
  [[ -n "${SLUG}" ]]    || die "--slug is required."
  validate_slug "partner" "${PARTNER}"
  validate_slug "client" "${SLUG}"
  check_state "${STATE:=provisioning}"

  local set="record_type = :rt, baseline_version = if_not_exists(baseline_version, :bv), partner = :p, slug = :s, #st = :st, updated_at = :u"
  local vals
  vals="\":rt\":{\"S\":\"CLIENT\"},\":bv\":{\"S\":\"000000\"},\":p\":{\"S\":\"${PARTNER}\"},\":s\":{\"S\":\"${SLUG}\"},\":st\":{\"S\":\"${STATE}\"},\":u\":{\"S\":\"$(now_iso)\"}"
  if [[ -n "${IP_OWNER}" ]]; then
    set+=", ip_owner = :ip";  vals+=",\":ip\":$(ddb_str "${IP_OWNER}")"
  else
    # First write records 'unrecorded' so the gap is findable; later writes
    # without --ip-owner leave whatever is there.
    set+=", ip_owner = if_not_exists(ip_owner, :ip)"; vals+=",\":ip\":{\"S\":\"unrecorded\"}"
  fi
  if [[ -n "${TECH_CONTACT}" ]]; then
    set+=", tech_contact = :tc"; vals+=",\":tc\":$(ddb_str "${TECH_CONTACT}")"
  fi
  if [[ -n "${DEV_MANAGER}" ]]; then
    set+=", dev_manager = :dm";  vals+=",\":dm\":$(ddb_str "${DEV_MANAGER}")"
  fi
  # Questionnaire 3.3 and 12.4. Change reconciliation routes an unapproved
  # deployment to the change approver and looks for change records in the
  # Jira project, both read from this record by sync-tenant-facts.sh. Until
  # these flags existed nothing wrote either field, so reconciliation had a
  # consumer and no supplier.
  if [[ -n "${CHANGE_APPROVER}" ]]; then
    set+=", change_approver = :ca"; vals+=",\":ca\":$(ddb_str "${CHANGE_APPROVER}")"
  fi
  if [[ -n "${JIRA_PROJECT}" ]]; then
    set+=", jira_change_project = :jp"; vals+=",\":jp\":$(ddb_str "${JIRA_PROJECT}")"
  fi
  # The tenant's own Atlassian site, when its change records live there rather
  # than on AltDigital's. Absent means AltDigital's site. Its credential is
  # platform/jira/tenant/<partner>-<client>, by convention, in the Audit account.
  if [[ -n "${JIRA_SITE_URL}" ]]; then
    [[ "${JIRA_SITE_URL}" =~ ^https://[a-z0-9-]+\.atlassian\.net/?$ ]]       || die "--jira-site must be an Atlassian Cloud site, https://<name>.atlassian.net"
    set+=", jira_site = :js"; vals+=",\":js\":$(ddb_str "${JIRA_SITE_URL%/}")"
  fi
  # D-029. Where this client's APPLICATION-layer evidence goes. Infrastructure
  # evidence goes to AltDigital's tenant regardless, so this is the client's
  # choice and nothing else. Read by the evidence tenant map (B-157 phase C).
  if [[ -n "${EVIDENCE_DESTINATION}" ]]; then
    [[ "${EVIDENCE_DESTINATION}" =~ ^(truveon|external|archive)$ ]]       || die "--evidence-destination must be truveon, external or archive (D-029)."
    set+=", evidence_destination = :ed"; vals+=",\":ed\":$(ddb_str "${EVIDENCE_DESTINATION}")"
  fi
  # Breach notification of record, and day-to-day security operations. Two
  # fields because they are different people more often than not: the first
  # must be able to act for the organisation legally, the second may be a
  # delegate. Nothing reads them yet -- the incident clock records who was
  # notified rather than sending -- but a recipient nobody wrote down cannot
  # be notified by anything later. Questionnaire identity.*, 2026-10-01.
  if [[ -n "${BREACH_CONTACT}" ]]; then
    set+=", breach_contact = :bc"; vals+=",\":bc\":$(ddb_str "${BREACH_CONTACT}")"
  fi
  if [[ -n "${SECURITY_CONTACT}" ]]; then
    set+=", security_contact = :sc"; vals+=",\":sc\":$(ddb_str "${SECURITY_CONTACT}")"
  fi
  # The zone the development manager reads the daily digest in
  # (docs/digest-design.md §5). The READER's zone, which is not necessarily
  # questionnaire 5.2's: that one is the developers' schedule default, and a
  # manager may sit elsewhere. Absent means America/Denver. Checked for shape
  # only; a name that does not resolve renders the digest in UTC with a line
  # saying so, rather than failing the send.
  #
  # `timezone` is a DynamoDB reserved word, so it goes through a name
  # placeholder -- added only when used, because DynamoDB rejects a
  # placeholder the expression does not reference.
  local names='{"#st":"state"}'
  if [[ -n "${TIMEZONE}" ]]; then
    [[ "${TIMEZONE}" == "UTC" || "${TIMEZONE}" =~ ^[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+)+$ ]] \
      || die "--timezone must be an IANA zone name such as America/Denver or Asia/Kolkata."
    set+=", #tz = :tz"; vals+=",\":tz\":$(ddb_str "${TIMEZONE}")"
    names='{"#st":"state","#tz":"timezone"}'
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  update-item CLIENT %s/%s SET %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${SLUG}" "${set}"
    return 0
  fi
  ddb_init
  ddb update-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${PARTNER}\"},\"SK\":{\"S\":\"CLIENT#${SLUG}\"}}" \
    --update-expression "SET ${set}" \
    --expression-attribute-names "${names}" \
    --expression-attribute-values "{${vals}}" >/dev/null
  ok "client ${PARTNER}/${SLUG} recorded: state=${STATE}${IP_OWNER:+ ip-owner=${IP_OWNER}}"
  # Questionnaire 1.5, 5.8 and 3.3: the tech contact, dev manager and change
  # approver. These are the ONLY personal data in the registry, and they are
  # here because the digest and change reconciliation have nowhere else to
  # find a recipient — design doc 06 makes that digest the documented
  # evidence that monitoring requirements were communicated, so a digest with
  # no addressee is a compliance gap rather than a missing nicety.
  #
  # DynamoDB, not the repository. Same rule as config/contacts.env: personal
  # data lives in a system with access control and an audit trail, never in
  # git.
  if [[ -z "${TECH_CONTACT}" ]]; then
    warn "No --tech-contact passed; any value already on the record is kept.
      If there is none, the instrumentation digest for this client has no
      recipient and will be skipped, which loses the evidence that monitoring
      requirements were communicated. Questionnaire field 1.5."
  fi
}

cmd_get_client() {
  [[ -n "${PARTNER}" ]] || die "--partner is required."
  [[ -n "${SLUG}" ]]    || die "--slug is required."
  # stdout is the item and nothing else: callers parse it as JSON. ddb_init's
  # "Account confirmed" line used to precede it, the parse failed, and
  # truveon-tenant-handoff.sh status reported every linked client as NOT linked.
  ddb_init >&2
  ddb get-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${PARTNER}\"},\"SK\":{\"S\":\"CLIENT#${SLUG}\"}}" \
    --query 'Item' --output json
}

# The client's Truveon tenant. One per CLIENT, not per application (B-023,
# decided 2026-09-23): frameworks and BAAs are signed by the organisation, and
# design 01 and 04 both say "member organisation". Applications are modelled
# inside the tenant.
#
# Conditional on the client record existing. Linking a Truveon tenant to a
# client the registry has never heard of would create a half-record that
# list-clients cannot show and put-client would then have to guess around.
cmd_link_truveon() {
  [[ -n "${PARTNER}" ]] || die "--partner is required."
  [[ -n "${SLUG}" ]]    || die "--slug is required."
  [[ -n "${NAME}" ]]    || die "--name <truveon-tenant-slug> is required."
  [[ "${NAME}" =~ ^[a-z][a-z0-9-]{1,62}$ ]] \
    || die "Truveon tenant slug '${NAME}' is not a valid slug (lowercase, digits, hyphens)."
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  update-item CLIENT %s/%s SET truveon_tenant_id=%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${SLUG}" "${NAME}"
    return 0
  fi
  ddb_init
  ddb update-item --table-name "${TABLE}" \
    --key "{\"PK\":{\"S\":\"PARTNER#${PARTNER}\"},\"SK\":{\"S\":\"CLIENT#${SLUG}\"}}" \
    --update-expression "SET truveon_tenant_id = :t, truveon_linked_at = :u, updated_at = :u" \
    --condition-expression "attribute_exists(PK)" \
    --expression-attribute-values "{\":t\":{\"S\":\"${NAME}\"},\":u\":{\"S\":\"$(now_iso)\"}}" \
    >/dev/null 2>&1 \
    || die "Could not link: client ${PARTNER}/${SLUG} has no registry record (or the write failed).
      Record the client first: scripts/registry.sh put-client --partner ${PARTNER} --slug ${SLUG}"
  ok "client ${PARTNER}/${SLUG} linked to Truveon tenant '${NAME}'"
}

cmd_list_clients() {
  ddb_init
  if [[ -n "${PARTNER}" ]]; then
    ddb query --table-name "${TABLE}" \
      --key-condition-expression 'PK = :p AND begins_with(SK, :c)' \
      --expression-attribute-values "{\":p\":{\"S\":\"PARTNER#${PARTNER}\"},\":c\":{\"S\":\"CLIENT#\"}}" \
      --query 'Items[].[partner.S,slug.S,state.S,ip_owner.S,truveon_tenant_id.S]' --output text | no_cr | sed 's/^/  /'
  else
    ddb query --table-name "${TABLE}" --index-name by-type \
      --key-condition-expression 'record_type = :t' \
      --expression-attribute-values '{":t":{"S":"CLIENT"}}' \
      --query 'Items[].[partner.S,slug.S,state.S,ip_owner.S,truveon_tenant_id.S]' --output text | no_cr | sed 's/^/  /'
  fi
}

# Two facts design 10 needs and this record did not carry, plus the one that
# tells them apart.
#
# service_model and compliance_scope drive Cost Categories (prompt 9.2) and
# were simply absent: the record had partner, client, app and environment.
#
# `kind` matters more than it looks. design 10:
#
#     Log Archive, Audit, Forensics, Platform Tooling and Shared Services
#     accounts generate real cost that nobody pays a flat fee for.
#
# Those accounts have no tenant and never will. Without a way to say so they
# would either be left out of the registry -- which is how B-064 lost an
# account and B-086 swept an empty estate -- or filed against a fictional
# tenant, which would put AltDigital's own overhead on somebody's invoice.
#
#   tenant   a client workload account. Billable.
#   shared   AltDigital's own. Cost is absorbed into the uplift, not allocated,
#            and B-090 is the report that checks the uplift covers it.
check_kind() {
  case "${1}" in
    tenant|shared) ;;
    *) die "--kind must be tenant or shared, got '${1}'." ;;
  esac
}

# design 10's service models, in the questionnaire's own letters (1.7): A
# infrastructure, B managed platform, C fully managed. The same letters
# Truveon uses and the partner page's dropdown will show, so a value never
# needs translating on its way to an invoice. This used to accept managed,
# co-managed and advisory, which nothing else in the platform used.
#
# Unset is allowed and is visible as 'unset' rather than blank, so an account
# nobody classified is findable instead of looking like a deliberate choice.
check_service_model() {
  case "${1}" in
    unset|A|B|C) ;;
    *) die "--service-model must be A, B or C (questionnaire 1.7), got '${1}'." ;;
  esac
}

cmd_put_account() {
  [[ -n "${ACCOUNT_ID}" ]]  || die "--account-id is required."
  check_kind "${KIND:=tenant}"
  check_service_model "${SERVICE_MODEL:=unset}"
  # Frameworks joined with '_', e.g. hipaa_soc2. The value becomes a Cost
  # Category rule value, which AWS restricts to letters, numbers, spaces, '-'
  # and '_'. The first real vesting wrote 'hipaa+soc2' and the Cost Category
  # update refused it (2026-10-01); checked here so it fails where it is written.
  [[ "${COMPLIANCE:-none}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] \
    || die "--compliance '${COMPLIANCE}' must be lowercase letters, digits, '-' and '_' (e.g. hipaa_soc2)."
  if [[ "${KIND}" == "shared" ]]; then
    # AltDigital's own accounts have no partner and no client. Requiring them
    # would mean inventing values, and an invented partner is one that shows
    # up in a billing roll-up.
    PARTNER="${PARTNER:-altdigital}"
    SLUG="${SLUG:-platform}"
  else
    [[ -n "${PARTNER}" ]]   || die "--partner is required."
    [[ -n "${SLUG}" ]]      || die "--client is required (pass with --slug)."
  fi
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
  "service_model":    {"S": "${SERVICE_MODEL:-unset}"},
  "compliance_scope": {"S": "${COMPLIANCE:-none}"},
  "kind":             {"S": "${KIND:-tenant}"},
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
  get-client)       cmd_get_client ;;
  set-budget)       cmd_set_budget ;;
  link-truveon)     cmd_link_truveon ;;
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
