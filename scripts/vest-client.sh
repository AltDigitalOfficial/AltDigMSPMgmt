#!/usr/bin/env bash
#
# Vest a client: create their member accounts, register them, and record the
# evidence. Prompt 3.1, orchestration half.
#
#   scripts/vest-client.sh --questionnaire path.yaml [--dry-run] [--confirm-vest]
#
# ---------------------------------------------------------------------------
# This is the irreversible one
# ---------------------------------------------------------------------------
# It creates three or four AWS accounts. An AWS account cannot be deleted, only
# closed, and closure suspends it for 90 days. The root email is consumed
# permanently — AWS will never allow it on another account.
#
# So --dry-run prints everything and creates nothing, and a real run needs
# --confirm-vest typed deliberately. There is no flag that does both.
#
# ---------------------------------------------------------------------------
# Order matters, and it is the order in design doc 13
# ---------------------------------------------------------------------------
# "The registry entry is created first, in provisioning state. Nothing else
# happens until it exists."
#
# The reason is recovery. Every later step is idempotent against the registry
# record, so a run that dies halfway can be re-run rather than unpicked. A
# vesting that created two accounts and then failed, with nothing written down,
# would leave two orphan accounts nobody can match to a client — and orphan
# accounts cannot be deleted.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

QUESTIONNAIRE=""; CONFIRM=0; ONLY_ENVS=""

usage() {
  cat <<'USAGE'
Usage: vest-client.sh --questionnaire <path> [--dry-run] [--confirm-vest]
                      [--environments dev,test]

  --questionnaire   Completed questionnaire, YAML. Validated and derived by
                    scripts/derive-vesting.py before anything is created.
  --dry-run         Print every derived value and every resource that would be
                    created. Creates nothing.
  --confirm-vest    Required for a real run. Creates AWS accounts, which cannot
                    be deleted.
  --environments    Vest only these environments, comma-separated, from the
                    set the questionnaire derives. For vesting in stages when
                    the account quota cannot hold the whole set yet. Re-run
                    later without it: accounts that exist are found, not
                    recreated, so the next run creates only what is missing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --questionnaire) QUESTIONNAIRE="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --confirm-vest)  CONFIRM=1; shift ;;
    --defer-truveon)
      die "--defer-truveon was removed by D-029 (2026-10-04). Vesting now checks the
      questionnaire's evidence destination (section 17). A client whose evidence
      goes to Truveon links its tenant first; one whose evidence goes elsewhere
      declares that instead of deferring." ;;
    --environments)  ONLY_ENVS="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${QUESTIONNAIRE}" ]] || { usage; die "--questionnaire is required."; }
[[ -f "${QUESTIONNAIRE}" ]] || die "Not found: ${QUESTIONNAIRE}"

if [[ "${DRY_RUN}" != "1" && ${CONFIRM} -ne 1 ]]; then
  die "Refusing to vest without --confirm-vest.

      This creates AWS accounts. They cannot be deleted, only closed, and
      closure suspends them for 90 days while permanently consuming their root
      email addresses.

      Review first:  scripts/vest-client.sh --questionnaire ${QUESTIONNAIRE} --dry-run"
fi

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

PY="$(command -v python || command -v python3)" || die "python not found."

# Windows python cannot open an MSYS path: /c/AltDigital/... arrives as
# C:\c\AltDigital\... and fails with a bare "No such file or directory" that
# names a path nobody typed. win_path is in common.sh for exactly this, and
# every python invocation below goes through it.
DERIVE_PY="$(win_path "${REPO_ROOT}/scripts/derive-vesting.py")"
QPATH="$(win_path "${QUESTIONNAIRE}")"

# ---------------------------------------------------------------------------
# 1. Derive
# ---------------------------------------------------------------------------
info "Validating questionnaire and deriving parameters"
"${PY}" "${DERIVE_PY}" --questionnaire "${QPATH}" \
  || die "Questionnaire is not vestable. Nothing was created."

DERIVED="$("${PY}" "${DERIVE_PY}" \
  --questionnaire "${QPATH}" --json)" || die "Derivation failed."

q_get() { "${PY}" -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1],encoding='utf-8'));print(eval('d'+sys.argv[2]))" "${QPATH}" "$1"; }
d_get() { printf '%s' "${DERIVED}" | "${PY}" -c "import sys,json;print(eval('json.load(sys.stdin)'+sys.argv[1]))" "$1"; }

PARTNER="$(q_get "['identity']['partner']")"
CLIENT="$(q_get "['identity']['client_slug']")"
APP="$(q_get "['identity']['application_code']")"
CLIENT_NAME="$(q_get "['identity']['client_name']")"
APP_OWNER="$(q_get "['identity']['partner_is_application_owner']")"
# Facts the registry must carry for something downstream to find them. Each
# of these had a consumer and no supplier until 2026-09-30: the digest read a
# tech contact nobody wrote, reconciliation read a change approver and Jira
# project nobody wrote, and every account was billed as service model 'unset'.
# See docs/questionnaire-coverage.md, findings 1 and 4.
TECH_EMAIL="$(q_get "['identity']['technical_contact']['email']")"
DEV_MGR_EMAIL="$(q_get "['security']['developer_manager']['email']")"
APPROVER_EMAIL="$(q_get "['identity']['change_approver']['email']")"
BREACH_EMAIL="$(q_get "['identity']['breach_notification_contact']['email']")"
SECOPS_EMAIL="$(q_get ".get('identity',{}).get('security_contact',{}).get('email') or ''")"
# Questionnaire change.ticketing (form 12.4). No project when the system is
# 'none'; a site only when the records live on the client's own Atlassian site.
JIRA_PROJECT="$(q_get "['change']['ticketing'].get('project_key') or ''")"
JIRA_SITE_URL="$(q_get "['change']['ticketing'].get('site') or ''")"
SERVICE_MODEL="$(q_get "['identity']['service_model']")"
# Section 17, D-029: where the client's application-layer evidence goes.
EVIDENCE_DESTINATION="$(d_get "['evidence_destination']")"
COMPLIANCE="$("${PY}" -c "import sys,yaml;d=yaml.safe_load(open(sys.argv[1],encoding='utf-8'));print('_'.join(f for f in d['compliance']['frameworks'] if f!='none') or 'none')" "${QPATH}")"
RETENTION="$(d_get "['retention_floor_days']")"
EGRESS="$(d_get "['egress_profile']")"
ENVS="$(printf '%s' "${DERIVED}" | "${PY}" -c "import sys,json;print(' '.join(json.load(sys.stdin)['environments']))")"
ALL_ENVS="${ENVS}"

