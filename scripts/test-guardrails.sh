#!/usr/bin/env bash
#
# Guardrail test harness — prove the SCPs actually deny what they claim to.
#
# Open item V1 is the load-bearing assumption this exists to test:
#   "a member account with full local administrator cannot remove detective
#    controls"
#
# It breaks quietly. An SCP that has been edited, detached, or shadowed by an
# exclusion still appears attached in the console and still reads as
# protective. The only way to know is to attempt the denied action and look at
# what comes back.
#
# Usage:
#   scripts/test-guardrails.sh --account <id> [--region us-east-2] [--json]
#
# Requires credentials in the MANAGEMENT account; it assumes into the target.
#
# ---------------------------------------------------------------------------
# Telling an SCP denial from any other failure
# ---------------------------------------------------------------------------
# This is the whole difficulty, and prompt 1.3 warns about it explicitly:
# "assert denial rather than relying on the action failing for another reason."
#
# Some services say so plainly:
#   "...with an explicit deny in a service control policy"
#
# Others do not. AWS Backup returns "Insufficient privileges to perform this
# action"; Organizations returns "You don't have permissions to access this
# resource". Both are AccessDenied with no indication of what denied them, so a
# message match alone cannot distinguish an SCP from an IAM gap or a
# service-side constraint.
#
# So every test with an exclusion runs TWICE, against two principals:
#
#   GuardrailTestAdmin    AdministratorAccess, ordinary name, matches no
#                         exclusion. Subject to every denial.
#   PlatformHarnessProbe  AdministratorAccess, named Platform*, therefore
#                         excluded by policies 01 and 02.
#
#   test DENIED + probe NOT denied  -> the SCP is what denied it.   CONFIRMED
#   test DENIED + probe DENIED      -> something else denied it, or the
#                                      statement has no exclusion.  AMBIGUOUS
#   test ALLOWED                    -> the guardrail is absent.     FAIL
#
# The probe is created by OrganizationAccountAccessRole, which policy 03
# excludes from the Platform* naming denial precisely so that platform roles
# can be created in a fresh account. That the probe can be created at all is
# itself a check on that exclusion.
#
# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
# Destructive calls target resources that do not exist, so nothing is destroyed
# whether or not the guardrail fires. The one exception is KMS: it resolves the
# key before evaluating authorization and returns NotFoundException for a
# made-up id, which tells us nothing. That test therefore uses a real key
# created for the purpose.
#
# NOTE: that key cannot be cleaned up. Scheduling its deletion is denied by the
# very policy under test, for OrganizationAccountAccessRole as well. It is a
# permanent fixture in the canary account at roughly $1/month. That is the
# correct trade — see policies/scp/README.md.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""
TEST_REGION="${PLATFORM_HOME_REGION}"
OUTPUT_JSON=0
TEST_ROLE="GuardrailTestAdmin"
PROBE_ROLE="PlatformHarnessProbe"
KEY_ALIAS="alias/guardrail-harness-fixture"

usage() {
  cat <<'USAGE'
Usage: test-guardrails.sh --account <id> [--region <region>] [--json]

  --account   Target member account id. Must NOT be the management account:
              SCPs never apply there, so a pass would be meaningless.
  --region    Region to run calls in. Defaults to the platform home region.
  --json      Emit machine-readable results for scheduled runs.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    --region)  TEST_REGION="$2"; shift 2 ;;
    --json)    OUTPUT_JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${TARGET_ACCOUNT}" ]] || { usage; die "--account is required."; }
