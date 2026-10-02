#!/usr/bin/env bash
#
# Grant a tenant's developers access to its member accounts.
#
#   scripts/grant-developer-access.sh --roster r.yaml --questionnaire q.yaml \
#     [--dry-run]
#
# Questionnaire section 3b, applied. Four things happen, in order, and every
# one of them is idempotent:
#
#   1. One Identity Center group per member account, plus one read-only
#      group for the whole tenant, created if absent.
#   2. Each person on the roster created in the identity store if absent.
#   3. Membership reconciled — people added to the groups their roster row
#      lists, and nobody removed. See below.
#   4. identity/20-developer-assignments.yaml binds each developer group to
#      the permission set design 11's gradient calls for, and the read-only
#      group to TenantReadOnly in every account.
#
# Steps 1-3 are not CloudFormation, for the reasons
# scripts/deploy-commercial-access.sh gives for the same split: the groups must
# survive the eventual switch of the identity source to Entra as parameters
# rather than as managed resources, and membership is personal data which the
# repository holds none of.
#
# ---------------------------------------------------------------------------
# Nobody is ever removed by this script
# ---------------------------------------------------------------------------
# A person in a group but not on the roster is REPORTED and left alone.
#
# Removing access is a leaver action. Making it a side effect of running a
# deploy with a stale local file is how somebody loses access in the middle of
# an incident because a colleague's working copy was three weeks old. The
# report names them; a human decides.
#
# This is the same policy deploy-commercial-access.sh states, and it is worth
# being consistent about: if one access script removes and another does not,
# nobody can remember which.
#
# ---------------------------------------------------------------------------
# Why the accounts are resolved by root email
# ---------------------------------------------------------------------------
# Member account IDs are not published to SSM — only the platform accounts are
# — so there is no /org/account/<alias> to read. The two available sources are
# the registry, which needs an assume-role hop into Tooling, and Organizations.
#
# This uses Organizations, keyed on the root email, because the email is
# DERIVED from the same three slugs the roster carries (account_email in
# lib/common.sh). An account whose root email matches is that tenant's account
# by construction, so the lookup cannot resolve to somebody else's account the
# way a name or tag match could.
#
# It is also exactly the check vest-client.sh uses for its own idempotency, so
# the two agree about what "this account already exists" means.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ROSTER=""; QUESTIONNAIRE=""

usage() {
  cat <<'USAGE'
Usage: grant-developer-access.sh --roster <path> --questionnaire <path>
                                 [--dry-run]

  --roster          Developer roster sidecar, YAML. Validated and derived by
                    scripts/derive-roster.py before anything is created.
  --questionnaire   The completed questionnaire this roster belongs to. The
                    roster's tenant block must match its identity block.
  --dry-run         Print every group, user and assignment that would be
                    created. Creates nothing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --roster)        ROSTER="$2"; shift 2 ;;
    --questionnaire) QUESTIONNAIRE="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${ROSTER}" ]]        || { usage; die "--roster is required."; }
[[ -n "${QUESTIONNAIRE}" ]] || { usage; die "--questionnaire is required."; }
[[ -f "${ROSTER}" ]]        || die "Not found: ${ROSTER}"
[[ -f "${QUESTIONNAIRE}" ]] || die "Not found: ${QUESTIONNAIRE}"

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

PY="$(command -v python || command -v python3)" || die "python not found."

# win_path for every python argument. Windows python cannot open an MSYS path:
# /c/AltDigital/... arrives as C:\c\AltDigital\... and fails with a bare
# "No such file or directory" naming a path nobody typed. See vest-client.sh.
DERIVE_PY="$(win_path "${REPO_ROOT}/scripts/derive-roster.py")"
RPATH="$(win_path "${ROSTER}")"
QPATH="$(win_path "${QUESTIONNAIRE}")"

# ---------------------------------------------------------------------------
# 1. Derive, which is also where the roster is validated
# ---------------------------------------------------------------------------
# Run twice on purpose: once for the human-readable report, which an operator
# reads before approving, and once for JSON. The alternative — deriving once
# and rendering both — would mean the report and the data could disagree.
info "Validating the roster against the questionnaire"
"${PY}" "${DERIVE_PY}" --roster "${RPATH}" --questionnaire "${QPATH}" \
  || die "The roster cannot be applied. Nothing was created."

DERIVED="$("${PY}" "${DERIVE_PY}" --roster "${RPATH}" \
  --questionnaire "${QPATH}" --json)" || die "Derivation failed."

d_get() {
  printf '%s' "${DERIVED}" \
    | "${PY}" -c "import sys,json;print(eval('json.load(sys.stdin)'+sys.argv[1]))" "$1"
}