# Staged vesting: a subset of the derived environments, in the derived order.
# Anything not in the derived set is refused rather than ignored -- a typo of
# `prod` must not become a run that quietly vests nothing and reports success.
if [[ -n "${ONLY_ENVS}" ]]; then
  SUBSET=""
  for want in ${ONLY_ENVS//,/ }; do
    [[ " ${ALL_ENVS} " == *" ${want} "* ]]       || die "--environments: '${want}' is not one of this tenant's environments (${ALL_ENVS})."
  done
  for env in ${ALL_ENVS}; do
    if [[ ",${ONLY_ENVS}," == *",${env},"* ]]; then SUBSET+="${env} "; fi
  done
  ENVS="${SUBSET% }"
fi

hr
log "Vesting"
log "  partner     : ${PARTNER}"
log "  client      : ${CLIENT} (${CLIENT_NAME})"
log "  application : ${APP}"
log "  environments: ${ENVS}$([[ "${ENVS}" != "${ALL_ENVS}" ]] && echo "   (STAGED — of ${ALL_ENVS})")"
log "  model       : ${SERVICE_MODEL}   compliance: ${COMPLIANCE}"
log "  retention   : ${RETENTION} days"
log "  egress      : ${EGRESS}"
log "  mode        : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo EXECUTE)"
hr

# ---------------------------------------------------------------------------
# 1b. The archive can hold this tenant's evidence for its floor
# ---------------------------------------------------------------------------
# Prompt 15.8 step 6. The Log Archive locks every object for the bucket's
# default period (COMPLIANCE, D-024), whatever the tenant. A tenant whose
# derived floor is LONGER than that would produce evidence the platform
# deletes before the framework allows. Refused here, before anything exists,
# rather than warned about after the evidence is written (section 9 below
# still reads back the lock actually applied, as the record of what happened).
#
# Read from the bucket, not from the template default: the template says 365,
# the bucket says 2190, and only the bucket is true. Checked in dry runs too:
# a dry run that passes here and a real run that refuses would be a lie.
ARCHIVE_ACCT="$(get_param /org/account/altdig-security-logarchive)"
ARCHIVE_LOCK="$(in_account "${ARCHIVE_ACCT}" aws s3api get-object-lock-configuration \
    --bucket "altdig-log-archive-${ARCHIVE_ACCT}" \
    --query 'ObjectLockConfiguration.Rule.DefaultRetention.[Mode,Days,Years]' \
    --output text 2>/dev/null | no_cr)" || true
read -r ARCHIVE_MODE ARCHIVE_DAYS ARCHIVE_YEARS <<<"${ARCHIVE_LOCK:-None None None}"
[[ "${ARCHIVE_DAYS}" =~ ^[0-9]+$ ]] \
  || { [[ "${ARCHIVE_YEARS}" =~ ^[0-9]+$ ]] && ARCHIVE_DAYS=$((ARCHIVE_YEARS * 365)); }
[[ "${ARCHIVE_DAYS}" =~ ^[0-9]+$ ]] \
  || die "Could not read the Log Archive's default retention, so this tenant's ${RETENTION}-day
      floor cannot be checked against it. Nothing has been created."
if [[ "${ARCHIVE_DAYS}" -lt "${RETENTION}" ]]; then
  die "This tenant's retention floor is ${RETENTION} days, and the Log Archive locks evidence
      for ${ARCHIVE_DAYS} (${ARCHIVE_MODE}). The platform would delete the tenant's evidence
      before its framework allows. Raise the archive's default retention first; that
      is irreversible, so it is a decision, not a fix. Nothing has been created."
fi
ok "archive retention ${ARCHIVE_DAYS} days (${ARCHIVE_MODE}) covers the tenant floor of ${RETENTION}"

# ---------------------------------------------------------------------------
# 2. Partner precondition
# ---------------------------------------------------------------------------
# Same gate create-client-ou.sh uses, called here too rather than relied upon
# downstream. Vesting reaches account creation before it would otherwise touch
# the OU script, and discovering an inactive partner after three accounts exist
# 'direct' is AltDigital's own client with no partner above them, so there is
# no partner record to check and never will be. create-client-ou.sh already
# exempts it; this did not, which would have made a direct client impossible to
# vest — a failure that only appears the first time AltDigital onboards its own
# customer rather than a partner's.
if [[ "${PARTNER}" == "direct" ]]; then
  skip "partner precondition — 'direct' has no partner above it"
else
  info "Partner precondition"
  bash "${REPO_ROOT}/scripts/registry.sh" check-partner --slug "${PARTNER}"     || die "Partner precondition failed. Nothing was created."
fi

# ---------------------------------------------------------------------------
# 2a. Evidence destination precondition (D-029)
# ---------------------------------------------------------------------------
# Checked here, before any account exists, because step 6 creates accounts and
# accounts cannot be deleted. Until 2026-10-04 this was only step 8, after
# creation, with --defer-truveon as the way past it -- and every Arc8 record
# from vesting until a runaway page two days later went nowhere (B-156).
#
# derive-vesting.py has already halted on external and archive, which are not
# deliverable until B-157 phase D. What is left to check is truveon: the
# client's tenant must be LINKED (registry names it, ingest key stored) before
# its accounts produce a single record. onboard-client.sh links it at step 2,
# before this; standalone, run truveon-tenant-handoff.sh first.
info "Evidence destination: ${EVIDENCE_DESTINATION}"
if [[ "${EVIDENCE_DESTINATION}" == "truveon" ]]; then
  if bash "${REPO_ROOT}/scripts/truveon-tenant-handoff.sh" status \
       --partner "${PARTNER}" --client "${CLIENT}" >/dev/null 2>&1; then
    ok "Truveon tenant linked for ${PARTNER}/${CLIENT}; ingest key stored."
  elif [[ "${DRY_RUN}" == "1" ]]; then
    warn "Truveon tenant NOT linked for ${PARTNER}/${CLIENT}. A real run stops here,"
    warn "before any account is created."
  else
    die "Evidence destination is truveon, and the client ${PARTNER}/${CLIENT} has no
      linked Truveon tenant. Nothing was created.

      Prompt 3.1: a vested account that is not sending evidence is worse than
      no account. Link the tenant first (one per client):
        scripts/truveon-tenant-handoff.sh prepare --questionnaire <q> [--truveon-roster <r>]
        # ...hand the pack to Truveon; its last step stores the key and links...
        scripts/truveon-tenant-handoff.sh status --partner ${PARTNER} --client ${CLIENT}"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Baseline reachability
# ---------------------------------------------------------------------------
# A member account with no baseline is worse than no account: it looks vested,
# it appears in the registry, and it has none of the detective controls the
# platform's claims rest on.
#
# Service-managed StackSets auto-deploy to accounts added to a TARGETED OU. If
# the baseline StackSets do not target Members, a newly created account inherits
# nothing and nothing says so — the account simply comes up bare.
info "Checking the baseline StackSets target the Members OU"
MEMBERS_OU="$(get_param /org/ou/members)"
[[ -n "${MEMBERS_OU}" && "${MEMBERS_OU}" != "None" ]] || die "Members OU not found in SSM."

# platform-developer-boundary since 2026-10-01. Without it a vested account has
# no PlatformDeveloperBoundary, and SCP 05 then refuses every role a developer
# or the tenant pipeline tries to create, because 05 requires exactly that
# boundary. That fails safe, but it is an outage on the tenant's first day.
BASELINE_STACKSETS="platform-config platform-kms-secrets platform-log-data-protection platform-network platform-developer-boundary"
MISSING_BASELINE=""
for ss in ${BASELINE_STACKSETS}; do
  # describe-stack-set, not list-stack-instances. The latter reports OUs that
  # HAVE ACCOUNTS, so an OU correctly targeted but currently empty — which the
  # Members OU is, right up until the first client is vested — is
  # indistinguishable from one never targeted. This check would then block the
  # first vesting and pass every later one, which is exactly backwards.
  TARGETS="$(MSYS_NO_PATHCONV=1 aws cloudformation describe-stack-set     --stack-set-name "${ss}" --query 'StackSet.OrganizationalUnitIds'     --output text 2>/dev/null | no_cr || true)"
  if [[ "${TARGETS}" != *"${MEMBERS_OU}"* ]]; then
    MISSING_BASELINE="${MISSING_BASELINE} ${ss}"
  fi
done

if [[ -n "${MISSING_BASELINE}" ]]; then
  warn "These baseline StackSets do not yet target the Members OU (${MEMBERS_OU}):"
  for ss in ${MISSING_BASELINE}; do warn "    ${ss}"; done
  warn ""
  warn "Accounts created now would come up with NO baseline — no Config recorder,"
  warn "no data protection policy, no platform KMS keys, no VPC. They would look"
  warn "vested and be bare."
  warn ""
  warn "Roll the baseline to Members first:"
  warn "  scripts/deploy-stackset.sh --name <each> --template <path> \\"
  warn "    --ou members --confirm-production"
  [[ "${DRY_RUN}" == "1" ]] || die "Refusing to vest into an unbaselined OU."
fi

# ---------------------------------------------------------------------------
# 3a. Account quota
# ---------------------------------------------------------------------------
# Organizations caps the number of accounts (L-E619E033; 10 by default). A
# vesting that hits the cap part-way creates some accounts and then fails, and
# the accounts it did create cannot be deleted. So the whole set must fit
# before the first one is created. B-140.
info "Checking the account quota"
QUOTA="$(MSYS_NO_PATHCONV=1 aws service-quotas get-service-quota --region us-east-1 \
  --service-code organizations --quota-code L-E619E033 \
  --query 'Quota.Value' --output text 2>/dev/null | no_cr | cut -d. -f1 || true)"
ACTIVE="$(MSYS_NO_PATHCONV=1 aws organizations list-accounts \
  --query "length(Accounts[?Status!='SUSPENDED'])" --output text | no_cr)"
NEEDED=0
for ENVNAME in ${ENVS}; do
  EMAIL="$(account_email "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}")"
  EXISTS="$(MSYS_NO_PATHCONV=1 aws organizations list-accounts \
    --query "length(Accounts[?Email=='${EMAIL}'])" --output text | no_cr)"
  [[ "${EXISTS}" == "0" ]] && NEEDED=$((NEEDED+1))
