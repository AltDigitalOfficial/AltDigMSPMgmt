#!/usr/bin/env bash
#
# The Truveon side of onboarding a client: one tenant per client (B-023).
#
#   scripts/truveon-tenant-handoff.sh prepare  --questionnaire Q [--truveon-roster R]
#   scripts/truveon-tenant-handoff.sh store-key --partner P --client C --truveon-tenant T  < key
#   scripts/truveon-tenant-handoff.sh status   --partner P --client C
#
# ---------------------------------------------------------------------------
# Why this is a handoff and not an API call
# ---------------------------------------------------------------------------
# Truveon deliberately has no create-tenant API (Truveon design D-26). Creating
# a tenant is DDL -- a schema and a role -- and the application's database role
# is built never to hold DDL rights; a test asserts it. An API that created
# tenants would need a privileged path nothing in the running stack has, by
# design, plus a caller identity for "a reseller's automation" that Truveon's
# organisation model does not have yet. D-26 names the triggers for building
# that. At today's volume a person running Truveon's CLI is not the
# bottleneck, so this makes that person's run exact and repeatable instead.
#
# `prepare` writes a pack under completed_qs/ (gitignored as a directory):
# the manifest, the CSVs, the exact commands, and a Claude Code prompt for the
# Truveon repository. Only users.csv holds personal data; the prompt and the
# commands refer to it by path and never reproduce it, because they are the
# files most likely to end up in an assistant's context.
#
# `store-key` takes the ingest key Truveon mints, from a PIPE, and puts it in
# the Audit account's Secrets Manager at platform/truveon/ingest/<partner>-<client>,
# where the evidence sender looks for it. It never echoes the key. It then links
# the Truveon tenant to the client in the registry.
#
# `status` answers "is this client's evidence able to reach Truveon" from the
# platform side: is the tenant linked, and does the key exist. It does not read
# the key and does not call Truveon -- first-evidence receipt is the
# verification sweep's question, asked from the Audit account where the key
# already lives, not from a laptop.
#
# ---------------------------------------------------------------------------
# Two passes, like PagerDuty
# ---------------------------------------------------------------------------
# onboard-client.sh runs `prepare` twice. Before vesting there are no accounts,
# so the first pack creates and configures the tenant and mints the key with
# the pipeline account as its only member account. After vesting the pack is
# regenerated with the new accounts, and only the configure step is re-run.
# Truveon's manifest apply is idempotent, so the second pass changes only what
# is new.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  cat <<'USAGE'
Usage: truveon-tenant-handoff.sh <command> [options]

  prepare    --questionnaire Q [--truveon-roster R] [--dry-run]
             Write the handoff pack to completed_qs/<partner>-<client>.truveon/.
             The roster is required when the tenant is being created.

  prepare-platform [--dry-run]
             The same pack for AltDigital's own estate, into Truveon's
             existing 'altdigital' tenant. Accounts from SSM; no roster.

  store-key  --partner P --client C --truveon-tenant T [--dry-run]
             Read a Truveon ingest key from stdin (a pipe, or typed with no
             echo) and store it for the evidence sender. Then link the tenant
             in the registry.

  status     --partner P --client C
             Exit 0 only if the tenant is linked and its key is stored.
USAGE
}

[[ $# -gt 0 ]] || { usage; exit 1; }
CMD="$1"; shift

QUESTIONNAIRE=""; ROSTER=""; PARTNER=""; CLIENT=""; TRUVEON_TENANT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --questionnaire)   QUESTIONNAIRE="$2"; shift 2 ;;
    --truveon-roster)  ROSTER="$2"; shift 2 ;;
    --partner)         PARTNER="$2"; shift 2 ;;
    --client)          CLIENT="$2"; shift 2 ;;
    --truveon-tenant)  TRUVEON_TENANT="$2"; shift 2 ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

SECRET_PREFIX="platform/truveon/ingest"
export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
PY="$(command -v python || command -v python3)" || die "python not found."

audit_account() {
  local id
  id="$(get_param /org/account/altdig-security-audit)"
  [[ -n "${id}" && "${id}" != "None" ]] || die "Audit account not registered in SSM."
  printf '%s' "${id}"
}

# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
cmd_prepare() {
  [[ -n "${QUESTIONNAIRE}" && -f "${QUESTIONNAIRE}" ]] \
    || die "--questionnaire is required and must exist."
  [[ -z "${ROSTER}" || -f "${ROSTER}" ]] || die "Not found: ${ROSTER}"
  require_cli
  require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

  local qpath partner client derived_file reg_file audit out summary
  qpath="$(win_path "${QUESTIONNAIRE}")"
  partner="$("${PY}" -c "import sys,yaml;print(yaml.safe_load(open(sys.argv[1],encoding='utf-8'))['identity']['partner'])" "${qpath}")"
  client="$("${PY}" -c "import sys,yaml;print(yaml.safe_load(open(sys.argv[1],encoding='utf-8'))['identity']['client_slug'])" "${qpath}")"

  # The pack holds users.csv, which is personal data. It goes under
  # completed_qs/ and nowhere else -- not configurable, because the one time
  # it is pointed somewhere convenient is the time it gets committed.
  out="${REPO_ROOT}/completed_qs/${partner}-${client}.truveon"

  derived_file="$(mktemp)"; reg_file="$(mktemp)"
  trap 'rm -f "${derived_file}" "${reg_file}"' RETURN

  "${PY}" "$(win_path "${REPO_ROOT}/scripts/derive-vesting.py")" \
    --questionnaire "${qpath}" --json >"${derived_file}" 2>/dev/null \
    || die "The questionnaire does not derive. Run scripts/derive-vesting.py --questionnaire ${QUESTIONNAIRE}."

  # Before vesting this lists no accounts for the client, which is expected.
  # A FAILED read is different and stops: a pack generated from an unreadable
  # registry would register no accounts and look like a pre-vesting pack.
  bash "${REPO_ROOT}/scripts/registry.sh" list-accounts >"${reg_file}" 2>/dev/null \
    || die "Could not read the account registry."

  audit="$(audit_account)"

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  would write the Truveon pack for %s/%s to %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${partner}" "${client}" "${out}"
  fi

  local target="${out}"
  [[ "${DRY_RUN}" == "1" ]] && target="$(mktemp -d)"
  summary="$("${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/truveon_handoff.py")" \
    --questionnaire "${qpath}" \
    ${ROSTER:+--roster "$(win_path "${ROSTER}")"} \
    --derived "$(win_path "${derived_file}")" \
    --registry-accounts "$(win_path "${reg_file}")" \
    --pipeline-account "${audit}" \
    --out "$(win_path "${target}")" \
    --pack-win "$(win_path "${out}")" \
    --msp-win "$(win_path "${REPO_ROOT}")" \
    --msp-profile "${PLATFORM_AWS_PROFILE}" \
    --msp-account "${PLATFORM_MGMT_ACCOUNT_ID}")" \
    || die "The Truveon pack could not be built. Nothing was written that should be used."
  [[ "${DRY_RUN}" == "1" ]] && rm -rf "${target}"

  hr
  log "Truveon handoff pack — ${partner}/${client}"
  printf '%s' "${summary}" | "${PY}" -c "
import json,sys
s=json.load(sys.stdin)
print(f\"  truveon tenant : {s['truveon_tenant']} ({'CREATE' if s['creating'] else 'existing - configure only'})\")
print(f\"  accounts       : {s['member_accounts']} member + the pipeline account\")
print(f\"  users          : {s['users']}   legal entities: {s['legal_entities']}\")
print(f\"  retention floor: {s['retention_floor_days']} days   notification windows: {s['notification_windows']}\")
"
  log "  pack           : ${out}"
  hr
  if [[ "${DRY_RUN}" != "1" ]]; then
    info "Hand it over, one of two ways:"
    log "  a) open a Claude Code session in the Truveon repository and give it"
    log "     ${out}/PROMPT.md"
    log "  b) run ${out}/commands.sh by hand from Truveon's infra/scripts"
    log ""
    log "  users.csv is personal data. Do not paste it, open it in an assistant,"
    log "  or copy the pack out of completed_qs/."
  fi
}

# ---------------------------------------------------------------------------
# prepare-platform
# ---------------------------------------------------------------------------
# AltDigital's own estate, into Truveon's existing `altdigital` tenant (Jamie,
# 2026-09-23). No questionnaire and no roster: the accounts come from SSM, the
# same list sync-evidence-tenants.sh files under altdigital-platform, and the
# other values are stated in truveon_handoff.py build_platform_manifest with
# their reasons.
cmd_prepare_platform() {
  require_cli
  require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
  local out accts_file reg_file audit summary
  out="${REPO_ROOT}/completed_qs/altdigital-platform.truveon"
  accts_file="$(mktemp)"; reg_file="$(mktemp)"
  trap 'rm -f "${accts_file}" "${reg_file}"' RETURN

  # Same sources as sync-evidence-tenants.sh, so the accounts Truveon expects
  # are exactly the accounts the pipeline files under this tenant.
  bash "${REPO_ROOT}/scripts/registry.sh" list-accounts >"${reg_file}" 2>/dev/null \
    || die "Could not read the account registry."

  MSYS_NO_PATHCONV=1 aws ssm get-parameters-by-path --path /platform/org/account/ \
    --query 'Parameters[].[Name,Value]' --output text 2>/dev/null | no_cr >"${accts_file}"
  [[ -s "${accts_file}" ]] || die "No platform accounts found under /platform/org/account/."
  audit="$(audit_account)"

  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  would write the platform Truveon pack to %s\n' "${C_YELLOW}" "${C_RESET}" "${out}"
    out="$(mktemp -d)"
  fi
  summary="$("${PY}" "$(win_path "${REPO_ROOT}/scripts/lib/truveon_handoff.py")" \
    --platform-accounts "$(win_path "${accts_file}")" \
    --registry-accounts "$(win_path "${reg_file}")" \
    --pipeline-account "${audit}" \
    --out "$(win_path "${out}")" \
    --pack-win "$(win_path "${REPO_ROOT}/completed_qs/altdigital-platform.truveon")" \
    --msp-win "$(win_path "${REPO_ROOT}")" \
    --msp-profile "${PLATFORM_AWS_PROFILE}" \
    --msp-account "${PLATFORM_MGMT_ACCOUNT_ID}")" \
    || die "The platform pack could not be built."
  [[ "${DRY_RUN}" == "1" ]] && rm -rf "${out}"

  hr
  log "Truveon handoff pack — AltDigital platform -> tenant 'altdigital'"
  printf '%s' "${summary}" | "${PY}" -c "
import json,sys
s=json.load(sys.stdin)
print(f\"  accounts       : {s['member_accounts']} platform + the pipeline account\")
print(f\"  retention floor: {s['retention_floor_days']} days\")
"
  log "  pack           : ${REPO_ROOT}/completed_qs/altdigital-platform.truveon"
  hr
}

# ---------------------------------------------------------------------------
# store-key
# ---------------------------------------------------------------------------
cmd_store_key() {
  [[ -n "${PARTNER}" && -n "${CLIENT}" ]] || die "--partner and --client are required."
  [[ -n "${TRUVEON_TENANT}" ]] || die "--truveon-tenant is required: the key is checked against it."
  require_cli
  require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

  local key=""
  if [[ -t 0 ]]; then
    # Typed by hand. Allowed, not encouraged: a key that passed through a
    # clipboard has been somewhere this script cannot see.
    printf 'Truveon ingest key (input hidden): ' >&2
    IFS= read -rs key; printf '\n' >&2
  else
    IFS= read -r key || true
  fi
  key="${key%$'\r'}"
  key="${key//[[:space:]]/}"

  # Checked by SHAPE and by TENANT, and the message names neither the key nor
  # any part of the secret. A key for the wrong tenant stored under this
  # client's name would send this client's evidence into another client's
  # Truveon -- Truveon trusts the key, not the body.
  [[ -n "${key}" ]] || die "No key arrived on stdin. Nothing was stored.
      If this was piped from 87-apply-msp-manifest.sh, that step failed -- read its output above."
  if [[ ! "${key}" =~ ^truveon_ing\.([a-z0-9-]+)\.[A-Za-z0-9_-]{20,}$ ]]; then
    key=""
    die "What arrived is not a Truveon ingest key (expected truveon_ing.<tenant>.<secret>). Nothing was stored."
  fi
  local key_tenant="${BASH_REMATCH[1]}"
  if [[ "${key_tenant}" != "${TRUVEON_TENANT}" ]]; then
    key=""
    die "The key belongs to Truveon tenant '${key_tenant}', not '${TRUVEON_TENANT}'. Nothing was stored.
      Storing it would route ${PARTNER}/${CLIENT}'s evidence into another client's tenant."
  fi

  local audit name payload
  audit="$(audit_account)"
  name="${SECRET_PREFIX}/${PARTNER}-${CLIENT}"
  payload="$(printf '%s' "${key}" | "${PY}" -c 'import json,sys;print(json.dumps({"key":sys.stdin.read()}))')"
  key=""

  hr
  log "Truveon ingest key for ${PARTNER}/${CLIENT}"
  log "  truveon tenant: ${TRUVEON_TENANT}"
  log "  secret        : ${name} in altdig-security-audit (${audit})"
  hr
  store_secret_everywhere "${name}" \
    "Truveon ingest key for platform tenant ${PARTNER}-${CLIENT} (Truveon tenant ${TRUVEON_TENANT}). Read by the evidence sender. docs/truveon-ingestion-contract.md" \
    "${payload}" "altdig-security-audit:${audit}"
  payload=""

  if [[ "${PARTNER}" == "altdigital" ]]; then
    # AltDigital's own estate has no client record to link; the internal
    # evidence tenant is named in scripts/lib/evidence_tenants.py.
    skip "altdigital/${CLIENT} is the platform's own tenant — no registry client record to link"
  else
    bash "${REPO_ROOT}/scripts/registry.sh" link-truveon \
      --partner "${PARTNER}" --slug "${CLIENT}" --name "${TRUVEON_TENANT}" \
      $([[ "${DRY_RUN}" == "1" ]] && echo --dry-run) \
      || die "The key is stored but the registry link failed. Re-run store-key, or:
      scripts/registry.sh link-truveon --partner ${PARTNER} --slug ${CLIENT} --name ${TRUVEON_TENANT}"
  fi
  hr
  info "Evidence for ${PARTNER}-${CLIENT} will deliver on the sender's next attempt;"
  log "  records buffered before now are replayed within the hour."
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
cmd_status() {
  [[ -n "${PARTNER}" && -n "${CLIENT}" ]] || die "--partner and --client are required."
  require_cli
  require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

  local audit name linked="" stored="" problems=0 item
  audit="$(audit_account)"
  name="${SECRET_PREFIX}/${PARTNER}-${CLIENT}"

  if [[ "${PARTNER}" != "altdigital" ]]; then
    item="$(bash "${REPO_ROOT}/scripts/registry.sh" get-client \
              --partner "${PARTNER}" --slug "${CLIENT}" 2>/dev/null)" || item=""
    linked="$(printf '%s' "${item}" | "${PY}" -c "
import json,sys
try: print((json.load(sys.stdin) or {}).get('truveon_tenant_id',{}).get('S',''))
except Exception: print('')
")"
  fi

  # describe-secret, never get-secret-value: this answers whether the key
  # exists, and has no reason to hold it.
  stored="$(in_account "${audit}" aws secretsmanager describe-secret \
              --secret-id "${name}" --query 'LastChangedDate' --output text 2>/dev/null \
            | no_cr)" || stored=""

  hr
  log "Truveon link — ${PARTNER}/${CLIENT}"
  if [[ "${PARTNER}" == "altdigital" ]]; then
    log "  registry : n/a (platform's own tenant)"
  elif [[ -n "${linked}" ]]; then
    ok  "  registry : linked to Truveon tenant '${linked}'"
  else
    warn "  registry : NOT linked"; problems=$((problems + 1))
  fi
  if [[ -n "${stored}" && "${stored}" != "None" ]]; then
    ok  "  key      : ${name} present (last changed ${stored})"
  else
    warn "  key      : ${name} ABSENT — evidence for this tenant stays pending"
    problems=$((problems + 1))
  fi
  hr
  log "  Not checked here: whether Truveon has RECEIVED evidence. That is the"
  log "  verification sweep's question (docs/verification-sweep.md check 1)."
  [[ ${problems} -eq 0 ]]
}

case "${CMD}" in
  prepare)   cmd_prepare ;;
  prepare-platform) cmd_prepare_platform ;;
  store-key) cmd_store_key ;;
  status)    cmd_status ;;
  -h|--help|help) usage ;;
  *) usage; die "Unknown command: ${CMD}" ;;
esac
