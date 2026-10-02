#!/usr/bin/env bash
#
# Prove that COMPLIANT CloudFormation gets past SCP 04 (require-tags).
#
#   scripts/test-tag-on-create.sh --account <sandbox account id> [--keep]
#
# test-guardrails.sh proves the SCPs deny what they should. This proves the
# opposite direction for the one SCP that denies ordinary creation: that a
# developer who tags correctly is NOT denied.
#
# ---------------------------------------------------------------------------
# Why this is a question at all
# ---------------------------------------------------------------------------
# SCP 04 conditions on aws:RequestTag, which is only present when the tags
# travel IN the create call. Some tooling creates first and tags afterwards --
# historically CloudFormation did this for S3 buckets, calling CreateBucket and
# then PutBucketTagging. If any resource type still works that way, SCP 04
# denies every correctly tagged template for it, and the developer guide's
# advice ("set tags at the stack level") is wrong.
#
# Nobody had tested it. The guardrail harness only sends untagged creates.
#
# ---------------------------------------------------------------------------
# What runs
# ---------------------------------------------------------------------------
# One stack per case, so one failure cannot mask another. Each tagged case is
# expected to reach CREATE_COMPLETE; the untagged control is expected to fail
# with the SCP named. Without the control, a run in an account where SCP 04 is
# not attached would report a clean sweep and prove nothing.
#
# The subject is GuardrailTestAdmin: AdministratorAccess, matching no SCP
# exemption, so SCP 04 applies to it exactly as it applies to a developer.
# CloudFormation without a service role acts with the caller's credentials, so
# the SCP is evaluated against that principal.
#
# Not covered, deliberately: rds:CreateDBInstance/CreateDBCluster (10+ minutes
# and real cost per run), ec2:RunInstances (needs an AMI and a subnet decision)
# and elasticloadbalancing:CreateLoadBalancer (needs subnets). Each is a
# follow-up if any covered case fails, because a failure here would mean the
# pattern is per-service and the uncovered ones need their own answer.
#
# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
# Every stack is named tagtest-* and deleted at the end unless --keep is given.
# This is test fixture teardown in a Sandbox account, of resources this script
# created seconds earlier -- not remediation. Leftovers from an earlier run are
# deleted at the start for the same reason, and only stacks with this prefix
# are ever touched.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TARGET_ACCOUNT=""
KEEP=0
TEST_ROLE="GuardrailTestAdmin"
PREFIX="tagtest"

usage() {
  cat <<'USAGE'
Usage: test-tag-on-create.sh --account <id> [--keep]

  --account   A Sandbox account with SCP 04 attached. Not the management
              account, where SCPs never apply.
  --keep      Leave the test stacks in place for inspection.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) TARGET_ACCOUNT="$2"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "${TARGET_ACCOUNT}" =~ ^[0-9]{12}$ ]] || { usage; die "--account must be 12 digits."; }
[[ "${TARGET_ACCOUNT}" != "${PLATFORM_MGMT_ACCOUNT_ID}" ]] \
  || die "SCPs do not apply to the management account. Target a Sandbox account."

require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"
export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"

# Refuse anything outside Sandbox. This creates and deletes resources, and a
# tenant account is not a test target however the id got typed.
SANDBOX_OU="$(get_param /org/ou/sandbox || true)"
PARENT="$(aws organizations list-parents --child-id "${TARGET_ACCOUNT}" \
  --query 'Parents[0].Id' --output text 2>/dev/null | no_cr)"
[[ -n "${SANDBOX_OU}" && "${PARENT}" == "${SANDBOX_OU}" ]] \
  || die "${TARGET_ACCOUNT} is not directly in the Sandbox OU (${SANDBOX_OU:-unresolved}); parent is ${PARENT:-unknown}. Refusing."

assume() {
  aws sts assume-role --role-arn "$1" --role-session-name "$2" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr
}

read -r O_KEY O_SECRET O_TOKEN <<<"$(assume \
  "arn:aws:iam::${TARGET_ACCOUNT}:role/OrganizationAccountAccessRole" tagtest-setup)"
[[ -n "${O_KEY:-}" ]] || die "Could not assume OrganizationAccountAccessRole in ${TARGET_ACCOUNT}."
as_org() { AWS_ACCESS_KEY_ID="${O_KEY}" AWS_SECRET_ACCESS_KEY="${O_SECRET}" \
           AWS_SESSION_TOKEN="${O_TOKEN}" "$@"; }