done
[[ "${QUOTA}" =~ ^[0-9]+$ ]] || die "Could not read the account quota (L-E619E033). Refusing to guess."
if (( ACTIVE + NEEDED > QUOTA )); then
  warn "Account quota: ${ACTIVE} accounts exist, this vesting needs ${NEEDED} more, the quota is ${QUOTA}."
  warn "Request an increase first (Service Quotas, AWS Organizations, 'Maximum number of accounts')."
  [[ "${DRY_RUN}" == "1" ]] || die "Refusing to start a vesting that cannot finish. Nothing was created."
else
  ok "account quota: ${ACTIVE} + ${NEEDED} <= ${QUOTA}"
fi

# ---------------------------------------------------------------------------
# 3b. Guardrails: Members carries everything Sandbox carries
# ---------------------------------------------------------------------------
# A tenant account inherits its SCPs and RCPs from Members (and Root). Until
# phase 15.4 promoted them, region restriction, required tags, the boundary
# requirement, deny-bypass and the external-principals RCP were Sandbox-only:
# an account vested then would have come up in Members without them, and
# nothing here would have said so. The baseline StackSet check above looks at
# StackSets, not at policies.
#
# Compared against Sandbox rather than a list of names. Sandbox is where every
# guardrail is proven before promotion, so "Members has what Sandbox has" IS
# the definition of promotion being finished -- and it survives renames (the
# v1 -> v2 protect policies) without anyone remembering to edit a list here.
# Root attachments count for both, since both inherit them. The AWS-managed
# FullAWSAccess pair is ignored: it allows, it guards nothing.
#
# Consequence worth knowing: a policy being trialled in Sandbox blocks vesting
# until it is promoted or detached. That is deliberate. A guardrail proven but
# not promoted is exactly the gap this exists to catch.
info "Checking Members carries the guardrails Sandbox does"
policies_on() {
  local target="$1" ptype
  for ptype in SERVICE_CONTROL_POLICY RESOURCE_CONTROL_POLICY; do
    MSYS_NO_PATHCONV=1 aws organizations list-policies-for-target \
      --target-id "${target}" --filter "${ptype}" \
      --query 'Policies[].Name' --output text 2>/dev/null | no_cr | tr '\t' '\n'
  done | grep -v -E '^(FullAWSAccess|RCPFullAWSAccess|None)?$' | sort -u
}
ROOT_ID="$(org_root_id)"
SANDBOX_OU="$(get_param /org/ou/sandbox)"
[[ -n "${ROOT_ID}" && -n "${SANDBOX_OU}" && "${SANDBOX_OU}" != "None" \
   && -n "${MEMBERS_OU}" && "${MEMBERS_OU}" != "None" ]] \
  || die "Could not resolve Root, Sandbox or Members. Refusing to vest without checking guardrails."
