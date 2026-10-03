#!/usr/bin/env bash
#
# Prove the deploy role's condition keys do what change/00-appdeployer.yaml
# says (B-153).
#
#   scripts/test-deployer-conditions.sh --account <sandbox account id>
#
# The boundary refuses unencrypted RDS, EFS and ElastiCache, SNS delivery
# outside AWS, secrets without a customer-managed key, and -- in uat and prod
# -- deleting data stores. Each refusal is a condition key, and the unknown
# was how each key resolves when a request OMITS the flag:
#
#   absent, and the request was unencrypted   -> BoolIfExists refuses. Wanted.
#   absent, and the request was compliant     -> refused too. A false denial.
#
# Every conditioned create is therefore sent three ways where the API allows:
# flag true, flag false, flag omitted.
#
# ---------------------------------------------------------------------------
# How a create is tested without creating anything
# ---------------------------------------------------------------------------
# Authorization runs before validation. A compliant create that names a subnet
# group which does not exist is AUTHORIZED, then fails with
# DBSubnetGroupNotFoundFault (or the ElastiCache equivalent). A non-authorization
# error is therefore the proof of "allowed", and nothing billable exists at any
# point. EFS, secrets and SNS have no such hook and are created for real --
# free when empty -- then removed by OrganizationAccountAccessRole, because the
# clone itself is refused the deletes in a prod-tier account.
#
# ---------------------------------------------------------------------------
# Who is tested
# ---------------------------------------------------------------------------
# The real deploy role trusts GitHub OIDC only. Its permissions are its
# boundary and its Deploy policy, so a clone carrying both is an exact test --
# the same reasoning test-guardrails.sh uses for its app-deployer checks. The
# clone takes a TenantAppDeployer- name so the SCPs treat it exactly as they
# treat the real one: exempt from nothing.
#
# The source role must be deployed with AccountTier prod, or the delete
# checks have nothing to find.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""
CLONE="TenantAppDeployer-conditions"

