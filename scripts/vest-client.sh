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

QUESTIONNAIRE=""; CONFIRM=0; DEFER_TRUVEON=0

usage() {
  cat <<'USAGE'
Usage: vest-client.sh --questionnaire <path> [--dry-run] [--confirm-vest]
                      [--defer-truveon]

  --questionnaire   Completed questionnaire, YAML. Validated and derived by
                    scripts/derive-vesting.py before anything is created.
  --dry-run         Print every derived value and every resource that would be
                    created. Creates nothing.
  --confirm-vest    Required for a real run. Creates AWS accounts, which cannot
                    be deleted.
  --defer-truveon   Proceed without a confirmed Truveon first-evidence receipt.
                    Records a deviation on the account rather than skipping
                    silently. See the Truveon section below.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --questionnaire) QUESTIONNAIRE="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --confirm-vest)  CONFIRM=1; shift ;;
    --defer-truveon) DEFER_TRUVEON=1; shift ;;
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
RETENTION="$(d_get "['retention_floor_days']")"
EGRESS="$(d_get "['egress_profile']")"
ENVS="$(printf '%s' "${DERIVED}" | "${PY}" -c "import sys,json;print(' '.join(json.load(sys.stdin)['environments']))")"

hr
log "Vesting"
log "  partner     : ${PARTNER}"
log "  client      : ${CLIENT} (${CLIENT_NAME})"
log "  application : ${APP}"
log "  environments: ${ENVS}"
log "  retention   : ${RETENTION} days"
log "  egress      : ${EGRESS}"
log "  mode        : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo EXECUTE)"
hr

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

BASELINE_STACKSETS="platform-config platform-kms-secrets platform-log-data-protection platform-network"
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
# 4. Registry entry FIRST, in provisioning state
# ---------------------------------------------------------------------------
info "Registry: client record in 'provisioning'"
IP_OWNER="$([[ "${APP_OWNER}" == "True" ]] && echo partner || echo client)"
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%s DRY%s  registry put-client %s/%s state=provisioning ip-owner=%s\n' \
    "${C_YELLOW}" "${C_RESET}" "${PARTNER}" "${CLIENT}" "${IP_OWNER}"
else
  bash "${REPO_ROOT}/scripts/registry.sh" put-client \
    --partner "${PARTNER}" --slug "${CLIENT}" --state provisioning --ip-owner "${IP_OWNER}"
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
    printf '%s DRY%s  registry put-account %s %s/%s/%s %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${ACCT}" "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}"
  else
    bash "${REPO_ROOT}/scripts/registry.sh" put-account \
      --account-id "${ACCT}" --partner "${PARTNER}" --slug "${CLIENT}" \
      --app "${APP}" --env "${ENVNAME}" --state provisioning \
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
# The integration is not built. That leaves two honest options — fail, or make
# the deferral explicit and recorded — and exactly one dishonest one, which is
# to skip the step because it is not wired yet. This takes the second: the gate
# fails by default, and --defer-truveon proceeds while writing a declared
# override against every account, owned and dated.
#
# That way the gap appears in `registry.sh report-overrides` alongside every
# other declared difference, rather than living in a comment in this file.
hr
if [[ ${DEFER_TRUVEON} -eq 1 ]]; then
  warn "Truveon evidence receipt NOT verified — deferred by --defer-truveon."
  warn "A declared override is recorded against each account so this appears in"
  warn "report-overrides rather than being forgotten."
  for pair in ${CREATED_IDS}; do
    ENVNAME="${pair%%:*}"; ACCT="${pair##*:}"
    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  registry put-override %s TruveonEvidenceUnverified\n' \
        "${C_YELLOW}" "${C_RESET}" "${ACCT}"
    else
      bash "${REPO_ROOT}/scripts/registry.sh" put-override \
        --account-id "${ACCT}" --name TruveonEvidenceUnverified \
        --reason "Vested before the Truveon integration existed. First evidence receipt was never confirmed, so this account may be sending no evidence and nothing would report it. Prompt 3.1 requires this to block; it was deferred deliberately." \
        --owner "Jamie Vernon" --review-by "$(date -u -d '+90 days' +%Y-%m-%d)" >/dev/null
    fi
  done
else
  die "Truveon tenant registration and first-evidence receipt are NOT verified.

      Prompt 3.1: a vested account that is not sending evidence is worse than
      no account, so this blocks rather than warns.

      The integration is not built yet (phase 9.1). To proceed anyway, pass
      --defer-truveon, which records a declared, owned, dated override against
      every account rather than skipping the step silently.

      Note that the accounts above may already exist. Re-running is safe."
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

EVIDENCE="$("${PY}" - "${QPATH}" "${CREATED_IDS}" "${DEFER_TRUVEON}" <<'PYEOF'
import hashlib, json, sys, datetime, pathlib
qpath, ids, defer = sys.argv[1], sys.argv[2], sys.argv[3]
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
    "truveon_evidence_verified": defer != "1",
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
  AWS_PROFILE= AWS_ACCESS_KEY_ID="${EAK}" AWS_SECRET_ACCESS_KEY="${ESK}" AWS_SESSION_TOKEN="${EST}" \
    MSYS_NO_PATHCONV=1 aws s3api put-object \
      --bucket "altdig-log-archive-${LOG_ARCHIVE_ACCT}" \
      --key "${EVIDENCE_KEY}" --body "$(win_path "${TMP}")" \
      --content-type application/json >/dev/null
  rm -f "${TMP}"
  ok "evidence written to s3://altdig-log-archive-${LOG_ARCHIVE_ACCT}/${EVIDENCE_KEY}"
  ok "immutable for ${RETENTION} days; replicated to the us-west-2 archive"
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
hr