SANDBOX_SET="$( { policies_on "${ROOT_ID}"; policies_on "${SANDBOX_OU}"; } | sort -u)"
MEMBERS_SET="$( { policies_on "${ROOT_ID}"; policies_on "${MEMBERS_OU}"; } | sort -u)"
MISSING_GUARDRAILS="$(comm -23 <(printf '%s\n' "${SANDBOX_SET}") <(printf '%s\n' "${MEMBERS_SET}") | grep -v '^$' || true)"
if [[ -n "${MISSING_GUARDRAILS}" ]]; then
  warn "Members does not carry these guardrails, which Sandbox does:"
  while IFS= read -r p; do warn "    ${p}"; done <<< "${MISSING_GUARDRAILS}"
  warn ""
  warn "An account vested now would inherit Members' policies and come up without"
  warn "them. Promote them to Members first (phase 15.4; scripts/deploy-policies.sh)."
  [[ "${DRY_RUN}" == "1" ]] || die "Refusing to vest beneath incomplete guardrails. Nothing was created."
else
  ok "Members carries every guardrail Sandbox does ($(printf '%s\n' "${MEMBERS_SET}" | grep -c . ) policies, Root included)"
fi

# ---------------------------------------------------------------------------
# 3c. AWS BAA, for tenants that hold PHI
# ---------------------------------------------------------------------------
# HIPAA requires a business associate agreement with every business associate
# that handles PHI, and AWS is one. It is accepted in AWS Artifact, and only an
# ORGANIZATION-level BAA covers member accounts: one accepted by a single
# account covers that account alone. Before this check nothing recorded or
# verified it, so a PHI tenant could be vested onto infrastructure with no
# BAA beneath it.
#
# Checked in the management account, which is where an organization BAA is
# accepted. "Active" means AWS reports the agreement ACTIVE and it names the
# Organization; anything else blocks, including an API that cannot be read --
# not knowing whether a BAA exists is not the same as knowing it does.
PHI="$(q_get "['compliance']['phi']")"
if [[ "${PHI}" == "True" ]]; then
  info "Checking an organization-wide AWS BAA is active (tenant holds PHI)"
  BAA_JSON="$(MSYS_NO_PATHCONV=1 aws artifact list-customer-agreements --region us-east-1 \
    --output json 2>/dev/null | no_cr || true)"
  BAA_VERDICT="$(printf '%s' "${BAA_JSON}" | "${PY}" -c '
import json, sys
# Bytes, then decoded explicitly. On Windows the CLI writes this response in
# the console code page (the agreement text carries curly quotes), and how
# Python decodes stdin depends on the locale of the machine. A UTF-8 decode error
# here would read as "unreadable" and refuse a tenant whose BAA is fine.
blob = sys.stdin.buffer.read()
try:
    raw = blob.decode("utf-8").strip()
except UnicodeDecodeError:
    raw = blob.decode("cp1252", errors="replace").strip()
if not raw:
    print("unreadable"); sys.exit()
try:
    items = json.loads(raw).get("customerAgreements", [])
except ValueError:
    print("unreadable"); sys.exit()
# Scope is read from the NAME. The API has no scope field, and organizationArn
# is documented as the organization that "owns the resource", which may be set
# on a single-account agreement accepted inside an organization -- so it
# cannot tell the two apart. Artifact offers the organization BAA as its own
# agreement, named for Organizations. Verified 2026-10-01 against the real
# acceptance: the organization BAA is "AWS Organizations Business Associate
# Addendum" with organizationArn set; the account BAA is "AWS Business
# Associate Addendum" with organizationArn empty and the id of the management
# account. If AWS renames it, this fails CLOSED and prints what it found.
# (No apostrophes in these comments: this Python is a single-quoted bash string.)
active = [a for a in items if a.get("state") == "ACTIVE"
          and "business associate" in (a.get("name") or "").lower()]