[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || die "Account id must be 12 digits."
[[ "${TARGET_ACCOUNT}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "Refusing to test the management account. SCPs do not apply to it,
      so the result would be meaningless. Target a member account."

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
export AWS_DEFAULT_REGION="${TEST_REGION}"

assume() {
  aws sts assume-role --role-arn "$1" --role-session-name "$2" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr
}

info "Assuming OrganizationAccountAccessRole in ${TARGET_ACCOUNT}"
read -r O_KEY O_SECRET O_TOKEN <<<"$(assume \
  "arn:aws:iam::${TARGET_ACCOUNT}:role/OrganizationAccountAccessRole" guardrail-setup)"
[[ -n "${O_KEY:-}" ]] \
  || die "Could not assume OrganizationAccountAccessRole in ${TARGET_ACCOUNT}.
      That role exists only in accounts created by Organizations."

as_org() { AWS_ACCESS_KEY_ID="${O_KEY}" AWS_SECRET_ACCESS_KEY="${O_SECRET}" \
           AWS_SESSION_TOKEN="${O_TOKEN}" "$@"; }

ensure_role() {
  local role="$1"
  local trust="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${PLATFORM_MGMT_ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"
  as_org aws iam create-role --role-name "${role}" \
    --assume-role-policy-document "${trust}" \
    --description "Guardrail harness principal" >/dev/null 2>&1 || true
  as_org aws iam attach-role-policy --role-name "${role}" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess >/dev/null 2>&1 || true
}

# Clear containment from the harness's own roles before testing.
#
# The harness attempts kms:ScheduleKeyDeletion and friends to prove the SCPs
# deny them. Since B-087 those attempts reach the organization bus, containment
# declares an incident and isolates the principal -- so the subject gets
# PlatformIncidentDenyAll partway through its own run, and every later check
# fails for a reason that has nothing to do with the policy under test. B-091.
#
# Reported rather than silently repaired: finding containment here means the
# PREVIOUS run left it, which is worth saying out loud.
release_containment() {
  local role="$1" found=0 pol
  for pol in $(as_org aws iam list-role-policies --role-name "${role}"                  --query 'PolicyNames' --output text 2>/dev/null | tr -d '
'); do
    case "${pol}" in
      PlatformIncident*)
        as_org aws iam delete-role-policy --role-name "${role}"           --policy-name "${pol}" >/dev/null 2>&1 && found=1
        ;;
    esac
  done
  [[ ${found} -eq 1 ]] && warn "${role} was still CONTAINED from a previous run; containment released so the harness can test. B-091."
  return 0
}

info "Ensuring harness principals exist"
ensure_role "${TEST_ROLE}"
ensure_role "${PROBE_ROLE}"
release_containment "${TEST_ROLE}"
release_containment "${PROBE_ROLE}"

# The probe must exist for the differential to mean anything. If policy 03's
# exclusion for OrganizationAccountAccessRole is wrong, creation fails here and
# every differential result silently degrades to AMBIGUOUS — so check.
PROBE_OK=1
as_org aws iam get-role --role-name "${PROBE_ROLE}" >/dev/null 2>&1 || PROBE_OK=0
[[ ${PROBE_OK} -eq 1 ]] || warn "Probe role ${PROBE_ROLE} could not be created — \
differential checks will report AMBIGUOUS. Policy 03's OrganizationAccountAccessRole \
exclusion may be wrong."

sleep 10   # sts:AssumeRole is eventually consistent after role creation

read -r T_KEY T_SECRET T_TOKEN <<<"$(assume "arn:aws:iam::${TARGET_ACCOUNT}:role/${TEST_ROLE}" gr-test)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume ${TEST_ROLE}. IAM propagation delay?"
read -r P_KEY P_SECRET P_TOKEN <<<"$(assume "arn:aws:iam::${TARGET_ACCOUNT}:role/${PROBE_ROLE}" gr-probe)"
[[ -n "${P_KEY:-}" ]] || PROBE_OK=0

as_test()  { AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" \
             AWS_SESSION_TOKEN="${T_TOKEN}" "$@"; }
as_probe() { AWS_ACCESS_KEY_ID="${P_KEY}" AWS_SECRET_ACCESS_KEY="${P_SECRET}" \
             AWS_SESSION_TOKEN="${P_TOKEN}" "$@"; }

# --- KMS fixture -----------------------------------------------------------
# Created via the probe role, which is excluded from the KMS denial. Reused
# across runs; see the note at the top about why it cannot be cleaned up.
info "Ensuring KMS test fixture"
KEY_ID="$(as_org aws kms describe-key --key-id "${KEY_ALIAS}" \
  --query 'KeyMetadata.KeyId' --output text 2>/dev/null | no_cr | sed 's/^None$//' || true)"
if [[ -z "${KEY_ID}" ]]; then
  # Deliberately NOT tagged platform-managed. kms:CreateKey --tags requires
  # kms:TagResource, which DenyPlatformManagedTagTampering denies for any
  # principal outside its exclusion list — OrganizationAccountAccessRole
  # included. The SCP doing its job would otherwise block its own test fixture.
  # ProtectKmsKeys carries no tag condition, so the tag is not needed here.
  KEY_ID="$(as_org aws kms create-key \
    --description "Guardrail harness fixture - deletion denied by the SCP under test" \
    --tags TagKey=Purpose,TagValue=guardrail-harness \
    --query 'KeyMetadata.KeyId' --output text 2>/dev/null | no_cr || true)"
  if [[ -n "${KEY_ID}" ]]; then
    as_org aws kms create-alias --alias-name "${KEY_ALIAS}" \
      --target-key-id "${KEY_ID}" >/dev/null 2>&1 || true
  fi
fi
if [[ -n "${KEY_ID}" ]]; then ok "KMS fixture ${KEY_ID}"; else warn "No KMS fixture — that test is skipped"; fi

# --- Backup vault fixture --------------------------------------------------
# AWS Backup returns AccessDenied for a vault that does not exist, regardless
# of whether the caller is permitted — information hiding, verified against an
# excluded principal. A made-up name therefore proves nothing.
#
# Against a REAL vault the inference is sound: the subject holds
# AdministratorAccess, so if the call is denied, the denial can only come from
# an SCP. No differential probe is needed, and none is used.
#
# Unlike the KMS key this vault IS deletable by the platform, so it costs
# nothing and can be cleaned up. If the guardrail were absent the subject would
# delete it — that is the failure path, it is reported, and the vault is simply
# recreated on the next run.
VAULT_NAME="guardrail-harness-fixture"
info "Ensuring Backup vault fixture"
as_org aws backup create-backup-vault --backup-vault-name "${VAULT_NAME}" \
  >/dev/null 2>&1 || true
VAULT_OK=0
as_org aws backup describe-backup-vault --backup-vault-name "${VAULT_NAME}" \
  >/dev/null 2>&1 && VAULT_OK=1
if [[ ${VAULT_OK} -eq 1 ]]; then ok "Backup fixture ${VAULT_NAME}"; else warn "No Backup fixture — that test is skipped"; fi

# --- classification --------------------------------------------------------

denied()     { printf '%s' "$1" | grep -qiE 'AccessDenied|UnauthorizedOperation|not authorized|Insufficient privileges'; }
scp_denied() { printf '%s' "$1" | grep -qi 'service control policy'; }

PASS=0; FAIL=0; AMBIG=0; RESULTS=()

emit() {
  local verdict="$1" label="$2" ref="$3" detail="$4" colour
  case "${verdict}" in
    PASS)  colour="${C_GREEN}"; PASS=$((PASS+1)) ;;
    AMBIG) colour="${C_YELLOW}"; AMBIG=$((AMBIG+1)) ;;
    *)     colour="${C_RED}"; FAIL=$((FAIL+1)) ;;
  esac
  printf '%s%6s%s  %-34s %s\n' "${colour}" "${verdict}" "${C_RESET}" "${label}" "${ref}"
  [[ "${verdict}" == PASS ]] || printf '        %s\n' "${detail}"
  RESULTS+=("${verdict}|${ref}|${label}|${detail}")
}

# check <policy> <sid> <label> <differential:yes|no> -- <cmd...>
check() {
  local policy="$1" sid="$2" label="$3" diff_mode="$4"; shift 4
  [[ "$1" == "--" ]] && shift
  local ref="${policy}/${sid}" t_out t_rc p_out p_rc

  t_out="$(as_test "$@" 2>&1)" && t_rc=0 || t_rc=$?

  if [[ ${t_rc} -eq 0 ]]; then
    emit FAIL "${label}" "${ref}" "action SUCCEEDED — guardrail absent"; return
  fi
  if ! denied "${t_out}"; then
    emit FAIL "${label}" "${ref}" \
      "got past authorization — $(printf '%s' "${t_out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-100)"
    return
  fi
  if scp_denied "${t_out}"; then
    emit PASS "${label}" "${ref}" "denied, SCP named explicitly"; return
  fi

  # Denied, but the service did not say by what. How we resolve that depends on
  # what the test had available:
  #
  #   yes   an exclusion exists -> run the same call as the excluded probe.
  #         NEVER on a real resource: the probe is exempt, so its call SUCCEEDS
  #         and does the damage the check exists to prevent (B-137). Use `real`.
  #   real  the target resource genuinely exists, and the subject holds
  #         AdministratorAccess, so an AccessDenied can only be an SCP
  #   no    neither -> honestly ambiguous, say so
  if [[ "${diff_mode}" == "real" ]]; then
    emit PASS "${label}" "${ref}" "denied on a real resource with AdministratorAccess — only an SCP can do that"
    return
  fi
  if [[ "${diff_mode}" != "yes" ]]; then
    emit AMBIG "${label}" "${ref}" \
      "denied, but service does not name the SCP and there is no exclusion to differentiate against"
    return
  fi
  if [[ ${PROBE_OK} -ne 1 ]]; then
    emit AMBIG "${label}" "${ref}" "denied, but probe role unavailable — cannot differentiate"; return
  fi

  p_out="$(as_probe "$@" 2>&1)" && p_rc=0 || p_rc=$?
  if [[ ${p_rc} -eq 0 ]] || ! denied "${p_out}"; then
    # Excluded principal got through, non-excluded did not. That difference can
    # only be the SCP.
    emit PASS "${label}" "${ref}" "denied; excluded principal permitted — SCP confirmed by differential"
  else
    emit AMBIG "${label}" "${ref}" \
      "denied for BOTH principals — exclusion not working, or denied by something other than the SCP"
  fi
}

# expect_allowed — the negative control. Without it, a harness that cannot
# authenticate reports a clean sweep.
expect_allowed() {
  local label="$1"; shift
  [[ "$1" == "--" ]] && shift
  if as_test "$@" >/dev/null 2>&1; then
    emit PASS "${label}" "control" "permitted as expected"
  else
    emit FAIL "${label}" "control" "control action BLOCKED — harness broken or SCP over-broad"
  fi
}

NOPE="guardrail-harness-does-not-exist"

hr
log "Guardrail harness"
log "  target account : ${TARGET_ACCOUNT}"
log "  region         : ${TEST_REGION}"
log "  subject        : ${TEST_ROLE}  (admin, matches no exclusion)"
log "  probe          : ${PROBE_ROLE} (admin, named in the exemption lists)"
hr

# --- 04: required tags (prompt 9.2, design 10's four-tag set) --------------
#
# FIRST, deliberately. The 01 and 02 checks attempt kms:ScheduleKeyDeletion and
# friends, which since B-087 reach the organization bus and get the subject
# contained mid-run. Anything after them runs as a contained principal and
# fails for reasons unrelated to the policy under test. B-091.
#
# Volumes rather than buckets: cheapest to attempt, no global namespace to
# collide in, and a denied CreateVolume leaves nothing behind.
# platform-layer since B-127's statement went live. Without it this control
# fails, and the failure reads as 04 being over-broad when 04 is correct.
TAGGED='ResourceType=volume,Tags=[{Key=Owner,Value=harness},{Key=Application,Value=guardrail},{Key=DataClassification,Value=internal},{Key=platform-layer,Value=infrastructure}]'
PARTIAL='ResourceType=volume,Tags=[{Key=Owner,Value=harness}]'
BADCLASS='ResourceType=volume,Tags=[{Key=Owner,Value=harness},{Key=Application,Value=guardrail},{Key=DataClassification,Value=top-secret}]'
AZ="${TEST_REGION}a"

# THE control that matters, and the one most likely to be skipped. Every deny
# check below would also pass against a policy that denied ALL creation. Only
# this proves a correctly tagged resource still gets through.
#
# Run first so the volume it creates can serve the platform-tag check, which
# needs a REAL resource id -- a synthetic one is rejected on validation before
# authorization is ever consulted, so the check silently tested nothing.
  HARNESS_VOL="$(as_test aws ec2 create-volume --availability-zone "${AZ}" --size 1 --volume-type gp3 --region "${TEST_REGION}" --tag-specifications "${TAGGED}" --query VolumeId --output text 2>/dev/null | no_cr)" || true

if [[ "${HARNESS_VOL}" == vol-* ]]; then
  emit PASS "ec2:CreateVolume WITH all four tags" "control"     "permitted — the policy denies untagged creation, not creation"
else
  emit FAIL "ec2:CreateVolume WITH all four tags" "control"     "correctly tagged creation was BLOCKED. Read the raw error before blaming policy 04: an identity-based deny means the subject is contained (B-091), not that the SCP is over-broad"
fi

check 04 RequireOwnerTag "ec2:CreateVolume with no tags" yes --   aws ec2 create-volume --availability-zone "${AZ}" --size 1   --volume-type gp3 --region "${TEST_REGION}"

check 04 RequireApplicationTag "ec2:CreateVolume with only Owner" yes --   aws ec2 create-volume --availability-zone "${AZ}" --size 1   --volume-type gp3 --region "${TEST_REGION}"   --tag-specifications "${PARTIAL}"

check 04 DataClassificationMustBeOneOfFour "ec2:CreateVolume, invalid classification" yes --   aws ec2 create-volume --availability-zone "${AZ}" --size 1   --volume-type gp3 --region "${TEST_REGION}"   --tag-specifications "${BADCLASS}"

if [[ "${HARNESS_VOL}" == vol-* ]]; then
  check 04 ProtectPlatformManagedTags "ec2:CreateTags on a platform tag" real --     aws ec2 create-tags --region "${TEST_REGION}"     --resources "${HARNESS_VOL}" --tags Key=BackupPolicy,Value=none
  # The volume is NOT deleted here. Check 07 takes a snapshot from it, and
  # deleting it at this point left that check with nothing to test.
else
  emit AMBIG "ec2:CreateTags on a platform tag" "04/ProtectPlatformManagedTags"     "no real volume to test against — a synthetic id fails validation before authorization, which tests nothing"
fi

# --- 06: region restriction (prompt 10.2, design 11's priority) -----------
#
# design 11: "Region sprawl is the one to prioritise. It is entirely innocent
# in intent and produces a total visibility blind spot." A developer picks the
# wrong region from a dropdown and Config, GuardDuty and every alarm are simply
# not there.
#
# Tested in BOTH directions and with a global service, because the classic way
# to get this wrong is a policy that denies correctly and also breaks IAM.
DENIED_REGION="eu-west-1"

check 06 DenyOutsideApprovedRegions "ec2:DescribeVpcs in ${DENIED_REGION}" yes --   aws ec2 describe-vpcs --region "${DENIED_REGION}"

expect_allowed "ec2:DescribeVpcs in ${TEST_REGION} (approved)" --   aws ec2 describe-vpcs --region "${TEST_REGION}"

# THE one that catches the common mistake. iam, organizations, cloudfront and
# route53 are global and report whatever region the caller used. A region deny
# without a NotAction carve-out denies them everywhere, and the first symptom
# is that nobody can create a role.
expect_allowed "iam:ListRoles (global service, must survive the region deny)" --   aws iam list-roles --max-items 1

# --- 07: the bypasses design 11 enumerates --------------------------------

check 07 NoNetworkingAroundTheBaseline "ec2:CreateVpcPeeringConnection" yes --   aws ec2 create-vpc-peering-connection --region "${TEST_REGION}"   --vpc-id vpc-00000000 --peer-vpc-id vpc-11111111

check 07 NoNetworkingAroundTheBaseline "ec2:CreateInternetGateway" yes --   aws ec2 create-internet-gateway --region "${TEST_REGION}"

# RAM sharing with the organization has been on since 2026-11-01 (ADR-022),
# so a share reaches another account with no acceptance step. The probe is an
# invitation that does not exist: denied by the SCP, or, for the exempt
# probe, refused as unknown. Nothing is shared or created either way.
check 07 NoResourceSharingAcrossAccounts "ram:AcceptResourceShareInvitation" yes -- \
  aws ram accept-resource-share-invitation --region "${TEST_REGION}" \
  --resource-share-invitation-arn "arn:aws:ram:${TEST_REGION}:111111111111:resource-share-invitation/00000000-0000-0000-0000-000000000000"

# Sharing a snapshot moves data out of the account with no data-plane call --
# no GetObject, no export, nothing the evidence pipeline would see as egress.
#
# Needs a REAL snapshot. A synthetic id returns InvalidSnapshot.NotFound, which
# EC2 raises before consulting authorization, so the check passes through the
# policy without ever testing it -- the same way a synthetic volume id defeated
# the platform-tag check. Third time that has happened here; validation runs
# before authorization and a made-up resource id tests nothing.
HARNESS_SNAP=""
if [[ "${HARNESS_VOL}" == vol-* ]]; then
  HARNESS_SNAP="$(as_test aws ec2 create-snapshot --volume-id "${HARNESS_VOL}" --region "${TEST_REGION}" --description "guardrail harness, safe to delete" --query SnapshotId --output text 2>/dev/null | no_cr)" || true
fi

if [[ "${HARNESS_SNAP}" == snap-* ]]; then
  check 07 NoSnapshotOrImageSharing "ec2:ModifySnapshotAttribute" real --     aws ec2 modify-snapshot-attribute --region "${TEST_REGION}"     --snapshot-id "${HARNESS_SNAP}" --attribute createVolumePermission     --operation-type add --user-ids 999999999999
  as_test aws ec2 delete-snapshot --snapshot-id "${HARNESS_SNAP}"     --region "${TEST_REGION}" >/dev/null 2>&1 ||     warn "Could not delete harness snapshot ${HARNESS_SNAP} — remove it by hand."
else
  emit AMBIG "ec2:ModifySnapshotAttribute" "07/NoSnapshotOrImageSharing"     "no real snapshot to test against; a synthetic id fails validation before authorization and would test nothing"
fi

# Registering an identity provider is how a local administrator would create
# an external trust path the RCP no longer covers: their own OIDC issuer, plus
# a role that trusts it (B-138). `no` mode, not the differential: the probe is
# not exempt from this statement, and a differential call that SUCCEEDED would
# create a real provider. The .invalid URL cannot resolve. If the guardrail is
# ever absent, the provider is created, reported, and deleted at once.
HARNESS_OIDC_URL="https://guardrail-harness.invalid"
oidc_out="$(as_test aws iam create-open-id-connect-provider --url "${HARNESS_OIDC_URL}" \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 0000000000000000000000000000000000000000 2>&1)" && oidc_rc=0 || oidc_rc=$?
if [[ ${oidc_rc} -eq 0 ]]; then
  emit FAIL "iam:CreateOpenIDConnectProvider" "07/NoUnapprovedIdentityProviders" \
    "a non-platform principal registered an identity provider — guardrail absent"
  as_org aws iam delete-open-id-connect-provider --open-id-connect-provider-arn \
    "arn:aws:iam::${TARGET_ACCOUNT}:oidc-provider/guardrail-harness.invalid" >/dev/null 2>&1 \
    || warn "Could not delete the harness OIDC provider — remove it by hand."
elif scp_denied "${oidc_out}"; then
  emit PASS "iam:CreateOpenIDConnectProvider" "07/NoUnapprovedIdentityProviders" "denied, SCP named explicitly"
else
  emit AMBIG "iam:CreateOpenIDConnectProvider" "07/NoUnapprovedIdentityProviders" \
    "refused, but not provably by the SCP — $(printf '%s' "${oidc_out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-100)"
fi

# The harness volume has now served both the platform-tag check and the
# snapshot check, so it can go. Deleted here rather than in the 04 block,
# where removing it left check 07 with nothing to snapshot.
if [[ "${HARNESS_VOL}" == vol-* ]]; then
  as_test aws ec2 delete-volume --volume-id "${HARNESS_VOL}" \
    --region "${TEST_REGION}" >/dev/null 2>&1 || \
    warn "Could not delete harness volume ${HARNESS_VOL} - remove it by hand."
fi

# --- 05: permissions boundary (prompt 10.1, design 11's Layer 2) ----------
#
# Also ahead of the containment-triggering checks. B-091.
#
# design 11 calls this "the layer most often forgotten", and the reason is that
# forgetting it breaks nothing visibly: developers create roles, the roles
# work, and the only difference is that one of them can be more privileged than
# the developer who made it.
BOUNDARY_ARN="arn:aws:iam::${TARGET_ACCOUNT}:policy/PlatformDeveloperBoundary"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
BOUNDED_ROLE="guardrail-bounded-$$"

check 05 NoBoundaryNoRole "iam:CreateRole without a boundary" yes --   aws iam create-role --role-name "guardrail-unbounded-$$"   --assume-role-policy-document "${TRUST}"

# THE control. An SCP that denied ALL role creation would pass the check above
# and stop every developer in the estate, and nothing else here would notice.
if as_test aws iam create-role --role-name "${BOUNDED_ROLE}"      --assume-role-policy-document "${TRUST}"      --permissions-boundary "${BOUNDARY_ARN}" >/dev/null 2>&1; then
  emit PASS "iam:CreateRole WITH the boundary" "control"     "permitted — the policy requires a boundary, it does not forbid roles"
  as_test aws iam delete-role --role-name "${BOUNDED_ROLE}" >/dev/null 2>&1 ||     warn "Could not delete harness role ${BOUNDED_ROLE} — remove it by hand."
else
  emit FAIL "iam:CreateRole WITH the boundary" "control"     "a correctly bounded role was REFUSED. Either the boundary policy is not deployed to this account, or SCP 05 names a different ARN than baseline/80-developer-boundary.yaml creates — the two must agree exactly"
fi

check 05 NeverEditTheBoundaryItself "iam:CreatePolicyVersion on the boundary" real --   aws iam create-policy-version --policy-arn "${BOUNDARY_ARN}"   --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'   --set-as-default

# --- Exemption squatting: names that match an SCP exemption (B-135) -------
#
# Every SCP exempts principals by NAME. A name is only a control if nobody
# outside the platform can create a role that carries it, and 03's squatting
# deny covers exactly one shape: an unpathed role/Platform*. Three other shapes
# match an exemption and are not covered:
#
#   /xrole/PlatformSquatPath    04-07 exempt `*role/Platform*`, and the leading
#                               wildcard absorbs a path ending in "role/".
#                               03 matches `role/Platform*` only, which a path
#                               breaks.
#   stacksets-exec-squat        exempt from 01-07. Nothing denies the name.
#   OrganizationAccountAccessRoleSquat
#                               04-07 exempt `OrganizationAccountAccessRole*`
#                               with a trailing wildcard; 03 and the boundary
#                               protect only the exact name.
#
# Each is created the way a developer would: by the non-exempt subject, WITH
# the developer boundary, so SCP 05 allows it. A created squatter is then
# assumed and tried against one action from each of 01, 04, 06 and 07:
#
#   01  access-analyzer:DeleteAnalyzer. Chosen because the boundary does NOT
#       deny it, so the result is the SCP's alone. For the pathed role this is
#       also the open question from B-001: if aws:PrincipalArn is the
#       assumed-role ARN (which carries no path), 01's
#       `assumed-role/Platform*/*` exemption matches and this gets through.
#   04  ec2:CreateVolume, untagged    --dry-run: nothing is created
#   06  ec2:DescribeVpcs in ${DENIED_REGION} --dry-run
#   07  ec2:CreateInternetGateway     --dry-run. Snapshot sharing sits in the
#       same policy under an identical exemption list, so IGW answers for both.
#
# EC2 dry-run authorizes fully, SCPs included, and then returns DryRunOperation
# instead of acting. That is "got past authorization" with nothing to clean up.
#
# All of these are expected to FAIL until the exemption rewrite lands. That is
# the point: they record the exposure before it is fixed, and they are what
# proves the fix.
SQUAT_TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${PLATFORM_MGMT_ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"

# past_auth <output> — the call was authorized: it either ran, dry-ran, or
# failed on a resource lookup that happens only after authorization.
#
# Only responses a service gives AFTER authorizing. Never a bare "not found":
# ECS answers "TaskDefinition not found" before it authorizes, so an untagged
# CreateService read as "exempt" for a principal the SCP was denying. Proved
# 2026-09-30 by the non-exempt subject getting the same answer. That is the
# fourth time validation-before-authorization has faked a result here.
past_auth() { printf '%s' "$1" | grep -qE 'DryRunOperation|ResourceNotFoundException|InvalidParameterValueException'; }
boundary_denied() { printf '%s' "$1" | grep -qi 'permissions boundary'; }

# exposure_check <ref> <label> <key> <secret> <token> -- <cmd...>
#
# Like check(), but as an arbitrary principal and with a third outcome that
# matters here: denied by the BOUNDARY rather than the SCP. That still holds
# the line for this principal, but it is not the SCP doing it, and the SCP is
# what the next account without a boundary relies on.
exposure_check() {
  local ref="$1" label="$2" k="$3" s="$4" t="$5"; shift 5
  [[ "$1" == "--" ]] && shift
  local out rc
  out="$(AWS_ACCESS_KEY_ID="${k}" AWS_SECRET_ACCESS_KEY="${s}" AWS_SESSION_TOKEN="${t}" \
         "$@" 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -eq 0 ]] || past_auth "${out}"; then
    emit FAIL "${label}" "${ref}" "got past authorization — the name is exempt"
  elif scp_denied "${out}"; then
    emit PASS "${label}" "${ref}" "denied, SCP named explicitly"
  elif boundary_denied "${out}"; then
    emit AMBIG "${label}" "${ref}" "denied by the permissions boundary, NOT the SCP"
  elif denied "${out}"; then
    emit AMBIG "${label}" "${ref}" "denied, by a policy the service did not name"
  else
    emit FAIL "${label}" "${ref}" \
      "unexpected — $(printf '%s' "${out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-100)"
  fi
}

# Harness-created roles only. Inline policies go first because containment may
# have put one there during the run (B-091), and DeleteRole refuses while any
# policy remains.
remove_harness_role() {
  local role="$1" pol
  for pol in $(as_org aws iam list-role-policies --role-name "${role}" \
                 --query 'PolicyNames' --output text 2>/dev/null | no_cr); do
    as_org aws iam delete-role-policy --role-name "${role}" --policy-name "${pol}" >/dev/null 2>&1
  done
  for pol in $(as_org aws iam list-attached-role-policies --role-name "${role}" \
                 --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | no_cr); do
    as_org aws iam detach-role-policy --role-name "${role}" --policy-arn "${pol}" >/dev/null 2>&1
  done
  as_org aws iam delete-role --role-name "${role}" >/dev/null 2>&1 \
    || warn "Could not delete harness role ${role} — remove it by hand."
}

SQUATTERS=("/xrole/:PlatformSquatPath" "/:stacksets-exec-squat" "/:OrganizationAccountAccessRoleSquat")
CREATED_SQUATTERS=()

# A previous run that died mid-way leaves squatters behind, and CreateRole then
# fails with EntityAlreadyExists, which would read as neither pass nor fail.
for entry in "${SQUATTERS[@]}"; do
  as_org aws iam get-role --role-name "${entry#*:}" >/dev/null 2>&1 \
    && { warn "Squatter ${entry#*:} left by a previous run — removing it first."; remove_harness_role "${entry#*:}"; }
done

for entry in "${SQUATTERS[@]}"; do
  sq_path="${entry%%:*}" sq_name="${entry#*:}"
  out="$(as_test aws iam create-role --path "${sq_path}" --role-name "${sq_name}" \
           --assume-role-policy-document "${SQUAT_TRUST}" \
           --permissions-boundary "${BOUNDARY_ARN}" 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -ne 0 ]]; then
    if scp_denied "${out}"; then
      emit PASS "iam:CreateRole ${sq_path}${sq_name}" "03/squatting" "denied, SCP named explicitly"
    else
      emit AMBIG "iam:CreateRole ${sq_path}${sq_name}" "03/squatting" \
        "refused, but not provably by the SCP — $(printf '%s' "${out}" | tr -d '\n' | grep -o 'An error occurred.*' | cut -c1-100)"
    fi
    continue
  fi
  if [[ "${sq_path}" == "/" ]]; then
    emit FAIL "iam:CreateRole ${sq_path}${sq_name}" "03/squatting" \
      "a non-platform principal created a role whose name matches an SCP exemption"
  else
    # A pathed role is allowed to exist. Since the rewrite every exemption is
    # an exact role/<Name> pattern, which a path cannot match (role/Platform*
    # does not match role/xrole/PlatformX), so the name buys nothing. What
    # proves that is the four checks below all holding, not the creation
    # failing. No blanket pathed-role deny: design 11 does not need one, and
    # AWS tooling creates /service-role/ roles legitimately.
    emit PASS "iam:CreateRole ${sq_path}${sq_name}" "03/squatting" \
      "permitted, and inert if its exemption checks below all hold"
  fi
  # Allowed because the role carries the boundary: 05 only refuses policies on
  # unbounded roles. Effective permissions are AdministratorAccess ∩ boundary,
  # which is what a developer's role would have.
  as_test aws iam attach-role-policy --role-name "${sq_name}" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess >/dev/null 2>&1 || true
  CREATED_SQUATTERS+=("${sq_name}")
done

if [[ ${#CREATED_SQUATTERS[@]} -gt 0 ]]; then
  sleep 10   # sts:AssumeRole is eventually consistent after role creation
  for sq_name in "${CREATED_SQUATTERS[@]}"; do
    sq_arn="$(as_org aws iam get-role --role-name "${sq_name}" --query Role.Arn --output text | no_cr)"
    read -r S_KEY S_SECRET S_TOKEN <<<"$(assume "${sq_arn}" gr-squat)"
    if [[ -z "${S_KEY:-}" ]]; then
      emit AMBIG "assume ${sq_name}" "squatting" "created but could not be assumed — its exemptions went untested"
      continue
    fi
    exposure_check 01/ProtectThreatDetection "${sq_name}: access-analyzer:DeleteAnalyzer" \
      "${S_KEY}" "${S_SECRET}" "${S_TOKEN}" -- \
      aws accessanalyzer delete-analyzer --analyzer-name "${NOPE}" --region "${TEST_REGION}"
    exposure_check 04/RequireOwnerTag "${sq_name}: ec2:CreateVolume untagged" \
      "${S_KEY}" "${S_SECRET}" "${S_TOKEN}" -- \
      aws ec2 create-volume --dry-run --availability-zone "${AZ}" --size 1 \
        --volume-type gp3 --region "${TEST_REGION}"
    exposure_check 06/DenyOutsideApprovedRegions "${sq_name}: ec2:DescribeVpcs ${DENIED_REGION}" \
      "${S_KEY}" "${S_SECRET}" "${S_TOKEN}" -- \
      aws ec2 describe-vpcs --dry-run --region "${DENIED_REGION}"
    exposure_check 07/NoNetworkingAroundTheBaseline "${sq_name}: ec2:CreateInternetGateway" \
      "${S_KEY}" "${S_SECRET}" "${S_TOKEN}" -- \
      aws ec2 create-internet-gateway --dry-run --region "${TEST_REGION}"
  done
  for sq_name in "${CREATED_SQUATTERS[@]}"; do remove_harness_role "${sq_name}"; done
fi

# --- The tenant app deployer's exemptions (B-136) --------------------------
#
# change/00-appdeployer.yaml named the tenant CI role PlatformAppDeployer-<app>,
# which put a pipeline AltDigital does not control inside every SCP's Platform*
# exemption. It is TenantAppDeployer-<app> now, and these checks prove the new
# name is exempt from nothing. The real role trusts GitHub OIDC only, so the harness cannot
# assume it. The exemption depends on the NAME alone, so a clone carrying the
# real role's boundary and its Deploy policy is an exact test of it.
#
# Runs only where an app deployer is deployed; skipped elsewhere, and says so.
#
#   06  lambda:ListFunctions in ${DENIED_REGION}. Deploy grants lambda:* on *
#       with no region condition, so the SCP is the only thing in the way.
#   04  lambda:CreateFunction, untagged, naming an execution role that does
#       not exist. Lambda authorizes first: for the non-exempt subject an
#       untagged call is AccessDenied by the SCP and a fully tagged one gets
#       InvalidParameterValue ("role ... cannot be assumed"), 2026-09-30. So
#       InvalidParameterValue here means the tag requirement never applied.
#       Nothing is created either way. The role name matches the deployer's
#       PassRole pattern (role/<app>-*), so a PassRole refusal cannot stand in
#       for the SCP's.
# Either name: TenantAppDeployer- since B-136, PlatformAppDeployer- in an
# account not yet redeployed. The clone always takes the new name, because the
# new name is what has to be exempt from nothing.
APP_SRC="$(as_org aws iam list-roles --query "Roles[?(starts_with(RoleName,'TenantAppDeployer-') || starts_with(RoleName,'PlatformAppDeployer-')) && RoleName!='TenantAppDeployer-harness'].RoleName | [0]" \
             --output text 2>/dev/null | no_cr | sed 's/^None$//')"
APP_CLONE="TenantAppDeployer-harness"
if [[ -z "${APP_SRC}" || "${APP_SRC}" == "${APP_CLONE}" ]]; then
  skip "No tenant app deployer in ${TARGET_ACCOUNT}; the app deployer's exemptions are tested where one is deployed."
else
  as_org aws iam get-role --role-name "${APP_CLONE}" >/dev/null 2>&1 && remove_harness_role "${APP_CLONE}"
  app_boundary="$(as_org aws iam get-role --role-name "${APP_SRC}" \
                    --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text | no_cr)"
  app_deploy="$(as_org aws iam get-role-policy --role-name "${APP_SRC}" --policy-name Deploy \
                  --query PolicyDocument --output json 2>/dev/null)"
  # Strip the role's OWN aws:RequestedRegion conditions from the clone. Since
  # B-136 the deployer scopes its compute to the approved regions itself, so an
  # out-of-region call is refused by the role's policy before the SCP is ever
  # consulted, and the check could no longer say whether SCP 06 exempts the
  # NAME. That is what it exists to prove. Both layers are wanted, and they are
  # tested separately. The role's own condition is visible in its template.
  app_deploy="$(printf '%s' "${app_deploy}" | "$(command -v python || command -v python3)" -c '
import json, sys
doc = json.load(sys.stdin)
for st in doc.get("Statement", []):
    for op in list(st.get("Condition", {})):
        st["Condition"][op].pop("aws:RequestedRegion", None)
        if not st["Condition"][op]:
            del st["Condition"][op]
    if "Condition" in st and not st["Condition"]:
        del st["Condition"]
print(json.dumps(doc))')"
  # OrganizationAccountAccessRole creates it: 03 lets that role create Platform*
  # names, which is how the real one gets created too.
  if as_org aws iam create-role --role-name "${APP_CLONE}" \
       --assume-role-policy-document "${SQUAT_TRUST}" \
       --permissions-boundary "${app_boundary}" \
       --description "Guardrail harness: clone of ${APP_SRC}'s permissions" >/dev/null 2>&1 \
     && as_org aws iam put-role-policy --role-name "${APP_CLONE}" --policy-name Deploy \
       --policy-document "${app_deploy}" >/dev/null 2>&1; then
    sleep 10
    read -r A_KEY A_SECRET A_TOKEN <<<"$(assume \
      "arn:aws:iam::${TARGET_ACCOUNT}:role/${APP_CLONE}" gr-appdeployer)"
    if [[ -n "${A_KEY:-}" ]]; then
      exposure_check 06/DenyOutsideApprovedRegions "app deployer: lambda:ListFunctions ${DENIED_REGION}" \
        "${A_KEY}" "${A_SECRET}" "${A_TOKEN}" -- \
        aws lambda list-functions --max-items 1 --region "${DENIED_REGION}"
      app_code="${APP_SRC#*-}"
      fn_zip="$(mktemp -d)/fn.zip"
      "$(command -v python || command -v python3)" -c 'import sys,zipfile
z = zipfile.ZipFile(sys.argv[1], "w"); z.writestr("index.py", "def h(e, c): return 0\n"); z.close()' \
        "$(win_path "${fn_zip}")"
      exposure_check 04/RequireOwnerTag "app deployer: lambda:CreateFunction untagged" \
        "${A_KEY}" "${A_SECRET}" "${A_TOKEN}" -- \
        aws lambda create-function --function-name "${NOPE}" --runtime python3.12 \
          --handler index.h --zip-file "fileb://$(win_path "${fn_zip}")" \
          --role "arn:aws:iam::${TARGET_ACCOUNT}:role/${app_code}-${NOPE}" \
          --region "${TEST_REGION}"
    else
      emit AMBIG "assume ${APP_CLONE}" "app-deployer" "clone created but could not be assumed"
    fi
  else
    emit AMBIG "create ${APP_CLONE}" "app-deployer" "could not clone ${APP_SRC}; its exemptions went untested"
  fi
  remove_harness_role "${APP_CLONE}"
fi

# --- 08: the platform's own monitoring (B-149, design 11 Layer 1) ---------
#
# None of these use the differential. 08 does not exempt the probe -- its
# exemption lists name the deployment principals and the one platform role
# that writes each resource, nothing else -- so the probe would be denied too
# and every check would read AMBIGUOUS. They use `real` instead: a genuine
# platform resource, a subject holding AdministratorAccess, so a denial can
# only be an SCP.
#
# `real` has a cost the differential does not: if the guardrail is ABSENT,
# the subject's call SUCCEEDS against a live control. So every call below is
# chosen to be harmless when it succeeds -- a tag added and then removed, a
# value overwritten with itself, an EC2 dry run, a fixture the harness made.
# Nothing here rewrites code, disables a rule or deletes a platform resource,
# even in the failure case. B-137 is what happens otherwise.

R="${TEST_REGION}"; A="${TARGET_ACCOUNT}"
HARNESS_TAG_KEY="guardrail-harness"

# Functions. Tagging stands in for UpdateFunctionCode: same statement, same
# resource match, and a stray tag is the worst outcome if it is not denied.
FN_ARN="arn:aws:lambda:${R}:${A}:function:platform-response"
if as_org aws lambda get-function-configuration --function-name platform-response \
     --region "${R}" >/dev/null 2>&1; then
  check 08 ProtectPlatformFunctions "lambda:TagResource platform-response" real -- \
    aws lambda tag-resource --resource "${FN_ARN}" --tags "${HARNESS_TAG_KEY}=probe" --region "${R}"
  as_org aws lambda untag-resource --resource "${FN_ARN}" \
    --tag-keys "${HARNESS_TAG_KEY}" --region "${R}" >/dev/null 2>&1 || true
else
  emit AMBIG "lambda:TagResource platform-response" "08/ProtectPlatformFunctions" \
    "platform-response is not deployed here; nothing real to test against"
fi

# Rules. The drift rule is the one whose loss makes every later alarm
# deletion permanent, so it is the one to prove.
RULE_ARN="arn:aws:events:${R}:${A}:rule/platform-alarm-drift-on-delete"
if as_org aws events describe-rule --name platform-alarm-drift-on-delete \
     --region "${R}" >/dev/null 2>&1; then
  check 08 ProtectPlatformEventRules "events:TagResource drift rule" real -- \
    aws events tag-resource --resource-arn "${RULE_ARN}" \
      --tags "Key=${HARNESS_TAG_KEY},Value=probe" --region "${R}"
  as_org aws events untag-resource --resource-arn "${RULE_ARN}" \
    --tag-keys "${HARNESS_TAG_KEY}" --region "${R}" >/dev/null 2>&1 || true
else
  emit AMBIG "events:TagResource drift rule" "08/ProtectPlatformEventRules" \
    "platform-alarm-drift-on-delete is not deployed here"
fi

ALARM_ARN="arn:aws:cloudwatch:${R}:${A}:alarm:platform-alarm-drift"
if [[ "$(as_org aws cloudwatch describe-alarms --alarm-names platform-alarm-drift \
          --region "${R}" --query 'length(MetricAlarms)' --output text 2>/dev/null | no_cr)" == "1" ]]; then
  check 08 ProtectPlatformAlarms "cloudwatch:TagResource platform alarm" real -- \
    aws cloudwatch tag-resource --resource-arn "${ALARM_ARN}" \
      --tags "Key=${HARNESS_TAG_KEY},Value=probe" --region "${R}"
  as_org aws cloudwatch untag-resource --resource-arn "${ALARM_ARN}" \
    --tag-keys "${HARNESS_TAG_KEY}" --region "${R}" >/dev/null 2>&1 || true
else
  emit AMBIG "cloudwatch:TagResource platform alarm" "08/ProtectPlatformAlarms" \
    "platform-alarm-drift is not deployed here"
fi

# The kill switch, overwritten with its own current value. If the guardrail
# is absent this writes a new version identical to the last.
read -r KS_VALUE KS_TYPE <<<"$(as_org aws ssm get-parameter --name /platform/response/enabled \
  --region "${R}" --query 'Parameter.[Value,Type]' --output text 2>/dev/null | no_cr)"
if [[ -n "${KS_VALUE:-}" && -n "${KS_TYPE:-}" ]]; then
  check 08 ProtectPlatformParameters "ssm:PutParameter kill switch (same value)" real -- \
    aws ssm put-parameter --name /platform/response/enabled --value "${KS_VALUE}" \
      --type "${KS_TYPE}" --overwrite --region "${R}"
else
  emit AMBIG "ssm:PutParameter kill switch" "08/ProtectPlatformParameters" \
    "/platform/response/enabled does not exist here"
fi

# Records. A delete of a key that cannot exist: DynamoDB treats it as a
# successful no-op, so an absent guardrail costs nothing, and the remediation
# history -- which is evidence -- is never written to.
KEYS_JSON="$(as_org aws dynamodb describe-table --table-name platform-remediation-history \
  --region "${R}" --query 'Table.[KeySchema,AttributeDefinitions]' --output json 2>/dev/null)" || KEYS_JSON=""
if [[ -n "${KEYS_JSON}" ]]; then
  ITEM_KEY="$(printf '%s' "${KEYS_JSON}" | "$(command -v python || command -v python3)" -c '
import json,sys
schema, defs = json.load(sys.stdin)
types = {d["AttributeName"]: d["AttributeType"] for d in defs}
val = {"S": "guardrail-harness-does-not-exist", "N": "-1", "B": "Z3VhcmRyYWls"}
print(json.dumps({k["AttributeName"]: {types[k["AttributeName"]]: val[types[k["AttributeName"]]]}
                  for k in schema}))')"
  check 08 ProtectPlatformRecords "dynamodb:DeleteItem remediation history" real -- \
    aws dynamodb delete-item --table-name platform-remediation-history \
      --key "${ITEM_KEY}" --region "${R}"
else
  emit AMBIG "dynamodb:DeleteItem remediation history" "08/ProtectPlatformRecords" \
    "platform-remediation-history does not exist here"
fi

# Stacks. Termination protection set to the value it already has.
PSTACK="$(as_org aws cloudformation describe-stacks --region "${R}" \
  --query "Stacks[?starts_with(StackName,'StackSet-platform-')]|[0].[StackName,EnableTerminationProtection]" \
  --output text 2>/dev/null | no_cr)"
PSTACK_NAME="${PSTACK%%[[:space:]]*}"; PSTACK_TP="${PSTACK##*[[:space:]]}"
if [[ "${PSTACK_NAME}" == StackSet-platform-* ]]; then
  TP_FLAG="--no-enable-termination-protection"
  [[ "${PSTACK_TP}" == "True" ]] && TP_FLAG="--enable-termination-protection"
  check 08 ProtectPlatformStacks "cloudformation:UpdateTerminationProtection (same value)" real -- \
    aws cloudformation update-termination-protection --stack-name "${PSTACK_NAME}" \
      "${TP_FLAG}" --region "${R}"
else
  emit AMBIG "cloudformation:UpdateTerminationProtection" "08/ProtectPlatformStacks" \
    "no StackSet-platform-* stack here"
fi

# Associations, both directions. This is the statement whose first draft
# denied every association in the account, including the tenant pipeline's
# (B-149); it is narrowed to platform-managed=true and the tag is protected.
# Proving the deny alone would pass the over-broad version too -- the
# positive control below is what shows it was narrowed.
#
# The fixture is created by OrganizationAccountAccessRole, which is exempt and
# may therefore apply the protected tag. It targets a tag nothing carries, so
# it never runs anywhere.
FIX_ID="$(as_org aws ssm create-association --name AWS-UpdateSSMAgent \
  --association-name guardrail-harness-platform-fixture \
  --targets "Key=tag:${HARNESS_TAG_KEY},Values=never-matches" \
  --tags Key=platform-managed,Value=true "Key=${HARNESS_TAG_KEY},Value=fixture" \
  --region "${R}" --query AssociationDescription.AssociationId --output text 2>/dev/null | no_cr)" || FIX_ID=""
if [[ -n "${FIX_ID}" && "${FIX_ID}" != "None" ]]; then
  check 08 ProtectPlatformAssociations "ssm:DeleteAssociation platform-managed" real -- \
    aws ssm delete-association --association-id "${FIX_ID}" --region "${R}"
  # Untag-then-delete is the way round the statement above.
  check 08 ProtectSsmManagedTag "ssm:RemoveTagsFromResource platform-managed" real -- \
    aws ssm remove-tags-from-resource --resource-type Association \
      --resource-id "${FIX_ID}" --tag-keys platform-managed --region "${R}"
  as_org aws ssm delete-association --association-id "${FIX_ID}" --region "${R}" >/dev/null 2>&1 \
    || warn "Could not delete the harness association fixture ${FIX_ID} - remove it by hand."
else
  emit AMBIG "ssm:DeleteAssociation platform-managed" "08/ProtectPlatformAssociations" \
    "could not create the tagged fixture association"
fi

OWN_ID="$(as_test aws ssm create-association --name AWS-UpdateSSMAgent \
  --association-name guardrail-harness-own \
  --targets "Key=tag:${HARNESS_TAG_KEY},Values=never-matches" \
  --region "${R}" --query AssociationDescription.AssociationId --output text 2>/dev/null | no_cr)" || OWN_ID=""
if [[ -n "${OWN_ID}" && "${OWN_ID}" != "None" ]]; then
  expect_allowed "ssm:UpdateAssociation (own, untagged)" -- \
    aws ssm update-association --association-id "${OWN_ID}" \
      --association-name guardrail-harness-own-updated --region "${R}"
  expect_allowed "ssm:DeleteAssociation (own, untagged)" -- \
    aws ssm delete-association --association-id "${OWN_ID}" --region "${R}"
else
  emit FAIL "ssm:CreateAssociation (own)" "control" \
    "a local administrator could not create an ordinary association"
fi

# --- 07: the baseline network (B-149) --------------------------------------

# A dry run. Permitted returns DryRunOperation, which `check` reports as got
# past authorization -- FAIL, correctly -- and nothing changes either way.
BASE_SG="$(as_org aws ec2 describe-security-groups --region "${R}" \
  --filters Name=tag:platform-managed,Values=true \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | no_cr)"
if [[ "${BASE_SG}" == sg-* ]]; then
  check 07 ProtectBaselineNetwork "ec2:AuthorizeSecurityGroupEgress baseline (dry run)" real -- \
    aws ec2 authorize-security-group-egress --dry-run --group-id "${BASE_SG}" \
      --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=203.0.113.9/32}]' \
      --region "${R}"
else
  emit AMBIG "ec2:AuthorizeSecurityGroupEgress baseline" "07/ProtectBaselineNetwork" \
    "no platform-managed security group here"
fi

# The DNS firewall's tag is what ProtectBaselineNetwork keys on, so removing
# it is the bypass. Re-applied afterwards only if the subject got through.
FRG_ARN="$(as_org aws route53resolver list-firewall-rule-groups --region "${R}" \
  --query 'FirewallRuleGroups[?starts_with(Name,`platform-`)]|[0].Arn' --output text 2>/dev/null | no_cr)"
FRG_TAGGED=""
if [[ "${FRG_ARN}" == arn:* ]]; then
  FRG_TAGGED="$(as_org aws route53resolver list-tags-for-resource \
    --resource-arn "${FRG_ARN}" --region "${R}" \
    --query "Tags[?Key=='platform-managed'].Value|[0]" --output text 2>/dev/null | no_cr)"
fi
if [[ "${FRG_TAGGED}" == "true" ]]; then
  check 07 ProtectResolverTags "route53resolver:UntagResource platform-managed" real -- \
    aws route53resolver untag-resource --resource-arn "${FRG_ARN}" \
      --tag-keys platform-managed --region "${R}"
  as_org aws route53resolver tag-resource --resource-arn "${FRG_ARN}" \
    --tags Key=platform-managed,Value=true --region "${R}" >/dev/null 2>&1 || true
else
  emit AMBIG "route53resolver:UntagResource" "07/ProtectResolverTags" \
    "no platform-managed DNS firewall rule group here"
fi

check 01 ProtectCloudTrail "cloudtrail:StopLogging" yes -- \
  aws cloudtrail stop-logging --name "${NOPE}"

check 01 ProtectConfigRecorder "config:DeleteConfigurationRecorder" yes -- \
  aws configservice delete-configuration-recorder --configuration-recorder-name "${NOPE}"

check 01 ProtectThreatDetection "guardduty:DeleteDetector" yes -- \
  aws guardduty delete-detector --detector-id "00000000000000000000000000000000"

check 01 ProtectFlowAndDnsLogging "ec2:DeleteFlowLogs" yes -- \
  aws ec2 delete-flow-logs --flow-log-ids "fl-00000000000000000"

# `real`, NOT the differential. The differential re-runs the call as the probe,
# which is exempt, so on a REAL key the probe's call succeeds: on 2026-09-20 it
# scheduled this fixture for deletion (B-137). A real key plus
# AdministratorAccess needs no differential. If the subject is denied, only an
# SCP can have done it, which is the same inference the Backup vault check uses.
if [[ -n "${KEY_ID}" ]]; then
  KEY_STATE="$(as_org aws kms describe-key --key-id "${KEY_ID}" \
                 --query KeyMetadata.KeyState --output text 2>/dev/null | no_cr)"
  [[ "${KEY_STATE}" == "Enabled" ]] || warn "KMS fixture ${KEY_ID} is ${KEY_STATE}, not Enabled. \
Authorization is expected to run before the key state is read, so the check below \
should still test the SCP, but a PendingDeletion fixture is gone on its deletion date. B-137."
  check 02 ProtectKmsKeys "kms:ScheduleKeyDeletion" real -- \
    aws kms schedule-key-deletion --key-id "${KEY_ID}" --pending-window-in-days 30
fi

if [[ ${VAULT_OK} -eq 1 ]]; then
  check 02 ProtectBackupVaults "backup:DeleteBackupVault" real -- \
    aws backup delete-backup-vault --backup-vault-name "${VAULT_NAME}"
fi

# Writes the all-true block apply-account-settings.sh already sets, so the
# call is harmless whether or not the guardrail holds: the account's real
# exposure cannot change. The account-level setting is real, so `real` holds.
check 02 ProtectAccountPublicAccessBlock "s3:PutAccountPublicAccessBlock" real -- \
  aws s3control put-public-access-block --account-id "${TARGET_ACCOUNT}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# The critical one. If this fails, a member administrator can name their way
# out of every wildcard exclusion in policies 01 and 02, and the whole set is
# decorative. No differential: the probe is itself excluded here.
check 03 DenyPlatformRoleNameSquatting "iam:CreateRole Platform*" no -- \
  aws iam create-role --role-name "PlatformEscapeHatch" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

check 03 DenyLeavingTheOrganization "organizations:LeaveOrganization" no -- \
  aws organizations leave-organization

expect_allowed "sts:GetCallerIdentity" -- aws sts get-caller-identity

hr
log "  ${PASS} passed, ${AMBIG} ambiguous, ${FAIL} failed"
hr

if [[ ${OUTPUT_JSON} -eq 1 ]]; then
  printf '{"account":"%s","region":"%s","passed":%d,"ambiguous":%d,"failed":%d,"results":[' \
    "${TARGET_ACCOUNT}" "${TEST_REGION}" "${PASS}" "${AMBIG}" "${FAIL}"
  first=1
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r v ref l d <<<"${r}"
    [[ ${first} -eq 1 ]] || printf ','
    printf '{"verdict":"%s","ref":"%s","label":"%s","detail":"%s"}' "${v}" "${ref}" "${l}" "${d//\"/}"
    first=0
  done
  printf ']}\n'
fi

[[ ${FAIL} -eq 0 ]] || die "${FAIL} guardrail assertion(s) FAILED. Do not vest accounts until clean —
      a guardrail that does not hold is worse than one known to be absent,
      because it is trusted."
[[ ${AMBIG} -eq 0 ]] || { warn "${AMBIG} assertion(s) AMBIGUOUS: denied, but not provably by the SCP.
      Acceptable only if you have read the detail and understand why."; exit 0; }
ok "All guardrail assertions passed, each provably by service control policy."