STEM="$(d_get "['tenant_stem']")"
PARTNER="$(d_get "['partner']")"
CLIENT="$(d_get "['client_slug']")"
APP="$(d_get "['application_code']")"
ENVS="$(printf '%s' "${DERIVED}" \
  | "${PY}" -c "import sys,json;print(' '.join(json.load(sys.stdin)['environments']))")"

hr
log "Developer access"
log "  tenant      : ${PARTNER}/${CLIENT}/${APP}"
log "  environments: ${ENVS}"
log "  mode        : $([[ "${DRY_RUN}" == "1" ]] && echo DRY-RUN || echo EXECUTE)"
hr

# ---------------------------------------------------------------------------
# 2. Identity Center
# ---------------------------------------------------------------------------
identity_center_init
ok "Identity Center ${INSTANCE_ARN}"
ok "Identity store ${IDENTITY_STORE_ID}"

# ---------------------------------------------------------------------------
# 3. Resolve the member accounts
# ---------------------------------------------------------------------------
# ALL of them before anything is created. An environment whose account does not
# exist must stop the run rather than be discovered after two groups have been
# populated — the same reasoning resolve_accounts uses in lib/common.sh.
#
# The likely cause of a miss is simply that vesting has not run yet, so the
# message says so instead of reporting a lookup failure.
hr
info "Resolving member accounts"
declare -A ACCOUNT_OF
MISSING=""
for ENVNAME in ${ENVS}; do
  EMAIL="$(account_email "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}")"
  ACCT="$(MSYS_NO_PATHCONV=1 aws organizations list-accounts \
    --query "Accounts[?Email=='${EMAIL}'].Id" --output text 2>/dev/null | no_cr || true)"
  if [[ -z "${ACCT}" || "${ACCT}" == "None" ]]; then
    MISSING="${MISSING} ${ENVNAME}"
    warn "${ENVNAME}: no account with root email ${EMAIL}"
    continue
  fi
  ACCOUNT_OF["${ENVNAME}"]="${ACCT}"
  ok "${ENVNAME} = ${ACCT}  ($(account_alias "${PARTNER}" "${CLIENT}" "${APP}" "${ENVNAME}"))"
done

if [[ -n "${MISSING}" ]]; then
  # A dry run WARNS where a real run dies, and the difference is not laziness.
  #
  # Run as part of onboard-client.sh, the vesting step immediately before this
  # one also dry-ran and therefore created nothing — so in a rehearsal these
  # accounts cannot exist yet, by construction. Dying here would make the
  # rehearsal useless for exactly the case it is most needed: seeing the whole
  # sequence before running any of it.
  #
  # A real run still dies. There the accounts genuinely should exist, and
  # granting access to an account that does not is not something to warn about.
  if [[ "${DRY_RUN}" == "1" ]]; then
    warn "No member account exists yet for:${MISSING}"
    warn "  Expected in a rehearsal — vesting also dry-ran and created nothing."
    warn "  Continuing so the groups and membership below can be reviewed. The"
    warn "  assignment stack is skipped: it needs real account ids."
  else
    die "No member account exists for:${MISSING}

      Access cannot be granted to an account that has not been vested. This is
      almost always ordering rather than a fault — run vesting first:

        scripts/vest-client.sh --questionnaire ${QUESTIONNAIRE} --dry-run
        scripts/vest-client.sh --questionnaire ${QUESTIONNAIRE} --confirm-vest

      Nothing was created."
  fi
fi

# ---------------------------------------------------------------------------
# 4. Groups, users and membership
# ---------------------------------------------------------------------------