if any("organization" in (a.get("name") or "").lower() for a in active):
    print("active-org")
elif active:
    print("active-account-only|" + "; ".join(sorted(a.get("name") or "?" for a in active)))
else:
    print("none")
')"
BAA_FOUND="${BAA_VERDICT#*|}"; BAA_VERDICT="${BAA_VERDICT%%|*}"
  case "${BAA_VERDICT}" in
    active-org)
      ok "an organization-wide AWS BAA is active" ;;
    active-account-only)
      warn "An AWS BAA is active, but not one this recognises as organization-wide."
      warn "  found: ${BAA_FOUND}"
      warn "If that IS the organization BAA under a different name, the name test in"
      warn "this step needs updating; a single-account BAA covers that account alone."
      warn "It does not cover member accounts. Accept the organization BAA in AWS"
      warn "Artifact from the management account."
      [[ "${DRY_RUN}" == "1" ]] || die "Refusing to vest a PHI tenant without an organization BAA." ;;
    none)
      warn "No active AWS BAA. This tenant holds PHI (questionnaire 4.4)."
      warn "Accept the organization BAA in AWS Artifact from the management account."
      [[ "${DRY_RUN}" == "1" ]] || die "Refusing to vest a PHI tenant without an AWS BAA." ;;
    *)
      warn "Could not read AWS Artifact agreements (artifact:ListCustomerAgreements)."
      warn "Not knowing whether a BAA exists is not knowing that one does."
      [[ "${DRY_RUN}" == "1" ]] || die "Refusing to vest a PHI tenant without confirming the AWS BAA." ;;
  esac
fi

# ---------------------------------------------------------------------------
# 4. Registry entry FIRST, in provisioning state
# ---------------------------------------------------------------------------
info "Registry: client record in 'provisioning'"
IP_OWNER="$([[ "${APP_OWNER}" == "True" ]] && echo partner || echo client)"
if [[ "${DRY_RUN}" == "1" ]]; then
  # Contacts are counted, not printed: a dry run's output is the thing most
  # likely to be pasted into a ticket or an assistant.
  printf '%s DRY%s  registry put-client %s/%s state=provisioning ip-owner=%s contacts=5 jira=%s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${CLIENT}" "${IP_OWNER}" "${JIRA_PROJECT:-none}"
else
  bash "${REPO_ROOT}/scripts/registry.sh" put-client \
    --partner "${PARTNER}" --slug "${CLIENT}" --state provisioning --ip-owner "${IP_OWNER}" \
    --tech-contact "${TECH_EMAIL}" --dev-manager "${DEV_MGR_EMAIL}" \
    --change-approver "${APPROVER_EMAIL}" --jira-project "${JIRA_PROJECT}" \
    --breach-contact "${BREACH_EMAIL}" --security-contact "${SECOPS_EMAIL}" \
    --jira-site "${JIRA_SITE_URL}" --evidence-destination "${EVIDENCE_DESTINATION}"
fi

# ---------------------------------------------------------------------------
# 5. Client OU
# ---------------------------------------------------------------------------
info "Client OU"
OU_ARGS=(--partner "${PARTNER}" --slug "${CLIENT}" --legal-name "${CLIENT_NAME}")
[[ "${APP_OWNER}" == "True" ]] && OU_ARGS+=(--app-owner partner --app-builder partner)
[[ "${DRY_RUN}" == "1" ]] && OU_ARGS+=(--dry-run)
bash "${REPO_ROOT}/scripts/create-client-ou.sh" "${OU_ARGS[@]}" \
  || die "Client OU step failed."

CLIENT_OU="$(get_param "/org/ou/members/${PARTNER}/${CLIENT}")"
if [[ "${DRY_RUN}" != "1" ]]; then
  [[ -n "${CLIENT_OU}" && "${CLIENT_OU}" != "None" ]] \
    || die "Client OU was not registered in SSM. Refusing to create accounts with nowhere to put them."
fi