# The subject. Created here only if the guardrail harness has never run in
# this account; same definition as test-guardrails.sh's ensure_role.
if ! as_org aws iam get-role --role-name "${TEST_ROLE}" >/dev/null 2>&1; then
  trust="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${PLATFORM_MGMT_ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"
  as_org aws iam create-role --role-name "${TEST_ROLE}" \
    --assume-role-policy-document "${trust}" \
    --description "Guardrail harness principal" >/dev/null
  as_org aws iam attach-role-policy --role-name "${TEST_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess >/dev/null
  sleep 10
fi

# Containment can isolate the subject after the control case's denial (B-091,
# the same reason test-guardrails.sh releases it). Reported, not hidden.
for pol in $(as_org aws iam list-role-policies --role-name "${TEST_ROLE}" \
               --query 'PolicyNames' --output text 2>/dev/null | no_cr); do
  case "${pol}" in
    PlatformIncident*)
      as_org aws iam delete-role-policy --role-name "${TEST_ROLE}" --policy-name "${pol}" >/dev/null \
        && warn "${TEST_ROLE} was CONTAINED from a previous run; released. B-091." ;;
  esac
done

# The Lambda case needs the developer boundary, because SCP 05 refuses any
# role without it. Its absence is a baseline fault, not a tagging result.
BOUNDARY_ARN="arn:aws:iam::${TARGET_ACCOUNT}:policy/PlatformDeveloperBoundary"
HAVE_BOUNDARY=1
as_org aws iam get-policy --policy-arn "${BOUNDARY_ARN}" >/dev/null 2>&1 || HAVE_BOUNDARY=0
[[ ${HAVE_BOUNDARY} -eq 1 ]] || warn "PlatformDeveloperBoundary absent in ${TARGET_ACCOUNT}; the Lambda case is skipped."