usage() {
  cat <<'USAGE'
Usage: test-deployer-conditions.sh --account <id>

  --account   A Sandbox account holding a TenantAppDeployer-<app> role deployed
              from change/00-appdeployer.yaml with AccountTier=prod.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || { usage; die "--account must be 12 digits."; }
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
R="${AWS_DEFAULT_REGION}"; A="${TARGET_ACCOUNT}"

# Sandbox only. This creates and removes resources.
SANDBOX_OU="$(get_param /org/ou/sandbox || true)"
PARENT="$(aws organizations list-parents --child-id "${A}" \
  --query 'Parents[0].Id' --output text 2>/dev/null | no_cr)"
[[ -n "${SANDBOX_OU}" && "${PARENT}" == "${SANDBOX_OU}" ]] \
  || die "${A} is not directly in the Sandbox OU. Refusing."

assume() {
  aws sts assume-role --role-arn "$1" --role-session-name "$2" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr
}
read -r O_KEY O_SECRET O_TOKEN <<<"$(assume "arn:aws:iam::${A}:role/OrganizationAccountAccessRole" b153-setup)"
[[ -n "${O_KEY:-}" ]] || die "Could not assume OrganizationAccountAccessRole in ${A}."
as_org() { AWS_ACCESS_KEY_ID="${O_KEY}" AWS_SECRET_ACCESS_KEY="${O_SECRET}" \
           AWS_SESSION_TOKEN="${O_TOKEN}" "$@"; }

# --- the clone --------------------------------------------------------------

SRC="$(as_org aws iam list-roles \
  --query "Roles[?starts_with(RoleName,'TenantAppDeployer-') && RoleName!='${CLONE}' && RoleName!='TenantAppDeployer-harness'].RoleName | [0]" \
  --output text | no_cr | sed 's/^None$//')"
[[ -n "${SRC}" ]] || die "No TenantAppDeployer-<app> role in ${A}. Deploy change/00-appdeployer.yaml first."
APP="${SRC#TenantAppDeployer-}"
BOUNDARY="$(as_org aws iam get-role --role-name "${SRC}" \
  --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text | no_cr)"
DEPLOY="$(as_org aws iam get-role-policy --role-name "${SRC}" --policy-name Deploy \
  --query PolicyDocument --output json)"

# The check that the source is the NEW template: its boundary must carry the
# statements under test. Testing an old boundary would pass nothing and say so
# unhelpfully.
BVER="$(as_org aws iam get-policy --policy-arn "${BOUNDARY}" --query 'Policy.DefaultVersionId' --output text | no_cr)"
as_org aws iam get-policy-version --policy-arn "${BOUNDARY}" --version-id "${BVER}" \
  --query 'PolicyVersion.Document' --output json | grep -q NeverDeleteDataInProduction \
  || die "${SRC}'s boundary has no NeverDeleteDataInProduction. Redeploy it from the current template with AccountTier=prod."

remove_clone() {
  local p
  for p in $(as_org aws iam list-role-policies --role-name "${CLONE}" --query PolicyNames --output text 2>/dev/null | no_cr); do
    as_org aws iam delete-role-policy --role-name "${CLONE}" --policy-name "${p}" >/dev/null 2>&1 || true
  done
  as_org aws iam delete-role --role-name "${CLONE}" >/dev/null 2>&1 || true
}
as_org aws iam get-role --role-name "${CLONE}" >/dev/null 2>&1 && remove_clone

TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${PLATFORM_MGMT_ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"
as_org aws iam create-role --role-name "${CLONE}" --assume-role-policy-document "${TRUST}" \
  --permissions-boundary "${BOUNDARY}" \
  --description "B-153 probe: clone of ${SRC}'s boundary and Deploy policy" >/dev/null \
  || die "Could not create ${CLONE}."
as_org aws iam put-role-policy --role-name "${CLONE}" --policy-name Deploy \
  --policy-document "${DEPLOY}" >/dev/null || { remove_clone; die "Could not attach Deploy to ${CLONE}."; }
sleep 12

read -r C_KEY C_SECRET C_TOKEN <<<"$(assume "arn:aws:iam::${A}:role/${CLONE}" b153-probe)"
[[ -n "${C_KEY:-}" ]] || { remove_clone; die "Could not assume ${CLONE}."; }
cleanup() {
  TOPIC="${TOPIC:-arn:aws:sns:${R}:${A}:${APP}-b153}"
info "Removing what the run created"
for s in "${APP}/b153-alias" "${APP}/b153-arn" "${APP}/b153-nokey"; do
  as_org aws secretsmanager delete-secret --secret-id "${s}" \
    --force-delete-without-recovery >/dev/null 2>&1 || true
done
for t in "${APP}-b153-enc" "${APP}-b153-plain"; do
  fs="$(as_org aws efs describe-file-systems --creation-token "${t}" \
          --query 'FileSystems[0].FileSystemId' --output text 2>/dev/null | no_cr | sed 's/^None$//')"
  [[ -n "${fs}" ]] && as_org aws efs delete-file-system --file-system-id "${fs}" >/dev/null 2>&1
done
as_org aws sns delete-topic --topic-arn "${TOPIC}" >/dev/null 2>&1 || true
remove_clone
}

trap 'cleanup' EXIT
as_clone() { AWS_ACCESS_KEY_ID="${C_KEY}" AWS_SECRET_ACCESS_KEY="${C_SECRET}" \
             AWS_SESSION_TOKEN="${C_TOKEN}" "$@"; }

# --- classification ---------------------------------------------------------

authz_error() { printf '%s' "$1" | grep -qiE 'AccessDenied|not authorized|UnauthorizedOperation|AuthorizationError'; }

PASS=0; FAIL=0
# expect <deny|allow> <label> -- <cmd...>
expect() {
  local want="$1" label="$2" out rc got why; shift 2; [[ "$1" == "--" ]] && shift
  out="$(as_clone "$@" 2>&1)" && rc=0 || rc=$?
  if [[ ${rc} -ne 0 ]] && authz_error "${out}"; then got=deny; else got=allow; fi
  # `|| true`: a successful call prints JSON, not "An error occurred", and an
  # unmatched grep under pipefail would end the run before cleanup.
  why="$(printf '%s' "${out}" | tr -d '\n' | { grep -oE 'An error occurred.*' || true; })"
  [[ -z "${why}" ]] && why="$([[ ${rc} -eq 0 ]] && echo 'succeeded' || printf '%s' "${out}" | tr -d '\n')"
  if [[ "${got}" == "${want}" ]]; then
    ok "$(printf '%-34s %-5s %s' "${label}" "${got}" "${why:0:150}")"; PASS=$((PASS+1))
  else
    # In full: which policy refused, and on which resource, is the diagnosis.
    printf '%s  FAIL%s  %-34s wanted %s, got %s:\n        %s\n' "${C_RED}" "${C_RESET}" "${label}" "${want}" "${got}" "${why}"
    FAIL=$((FAIL+1))
  fi
}

TAGS=(Key=Owner,Value=b153-probe Key=Application,Value="${APP}"
      Key=DataClassification,Value=internal Key=platform-layer,Value=application)
NOSUBNET="${APP}-b153-does-not-exist"
PW="Probe-never-used-0"

hr
log "B-153: the deploy role's condition keys"
log "  account : ${A} (Sandbox), ${R}"
log "  source  : ${SRC}, application '${APP}', boundary ${BOUNDARY##*/}"
log "  subject : ${CLONE}"
hr

info "RDS instance (allowed = DBSubnetGroupNotFound; nothing is created)"
rds_inst() { aws rds create-db-instance --db-instance-identifier "${APP}-b153-inst" \
  --engine postgres --db-instance-class db.t4g.micro --allocated-storage 20 \
  --master-username probe --master-user-password "${PW}" \
  --db-subnet-group-name "${NOSUBNET}" --tags "${TAGS[@]}" "$@"; }
expect deny  "rds instance, flag omitted"     -- rds_inst
expect deny  "rds instance, encrypted false"  -- rds_inst --no-storage-encrypted
expect allow "rds instance, encrypted true"   -- rds_inst --storage-encrypted
expect allow "aurora member, flag omitted"    -- aws rds create-db-instance \
  --db-instance-identifier "${APP}-b153-member" --engine aurora-postgresql \
  --db-instance-class db.t4g.medium --db-cluster-identifier "${APP}-b153-nocluster" --tags "${TAGS[@]}"

info "Aurora cluster"
rds_cluster() { aws rds create-db-cluster --db-cluster-identifier "${APP}-b153-cluster" \
  --engine aurora-postgresql --master-username probe --master-user-password "${PW}" \
  --db-subnet-group-name "${NOSUBNET}" --tags "${TAGS[@]}" "$@"; }
expect deny  "aurora cluster, flag omitted"   -- rds_cluster
expect allow "aurora cluster, encrypted true" -- rds_cluster --storage-encrypted

info "ElastiCache replication group (allowed = CacheSubnetGroupNotFound)"
ec_rg() { aws elasticache create-replication-group --replication-group-id "${APP}-b153-rg" \
  --replication-group-description b153 --engine redis --cache-node-type cache.t4g.micro \
  --num-cache-clusters 1 --cache-subnet-group-name "${NOSUBNET}" "$@"; }
expect deny  "elasticache, flag omitted"      -- ec_rg
expect deny  "elasticache, at-rest false"     -- ec_rg --no-at-rest-encryption-enabled
expect allow "elasticache, at-rest true"      -- ec_rg --at-rest-encryption-enabled

info "EFS (the encrypted case is created for real, then removed)"
expect deny  "efs, flag omitted"              -- aws efs create-file-system \
  --creation-token "${APP}-b153-plain" --tags Key=Application,Value="${APP}"
expect allow "efs, encrypted true"            -- aws efs create-file-system \
  --creation-token "${APP}-b153-enc" --encrypted --tags Key=Application,Value="${APP}"

info "Secrets (created for real, then removed)"
SECRETS_KEY_ARN="$(as_org aws kms describe-key --key-id alias/platform-secrets \
  --query KeyMetadata.Arn --output text 2>/dev/null | no_cr)"
expect deny  "secret, no kms key"             -- aws secretsmanager create-secret \
  --name "${APP}/b153-nokey" --secret-string probe
expect allow "secret, key by alias"           -- aws secretsmanager create-secret \
  --name "${APP}/b153-alias" --kms-key-id alias/platform-secrets --secret-string probe
if [[ -n "${SECRETS_KEY_ARN}" ]]; then
  expect allow "secret, key by arn"           -- aws secretsmanager create-secret \
    --name "${APP}/b153-arn" --kms-key-id "${SECRETS_KEY_ARN}" --secret-string probe
else
  warn "alias/platform-secrets absent in ${A}; the by-ARN case is skipped."
fi

info "SNS (topic created for real, then removed)"
TOPIC="arn:aws:sns:${R}:${A}:${APP}-b153"
expect allow "sns create topic"               -- aws sns create-topic --name "${APP}-b153"
expect deny  "sns subscribe email"            -- aws sns subscribe --topic-arn "${TOPIC}" \
  --protocol email --notification-endpoint probe@example.invalid
expect deny  "sns subscribe https"            -- aws sns subscribe --topic-arn "${TOPIC}" \
  --protocol https --notification-endpoint https://example.invalid/hook
expect allow "sns subscribe sqs"              -- aws sns subscribe --topic-arn "${TOPIC}" \
  --protocol sqs --notification-endpoint "arn:aws:sqs:${R}:${A}:${APP}-b153-does-not-exist"

info "Prod tier: deletes refused even on resources that do not exist"
expect deny  "rds delete instance"            -- aws rds delete-db-instance \
  --db-instance-identifier "${APP}-b153-does-not-exist" --skip-final-snapshot
expect deny  "dynamodb delete table"          -- aws dynamodb delete-table \
  --table-name "${APP}-b153-does-not-exist"
expect deny  "secret delete (real secret)"    -- aws secretsmanager delete-secret \
  --secret-id "${APP}/b153-alias"

info "Account public access block"
expect deny  "s3 account public access block" -- aws s3control put-public-access-block \
  --account-id "${A}" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

info "Control"
expect allow "sts get-caller-identity"        -- aws sts get-caller-identity

# --- cleanup, as OrganizationAccountAccessRole ------------------------------
# The clone is refused these deletes by design. Only what this run created.
# Also the EXIT trap, so an aborted run does not leave the clone behind.
trap - EXIT
cleanup

hr
log "  passed ${PASS}, failed ${FAIL}"
hr
[[ ${FAIL} -eq 0 ]]