# ---------------------------------------------------------------------------
# 6. Member accounts
# ---------------------------------------------------------------------------
# Idempotent on root email. AWS refuses a duplicate, so a re-run after a partial
# failure resumes rather than duplicating — which matters because the failure
# mode being guarded against is unrecoverable.
hr
info "Member accounts"
CREATED_IDS=""
for ENVNAME in ${ENVS}; do
  ALIAS="$(account_alias "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}")"
  EMAIL="$(account_email "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}")"
  ACCT_NAME="AltDigital ${CLIENT_NAME} ${APP} ${ENVNAME}"

  EXISTING="$(MSYS_NO_PATHCONV=1 aws organizations list-accounts \
    --query "Accounts[?Email=='${EMAIL}'].Id" --output text 2>/dev/null | no_cr || true)"

  if [[ -n "${EXISTING}" && "${EXISTING}" != "None" ]]; then
    skip "${ALIAS} exists (${EXISTING})"
    CREATED_IDS="${CREATED_IDS} ${ENVNAME}:${EXISTING}"
    continue
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create-account %-46s %s\n' "${C_YELLOW}" "${C_RESET}" "${ALIAS}" "${EMAIL}"
    CREATED_IDS="${CREATED_IDS} ${ENVNAME}:<new>"
    continue
  fi

  info "Creating ${ALIAS}"
  REQ="$(MSYS_NO_PATHCONV=1 aws organizations create-account \
    --account-name "${ACCT_NAME}" --email "${EMAIL}" \
    --query 'CreateAccountStatus.Id' --output text | no_cr)"
  ACCT=""
  for _ in $(seq 1 60); do
    STATUS="$(MSYS_NO_PATHCONV=1 aws organizations describe-create-account-status \
      --create-account-request-id "${REQ}" \
      --query '[CreateAccountStatus.State,CreateAccountStatus.AccountId,CreateAccountStatus.FailureReason]' \
      --output text | no_cr)"
    read -r STATE ACCT REASON <<<"${STATUS}"
    [[ "${STATE}" == "IN_PROGRESS" ]] || break
    sleep 10
  done
  [[ "${STATE}" == "SUCCEEDED" ]] || die "Account creation failed for ${ALIAS}: ${REASON:-unknown}"
  ok "${ALIAS} = ${ACCT}"

  MSYS_NO_PATHCONV=1 aws organizations move-account --account-id "${ACCT}" \
    --source-parent-id "$(org_root_id)" --destination-parent-id "${CLIENT_OU}"
  ok "moved into ${CLIENT}"
  CREATED_IDS="${CREATED_IDS} ${ENVNAME}:${ACCT}"

  # Register the account and publish the router's tier map NOW, before the
  # baseline StackSets -- which start deploying the moment the account lands in
  # the OU -- bring up event forwarding. Until the router knows the account it
  # treats it as prod, which pages, and a brand-new account's first Security
  # Hub findings (CloudTrail.1, Config.1, CIS 2.1, all transient while the
  # baseline is still arriving) are many. On the first real vesting (Arc8,
  # 2026-10-01) the account was registered only in step 7, after the baseline
  # settled, and the tier map published in onboarding step 8: four [prod]
  # pages at 21:36 for two dev/test accounts. Step 7 still re-writes the
  # record with the final baseline version.
  bash "${REPO_ROOT}/scripts/registry.sh" put-account \
    --account-id "${ACCT}" --partner "${PARTNER}" --slug "${CLIENT}" \
    --app "${APP}" --env "${ENVNAME}" --state provisioning \
    --service-model "${SERVICE_MODEL}" --compliance "${COMPLIANCE}" >/dev/null \
    || die "Could not register ${ACCT} (${ENVNAME}). The router will treat it as prod and page; register it before continuing."
  bash "${REPO_ROOT}/scripts/sync-account-tiers.sh" >/dev/null \
    || die "Could not publish the tier map for ${ACCT}. Run scripts/sync-account-tiers.sh before the baseline arrives, or its findings page as prod."
  ok "registered ${ACCT} as ${ENVNAME}; router tier map published"

  # Lambda concurrency (B-151). New accounts arrive with 10 concurrent
  # executions in the home region, shared between the platform's own functions
  # (instrumentation, response) and whatever the tenant deploys -- so a busy
  # application can starve the platform's monitoring in its own account. Every
  # account in the organization was found at 10 on 2026-10-02. The increase is
  # requested here, from inside the new account, as the account is created; it
  # is routine and approved by AWS on its own schedule. A failed request warns
  # rather than stopping vesting: the account works at 10, and B-151's check
  # reports any account still below 1000.
  if ( unset AWS_PROFILE
       CA="$(aws sts assume-role --role-arn "arn:aws:iam::${ACCT}:role/OrganizationAccountAccessRole" \
             --role-session-name vest-lambda-quota \
             --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text | no_cr)"
       read -r QK QS QT <<<"${CA}"
       export AWS_ACCESS_KEY_ID="${QK}" AWS_SECRET_ACCESS_KEY="${QS}" AWS_SESSION_TOKEN="${QT}"
       aws service-quotas request-service-quota-increase --region "${PLATFORM_HOME_REGION}" \
         --service-code lambda --quota-code L-B99A9384 --desired-value 1000 >/dev/null ); then
    ok "requested Lambda concurrency 1000 for ${ACCT} (from 10)"
  else
    warn "Could not request the Lambda concurrency increase for ${ACCT}. It runs at 10
      until someone does: Service Quotas, Lambda, Concurrent executions (L-B99A9384)."
  fi
done

# ---------------------------------------------------------------------------
# 6a. Account settings
# ---------------------------------------------------------------------------
# The account-level settings the baseline StackSets cannot express: EBS
# encryption by default, snapshot and SSM document public sharing blocked,
# default security groups emptied, S3 Block Public Access, the password
# policy and the Support role (B-139). A new account has none of them, and
# nothing else sets them.
#
# Failure stops vesting, and a re-run resumes: account creation above is
# idempotent on email and so is this.
hr
info "Account settings"
for pair in ${CREATED_IDS}; do
  ACCT="${pair##*:}"
  [[ "${ACCT}" == "<new>" ]] && { skip "account settings for ${pair%%:*} (dry run; account not created)"; continue; }
  SETTINGS_ARGS=(--account "${ACCT}")
  [[ "${DRY_RUN}" == "1" ]] && SETTINGS_ARGS+=(--dry-run)
  bash "${REPO_ROOT}/scripts/apply-account-settings.sh" "${SETTINGS_ARGS[@]}" \
    || die "Account settings failed for ${ACCT}. Fix and re-run vesting; it resumes."
  if [[ "${DRY_RUN}" != "1" ]]; then
    bash "${REPO_ROOT}/scripts/apply-account-settings.sh" --account "${ACCT}" --verify \
      || die "Account settings did not verify for ${ACCT}. Do not treat it as vested."
  fi
done

# ---------------------------------------------------------------------------
# 6b. Baseline settles (B-062)
# ---------------------------------------------------------------------------
# Every baseline StackSet reaches a new account at once, in no order, and two
# depend on others (kms-secrets on the Config recorder, instrumentation on the
# logs key). The first attempt fails, nothing retries it, and the account
# looks baselined while it has no instrumentation. settle-baseline.sh waits,
# retries once in dependency order, and refuses unless every instance is
# CURRENT. Failure stops vesting. A re-run resumes.
hr
info "Baseline settles"
for pair in ${CREATED_IDS}; do
  ACCT="${pair##*:}"
  [[ "${ACCT}" == "<new>" ]] && { skip "baseline for ${pair%%:*} (dry run; account not created)"; continue; }
  SETTLE_ARGS=(--account "${ACCT}")
  [[ "${DRY_RUN}" == "1" ]] && SETTLE_ARGS+=(--dry-run)
  bash "${REPO_ROOT}/scripts/settle-baseline.sh" "${SETTLE_ARGS[@]}" \
    || die "The baseline did not settle for ${ACCT}. Do not treat it as vested. Fix and re-run vesting; it resumes."