trim() { printf '%s' "$1" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'; }

# reconcile_group <group name> <description> <--members key> <label>
#
# Create the group if absent, create each member if absent, add each to the
# group, and report anyone in the group who is not on the roster. Sets GID.
#
# One function for both kinds of group -- the per-account developer groups and
# the tenant-wide read-only group -- because the leaver policy in the header
# must be identical for both, and two copies of this loop would agree only
# until one of them was edited.
reconcile_group() {
  local GROUP_NAME="$1" DESCRIPTION="$2" KEY="$3" LABEL="$4"
  local MEMBERS record GIVEN FAMILY EMAIL UNAME UID_ CONFIGURED member_username

  GID="$(group_id_by_name "${GROUP_NAME}")"
  if [[ -n "${GID}" && "${GID}" != "None" ]]; then
    ok "group exists (${GID})"
  elif [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s DRY%s  create identity store group %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${GROUP_NAME}"
    GID=""
  else
    GID="$(aws identitystore create-group \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --display-name "${GROUP_NAME}" \
      --description "${DESCRIPTION}" \
      --query GroupId --output text | no_cr)"
    ok "group created (${GID})"
  fi

  # Members. derive-roster.py emits the same pipe-delimited record format
  # config/identity.env uses, so there is one parser for both rather than two
  # that agree until one is edited.
  MEMBERS="$("${PY}" "${DERIVE_PY}" --roster "${RPATH}" \
    --questionnaire "${QPATH}" --members "${KEY}")"

  if [[ -z "${MEMBERS}" ]]; then
    skip "nobody on the roster reaches ${LABEL}"
    return 0
  fi

  while read -r record; do
    record="$(trim "${record}")"
    [[ -n "${record}" ]] || continue

    GIVEN="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f1)")"
    FAMILY="$(trim "$(printf '%s' "${record}" | cut -d'|' -f2)")"
    EMAIL="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f3)")"
    UNAME="$(trim "$(printf '%s' "${record}"  | cut -d'|' -f4)")"

    # derive-roster.py has already validated all four fields. This is a guard
    # against the RECORD FORMAT breaking — a name containing a pipe would shift
    # every field left and create a user with somebody else's email.
    [[ -n "${GIVEN}" && -n "${FAMILY}" && -n "${EMAIL}" && -n "${UNAME}" ]] \
      || die "Malformed member record for ${LABEL}: ${record}
      Expected GivenName|FamilyName|email|userName."

    # Looked up by userName, which is the identity store's unique attribute.
    # The email is an attribute OF the user, not the key — for anyone carrying
    # a user_name override the two differ, and searching on the email finds
    # nothing and creates a duplicate human.
    UID_="$(user_id_by_name "${UNAME}")"

    if [[ -z "${UID_}" || "${UID_}" == "None" ]]; then
      if [[ "${DRY_RUN}" == "1" ]]; then
        printf '%s DRY%s  create identity store user %s\n' \
          "${C_YELLOW}" "${C_RESET}" "${UNAME}"
        continue
      fi
      UID_="$(aws identitystore create-user \
        --identity-store-id "${IDENTITY_STORE_ID}" \
        --user-name "${UNAME}" \
        --display-name "${GIVEN} ${FAMILY}" \
        --name "GivenName=${GIVEN},FamilyName=${FAMILY}" \
        --emails "Value=${EMAIL},Type=work,Primary=true" \
        --query UserId --output text | no_cr)"
      ok "user created ${UNAME}"
    fi

    if [[ -n "${GID}" ]] && is_member "${GID}" "${UID_}"; then
      ok "${UNAME} already in ${GROUP_NAME}"
      continue
    fi

    if [[ "${DRY_RUN}" == "1" ]]; then
      printf '%s DRY%s  add %s to %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${UNAME}" "${GROUP_NAME}"
      continue
    fi

    aws identitystore create-group-membership \
      --identity-store-id "${IDENTITY_STORE_ID}" \
      --group-id "${GID}" --member-id "UserId=${UID_}" \
      --query MembershipId --output text >/dev/null
    ok "${UNAME} added to ${GROUP_NAME}"
  done <<< "${MEMBERS}"

  # Leavers. Reported, never removed — see the header.
  if [[ -n "${GID}" && "${DRY_RUN}" != "1" ]]; then
    CONFIGURED="$(printf '%s\n' "${MEMBERS}" | cut -d'|' -f4)"
    while read -r member_username; do
      [[ -n "${member_username}" ]] || continue
      # -F -x: the whole line, literally. A substring match would treat
      # a.person@example.com as covering not.a.person@example.com.
      if ! printf '%s\n' "${CONFIGURED}" | grep -Fxq -- "${member_username}"; then
        warn "In ${GROUP_NAME} but not on the roster: ${member_username}"
        warn "  Not removed. Remove deliberately if this is a leaver."
      fi
    done < <(group_member_usernames "${GID}")
  fi
}

declare -A GROUP_OF

for ENVNAME in ${ENVS}; do
  GROUP_NAME="$(d_get "['groups']['${ENVNAME}']['group_name']")"
  GRANT="$(d_get "['groups']['${ENVNAME}']['permission_set']")"

  hr
  info "${ENVNAME} — group ${GROUP_NAME} (${GRANT})"
  reconcile_group "${GROUP_NAME}" \
    "Developer access to ${ENVNAME} for ${PARTNER}/${CLIENT}/${APP} (design 11)" \
    "${ENVNAME}" "${ENVNAME}"
  GROUP_OF["${ENVNAME}"]="${GID}"
done

# The read-only group: one for the tenant, assigned to every account. Always
# reconciled, because derive-roster.py refuses a roster without the
# development manager on it -- so this group is never legitimately empty.
RO_GROUP_NAME="$(d_get "['readonly']['group_name']")"
hr
info "every environment — group ${RO_GROUP_NAME} (tenant-readonly)"
reconcile_group "${RO_GROUP_NAME}" \
  "Read-only oversight of every environment for ${PARTNER}/${CLIENT}/${APP} (design 11)" \
  "readonly" "the read-only group"