read -r T_KEY T_SECRET T_TOKEN <<<"$(assume "arn:aws:iam::${TARGET_ACCOUNT}:role/${TEST_ROLE}" tagtest)"
[[ -n "${T_KEY:-}" ]] || die "Could not assume ${TEST_ROLE}."
as_test() { AWS_ACCESS_KEY_ID="${T_KEY}" AWS_SECRET_ACCESS_KEY="${T_SECRET}" \
            AWS_SESSION_TOKEN="${T_TOKEN}" "$@"; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# The four tags SCP 04 requires, as the developer guide tells people to set
# them. Owner is a team name, never a person.
STACK_TAGS=(Key=Owner,Value=tagtest-harness Key=Application,Value=tagtest
            Key=DataClassification,Value=internal Key=platform-layer,Value=application)

# --- templates --------------------------------------------------------------

cat >"${WORK}/s3.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Bucket:
    Type: AWS::S3::Bucket
YAML

cat >"${WORK}/s3-resource-tags.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - { Key: Owner, Value: tagtest-harness }
        - { Key: Application, Value: tagtest }
        - { Key: DataClassification, Value: internal }
        - { Key: platform-layer, Value: application }
YAML

cat >"${WORK}/dynamodb.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Table:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions: [{ AttributeName: pk, AttributeType: S }]
      KeySchema: [{ AttributeName: pk, KeyType: HASH }]
YAML

cat >"${WORK}/ebs.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Volume:
    Type: AWS::EC2::Volume
    Properties:
      AvailabilityZone: !Select [0, !GetAZs '']
      Size: 1
      VolumeType: gp3
YAML

cat >"${WORK}/lambda.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  BoundaryArn: { Type: String }
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      PermissionsBoundary: !Ref BoundaryArn
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: { Service: lambda.amazonaws.com }
            Action: sts:AssumeRole
  Function:
    Type: AWS::Lambda::Function
    Properties:
      Runtime: python3.12
      Handler: index.handler
      Role: !GetAtt Role.Arn
      Code:
        ZipFile: |
          def handler(event, context):
              return "ok"
YAML

# DesiredCount 0 on the EC2 launch type: CreateService runs, nothing is
# scheduled, nothing costs anything, and no subnet decision is needed.
cat >"${WORK}/ecs.yaml" <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Cluster:
    Type: AWS::ECS::Cluster
  TaskDefinition:
    Type: AWS::ECS::TaskDefinition
    Properties:
      RequiresCompatibilities: [EC2]
      NetworkMode: bridge
      ContainerDefinitions:
        - Name: app
          Image: public.ecr.aws/docker/library/busybox:latest
          Memory: 64
          Essential: true
  Service:
    Type: AWS::ECS::Service
    Properties:
      Cluster: !Ref Cluster
      TaskDefinition: !Ref TaskDefinition
      LaunchType: EC2
      DesiredCount: 0
YAML

# --- running a case ---------------------------------------------------------

stack_status() {
  as_test aws cloudformation describe-stacks --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null | no_cr
}

failure_reason() {
  as_test aws cloudformation describe-stack-events --stack-name "$1" \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output text 2>/dev/null | no_cr | head -3 | tr '\t\n' ' ' | cut -c1-300
}

delete_stack() {
  as_test aws cloudformation delete-stack --stack-name "$1" >/dev/null 2>&1 || true
}

PASS=0; FAIL=0; STACKS=()

# case <name> <template> <expect: created|denied> <stack-tags: yes|no> [params...]
run_case() {
  local name="$1" template="$2" expect="$3" tagged="$4"; shift 4
  local stack="${PREFIX}-${name}" args=() status reason
  STACKS+=("${stack}")
  args=(--stack-name "${stack}" --template-body "file://$(win_path "${template}")"
        --capabilities CAPABILITY_IAM --on-failure ROLLBACK)
  [[ "${tagged}" == yes ]] && args+=(--tags "${STACK_TAGS[@]}")
  [[ $# -gt 0 ]] && args+=(--parameters "$@")

  if ! out="$(as_test aws cloudformation create-stack "${args[@]}" 2>&1)"; then
    printf '%s  FAIL%s  %-26s create-stack refused: %s\n' "${C_RED}" "${C_RESET}" "${name}" \
      "$(printf '%s' "${out}" | tr -d '\n' | cut -c1-200)"
    FAIL=$((FAIL+1)); return
  fi
  as_test aws cloudformation wait stack-create-complete --stack-name "${stack}" >/dev/null 2>&1 || true
  status="$(stack_status "${stack}")"

  if [[ "${expect}" == created && "${status}" == CREATE_COMPLETE ]]; then
    ok "$(printf '%-26s CREATE_COMPLETE' "${name}")"; PASS=$((PASS+1)); return
  fi
  reason="$(failure_reason "${stack}")"
  if [[ "${expect}" == denied && "${status}" != CREATE_COMPLETE ]] \
     && printf '%s' "${reason}" | grep -qi 'service control policy'; then
    ok "$(printf '%-26s denied by the SCP, as the control requires' "${name}")"; PASS=$((PASS+1)); return
  fi
  printf '%s  FAIL%s  %-26s %s: %s\n' "${C_RED}" "${C_RESET}" "${name}" "${status:-unknown}" "${reason:-no reason recorded}"
  FAIL=$((FAIL+1))
}

hr
log "Tag-on-create: does compliant CloudFormation get past SCP 04?"
log "  account : ${TARGET_ACCOUNT} (Sandbox)"
log "  region  : ${AWS_DEFAULT_REGION}"
log "  subject : ${TEST_ROLE} (admin, matches no exemption)"
hr

info "Clearing leftovers from an earlier run"
for s in $(as_test aws cloudformation list-stacks \
             --query "StackSummaries[?starts_with(StackName,'${PREFIX}-') && StackStatus!='DELETE_COMPLETE'].StackName" \
             --output text 2>/dev/null | no_cr); do
  delete_stack "${s}"
  as_test aws cloudformation wait stack-delete-complete --stack-name "${s}" >/dev/null 2>&1 || true
  skip "removed leftover ${s}"
done

info "Tagged cases (expected: CREATE_COMPLETE)"
run_case s3-stack-tags      "${WORK}/s3.yaml"               created yes
run_case s3-resource-tags   "${WORK}/s3-resource-tags.yaml" created no
run_case dynamodb           "${WORK}/dynamodb.yaml"         created yes
run_case ebs                "${WORK}/ebs.yaml"              created yes
run_case ecs-service        "${WORK}/ecs.yaml"              created yes
if [[ ${HAVE_BOUNDARY} -eq 1 ]]; then
  run_case lambda           "${WORK}/lambda.yaml"           created yes \
    "ParameterKey=BoundaryArn,ParameterValue=${BOUNDARY_ARN}"
fi

# Last, because its denial is the one most likely to draw containment onto
# the subject, and every case after it would then fail for that reason.
info "Control (expected: denied by SCP 04)"
run_case s3-untagged        "${WORK}/s3.yaml"               denied no

if [[ ${KEEP} -eq 0 ]]; then
  info "Removing test stacks"
  for s in "${STACKS[@]}"; do delete_stack "${s}"; done
  for s in "${STACKS[@]}"; do
    as_test aws cloudformation wait stack-delete-complete --stack-name "${s}" >/dev/null 2>&1 \
      || warn "${s} did not delete cleanly; check it in the console"
  done
else
  skip "--keep: stacks left in place (${PREFIX}-*)"
fi

hr
log "  passed ${PASS}, failed ${FAIL}"
hr
[[ ${FAIL} -eq 0 ]]