done

# ---------------------------------------------------------------------------
# 7. Registry: account records
# ---------------------------------------------------------------------------
hr
info "Registry: account records"
BASELINE_VERSION="$(bash "${REPO_ROOT}/scripts/registry.sh" get-baseline 2>/dev/null | tail -1 | tr -d ' ')"
for pair in ${CREATED_IDS}; do
  ENVNAME="${pair%%:*}"; ACCT="${pair##*:}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  registry put-account %s %s/%s/%s %s model=%s compliance=%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${ACCT}" "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}" \
      "${SERVICE_MODEL}" "${COMPLIANCE}"
  else
    bash "${REPO_ROOT}/scripts/registry.sh" put-account \
      --account-id "${ACCT}" --partner "${PARTNER}" --slug "${CLIENT}" \
      --app "${APP}" --env "${ENVNAME}" --state provisioning \
      --service-model "${SERVICE_MODEL}" --compliance "${COMPLIANCE}" \
      --baseline-version "$((10#${BASELINE_VERSION:-0}))" >/dev/null
    ok "registered ${ACCT} (${ENVNAME})"
  fi
done

# ---------------------------------------------------------------------------
# 8. Truveon
# ---------------------------------------------------------------------------
# Prompt 3.1: "Vesting must fail loudly if the Truveon tenant registration does
# not confirm first evidence receipt. A vested account that is not sending
# evidence is worse than no account."
#
# What this records is REGISTRATION, not receipt -- D-021. Accounts created
# seconds ago have emitted nothing, and the evidence tenant map that attributes
# them is published at onboarding step 8, after this. Receipt is the
# verification sweep's check 1, before go-live.
#
# The GATE is step 2a, before account creation (D-029). This re-reads the link
# for the evidence record, so the record states what was true after the
# accounts existed rather than what was true before.
hr
TRUVEON_LINKED=0
if [[ "${EVIDENCE_DESTINATION}" == "truveon" ]] \
   && bash "${REPO_ROOT}/scripts/truveon-tenant-handoff.sh" status \
        --partner "${PARTNER}" --client "${CLIENT}" >/dev/null 2>&1; then
  TRUVEON_LINKED=1
  ok "Truveon tenant linked for ${PARTNER}/${CLIENT}; ingest key stored."
  log "  First-evidence receipt is confirmed by the verification sweep, not here"
  log "  — these accounts have not emitted anything yet. D-021."
elif [[ "${DRY_RUN}" == "1" ]]; then
  warn "Truveon tenant not linked (dry run; step 2a stops a real run)."
else
  # Unreachable unless the link was removed between step 2a and here.
  die "The Truveon link for ${PARTNER}/${CLIENT} was present at step 2a and is
      not now. The accounts exist; find out what removed the link, restore it
      with scripts/truveon-tenant-handoff.sh, and re-run. Re-running is safe."
fi

# ---------------------------------------------------------------------------
# 9. Evidence record
# ---------------------------------------------------------------------------
# Written to the Object Lock archive, not to the registry. The registry is
# mutable by design — states change, overrides come and go. A vesting evidence
# record is a statement about a moment, and an auditor's question is "what was
# true when this account was created", which a mutable store cannot answer.
#
# The archive is COMPLIANCE mode, so this cannot be altered or deleted by any
# principal for the retention period, and it replicates to us-west-2.
hr
info "Vesting evidence record"
LOG_ARCHIVE_ACCT="$(get_param /org/account/altdig-security-logarchive)"
EVIDENCE_KEY="vesting-evidence/${PARTNER}/${CLIENT}/${APP}/$(date -u +%Y%m%dT%H%M%SZ).json"

EVIDENCE="$("${PY}" - "${QPATH}" "${CREATED_IDS}" "${TRUVEON_LINKED}" "${ENVS}" "${ALL_ENVS}" <<'PYEOF'
import hashlib, json, sys, datetime, pathlib
qpath, ids, linked, envs, all_envs = sys.argv[1:6]
raw = pathlib.Path(qpath).read_bytes()
print(json.dumps({
    "record_type": "vesting-evidence",
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    # The questionnaire itself is NOT embedded: it carries named contacts, and
    # this record is retained for years in an archive read by auditors. The
    # hash proves which questionnaire was used without reproducing the personal
    # data in it.
    "questionnaire_sha256": hashlib.sha256(raw).hexdigest(),
    "questionnaire_filename": pathlib.Path(qpath).name,
    "accounts": dict(p.split(":", 1) for p in ids.split() if ":" in p),
    # Staged vesting (--environments). Recorded so a record covering two of
    # three accounts is not read as the tenant's whole set.
    "environments_this_run": envs.split(),
    "environments_derived": all_envs.split(),
    "staged": envs.split() != all_envs.split(),
    # Linked is what vesting checks. Receipt is never verified at vesting --
    # the accounts have emitted nothing yet -- and the record says so
    # rather than implying a check that did not happen. D-021.
    "truveon_tenant_linked": linked == "1",
    "truveon_evidence_verified": False,
}, indent=2))
PYEOF
)"

DERIVED_MERGED="$(printf '%s\n%s' "${EVIDENCE}" "${DERIVED}" | "${PY}" -c "
import json,sys
parts=sys.stdin.read().split('}\n{')
a=json.loads(parts[0]+'}'); b=json.loads('{'+parts[1])
a['derived']=b
print(json.dumps(a,indent=2))
")"