RO_GROUP="${GID}"

# ---------------------------------------------------------------------------
# 5. Assignments
# ---------------------------------------------------------------------------
# UAT is passed as a pair or not at all — the template's HasUat condition
# requires both, so that a half-supplied election fails here with a message
# rather than at create time with one that names neither.

hr
STACK_NAME="platform-identity-dev-${STEM}"

UAT_GROUP=""; UAT_ACCOUNT=""
if [[ " ${ENVS} " == *" uat "* ]]; then
  UAT_GROUP="${GROUP_OF[uat]:-}"
  UAT_ACCOUNT="${ACCOUNT_OF[uat]:-}"
fi

# The stack needs a real group id AND a real account id for every environment.
# In a dry run before either exists it cannot be changeset-tested at all —
# both are required parameters with AllowedPatterns that an empty string fails.
# Said plainly rather than emitting a CloudFormation error that names a
# parameter rather than a cause; deploy-commercial-access.sh reports the same
# limitation for the same reason.
MISSING_IDS=""
for ENVNAME in ${ENVS}; do
  [[ -n "${GROUP_OF[${ENVNAME}]:-}" ]]   || MISSING_IDS="${MISSING_IDS} ${ENVNAME}(group)"
  [[ -n "${ACCOUNT_OF[${ENVNAME}]:-}" ]] || MISSING_IDS="${MISSING_IDS} ${ENVNAME}(account)"
done
[[ -n "${RO_GROUP:-}" ]] || MISSING_IDS="${MISSING_IDS} readonly(group)"

if [[ -n "${MISSING_IDS}" ]]; then
  skip "stack ${STACK_NAME} — not yet resolvable:${MISSING_IDS}"
  log ""
  log "  The assignment stack cannot be changeset-tested until both the groups"
  log "  and the accounts exist. Vest the accounts, re-run without --dry-run to"
  log "  create the groups, then dry-run again to see the assignments."
else
  # The UAT pair is OMITTED rather than passed empty when there is no UAT.
  # `Key=` with nothing after it is accepted by the CLI's shorthand parser in
  # some versions and rejected in others, and the template already declares
  # Default: '' for both — so letting the default apply is both shorter and
  # the only form that cannot depend on a CLI version. Passed as a pair or not
  # at all, which is what the HasUat condition requires.
  PARAMS=(
    "InstanceArn=${INSTANCE_ARN}"
    "TenantStem=${STEM}"
    "DevGroupId=${GROUP_OF[dev]}"
    "TestGroupId=${GROUP_OF[test]}"
    "ProdGroupId=${GROUP_OF[prod]}"
    "DevAccountId=${ACCOUNT_OF[dev]}"
    "TestAccountId=${ACCOUNT_OF[test]}"
    "ProdAccountId=${ACCOUNT_OF[prod]}"
    "ReadOnlyGroupId=${RO_GROUP}"
  )
  if [[ -n "${UAT_GROUP}" && -n "${UAT_ACCOUNT}" ]]; then
    PARAMS+=("UatGroupId=${UAT_GROUP}" "UatAccountId=${UAT_ACCOUNT}")
  elif [[ -n "${UAT_GROUP}" || -n "${UAT_ACCOUNT}" ]]; then
    die "Half a UAT election: group='${UAT_GROUP}' account='${UAT_ACCOUNT}'.
      The template needs both or neither. Nothing was deployed."
  fi

  cfn_deploy "${STACK_NAME}" "${REPO_ROOT}/identity/20-developer-assignments.yaml" \
    "${PARAMS[@]}"
fi

hr
if [[ "${DRY_RUN}" == "1" ]]; then
  ok "Dry run complete — nothing created."
else
  ok "Developer access granted for ${PARTNER}/${CLIENT}/${APP}"
  log ""
  log "Access portal: https://altdigital.awsapps.com/start"
  log ""
  log "Have one person confirm the gradient actually holds. In dev this must"
  log "succeed and in prod it must be denied:"
  log "  aws ecs list-clusters"
  log "  aws secretsmanager list-secrets          # names, allowed everywhere"
  log "  aws secretsmanager get-secret-value --secret-id any   # DENIED in prod"
  log ""
  log "And as TenantReadOnly, in any account, the write must be refused:"
  log "  aws ecs list-clusters                    # allowed"
  log "  aws logs create-log-group --log-group-name probe   # DENIED"
  log ""
  log "A grant that reads correctly and does not hold is the failure this"
  log "cannot detect from here — design 11's gradient is only real once"
  log "somebody has signed in and been refused."
fi
hr