if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  s3://altdig-log-archive-%s/%s\n' \
    "${C_YELLOW}" "${C_RESET}" "${LOG_ARCHIVE_ACCT}" "${EVIDENCE_KEY}"
  printf '%s\n' "${DERIVED_MERGED}" | sed 's/^/      /'
else
  CREDS="$(aws sts assume-role \
    --role-arn "arn:aws:iam::${LOG_ARCHIVE_ACCT}:role/OrganizationAccountAccessRole" \
    --role-session-name vesting-evidence \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${CREDS}" ]] || die "Could not assume into the Log Archive account to write evidence."
  read -r EAK ESK EST <<<"${CREDS}"
  TMP="$(mktemp)"; printf '%s' "${DERIVED_MERGED}" > "${TMP}"
  # A SUBSHELL with `unset AWS_PROFILE`, not an inline `AWS_PROFILE=` prefix.
  # An empty AWS_PROFILE is not an absent one: the CLI looks for a profile
  # literally named "" and fails with "The config profile () could not be
  # found". Inline prefixes can set a variable but never unset it. The same
  # trap is documented in scripts/pagerduty-apply.sh; this step walked into it
  # on the first real vesting (OEight / Arc8, 2026-10-01), after both accounts
  # had been created, because common.sh exports AWS_PROFILE for every run.
  as_log_archive() {
    ( unset AWS_PROFILE
      export AWS_ACCESS_KEY_ID="${EAK}" AWS_SECRET_ACCESS_KEY="${ESK}" \
             AWS_SESSION_TOKEN="${EST}" MSYS_NO_PATHCONV=1
      aws "$@" )
  }
  as_log_archive s3api put-object \
      --bucket "altdig-log-archive-${LOG_ARCHIVE_ACCT}" \
      --key "${EVIDENCE_KEY}" --body "$(win_path "${TMP}")" \
      --content-type application/json >/dev/null \
    || die "Could not write the vesting evidence record to the Log Archive."
  ok "evidence written to s3://altdig-log-archive-${LOG_ARCHIVE_ACCT}/${EVIDENCE_KEY}"

  # Report the retention S3 ACTUALLY applied, read back from the object, not
  # the tenant's derived floor. They differ: the archive's bucket default is
  # COMPLIANCE for every object from every account (D-024), so a SOC 2-only
  # tenant with a 365-day floor still gets the archive's full period. This line
  # used to print the derived floor, which on that tenant would have understated
  # the lock -- in the output of the step whose whole job is evidence.
  #
  # The direction that matters is the other one: an archive period SHORTER than
  # the tenant's floor means the platform cannot meet a framework's retention.
  # Nothing today can cause it, since the archive is at 2190 and no framework
  # floor exceeds that, but a new framework or a changed bucket default could.
  LOCK="$(as_log_archive s3api get-object-retention \
      --bucket "altdig-log-archive-${LOG_ARCHIVE_ACCT}" --key "${EVIDENCE_KEY}" \
      --query 'Retention.[Mode,RetainUntilDate]' --output text 2>/dev/null | no_cr)" || true
  rm -f "${TMP}"
  if [[ -z "${LOCK}" || "${LOCK}" == "None"* ]]; then
    warn "Could not read the evidence object's retention. The archive is meant to lock it
      COMPLIANCE for its full period (D-024); confirm with s3api get-object-retention
      before relying on this record."
  else
    read -r LOCK_MODE LOCK_UNTIL <<<"${LOCK}"
    LOCK_DAYS="$("${PY}" -c "import datetime,sys;u=datetime.datetime.fromisoformat(sys.argv[1].replace('Z','+00:00'));print((u-datetime.datetime.now(datetime.timezone.utc)).days)" "${LOCK_UNTIL}")"
    # +1: whole days remaining on an object locked seconds ago for exactly N
    # days reads back as N-1, which would warn on every run.
    if [[ "$((LOCK_DAYS + 1))" -lt "${RETENTION}" ]]; then
      warn "Evidence is locked ${LOCK_MODE} for ${LOCK_DAYS} days, SHORTER than this tenant's
      ${RETENTION}-day floor. The platform cannot meet the framework's retention for
      this tenant. Raise the archive period before this tenant produces evidence."
    else
      ok "immutable (${LOCK_MODE}) until ${LOCK_UNTIL}, ${LOCK_DAYS} days; tenant floor ${RETENTION}; replicated to the us-west-2 archive"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# The trail query learns the new accounts (ADR-008)
# ---------------------------------------------------------------------------
# The org trail's Athena table projects a fixed account list. Until it is
# redeployed, every query silently leaves these accounts out. A warning, not a
# failure: the accounts are vested either way, and a re-run of
# deploy-trail-query.sh is all that is missing.
if [[ "${DRY_RUN}" != "1" ]]; then
  info "Adding the new accounts to the trail query (Log Archive)"
  if bash "${REPO_ROOT}/scripts/deploy-trail-query.sh" >/dev/null 2>&1; then
    ok "trail query covers every account"
  else
    warn "The trail query was not updated; these accounts are invisible to it."
    warn "  Run scripts/deploy-trail-query.sh"
  fi
fi

hr
if [[ "${DRY_RUN}" == "1" ]]; then
  ok "Dry run complete — nothing created."
else
  ok "Vesting complete for ${PARTNER}/${CLIENT}/${APP}"
  log ""
  log "The client and accounts are in 'provisioning', NOT 'active'. Promoting"
  log "them is the verification sweep's job (phase 12.5), not this script's —"
  log "an account is vested when it is proven working, not when it is created."
fi
if [[ "${ENVS}" != "${ALL_ENVS}" ]]; then
  log ""
  warn "STAGED: vested ${ENVS} of ${ALL_ENVS}. The tenant is incomplete and cannot go"
  warn "live. Re-run without --environments when the rest can be created; accounts"
  warn "that exist are found, not recreated."
fi
hr
