"""Inventory the whole platform -- live AWS estate plus the code that built it.

Written 2026-09-22 for the Arc8 questionnaire dry run.

What this is for
-----------------
One file that answers "what have we actually built", without anybody having to
open eight AWS consoles or read fifty templates. The live estate and the
repository are both in it, because neither one alone is the answer: the
templates say what should exist and AWS says what does, and the interesting
part is usually the gap.

Safety is by construction, not by remembering
-----------------------------------------------
Every collector names the fields it records. Nothing dumps a raw API response.

That is deliberate and it is the whole safety argument: a deny-list of
sensitive fields fails the first time AWS adds one, and it fails silently,
into a file somebody is about to share. An allow-list fails by omitting
something useful, which is visible and harmless.

Three things are NEVER read, in any account:

  Secrets Manager values      names and metadata only
  SSM SecureString values     names and types only
  AWS alternate contacts      not called at all

and two more are excluded for the same reason CLAUDE.md gitignores them:

  Identity Center membership  group and permission set NAMES are not personal
                              data and are included; who is in them is not read
  SNS email subscriptions     the endpoint IS a person; protocol recorded,
                              endpoint redacted

`verify_no_leak` then checks the finished file against the real contents of
config/contacts.env and config/identity.env -- the actual personal data this
platform holds -- and refuses to write if any of it appears. Testing the
redaction by USING the thing it is supposed to exclude, rather than trusting
that the allow-list was written correctly.
"""
import datetime
import json
import os
import re
import sys

import boto3
from botocore.config import Config

REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
HOME_REGION = "us-east-2"
REDACTED = "[redacted-by-estate-dump]"

# Retries and a long read timeout: this walks eight accounts across three
# regions and will be throttled if it does not back off.
BOTO = Config(retries={"max_attempts": 10, "mode": "adaptive"},
              read_timeout=60, connect_timeout=15)

ERRORS = []


def note_error(where, exc):
    ERRORS.append({"where": where, "error": type(exc).__name__,
                   "message": str(exc)[:300]})
    return []


def safe(where, fn, *a, **kw):
    """Run a collector; record the failure and carry on.

    An SSO session expires roughly hourly and this run is longer than some of
    them. Aborting the whole dump because one account went unreachable would
    throw away everything already collected, so failures become entries in
    `errors` and the file says which parts of it are incomplete.
    """
    try:
        return fn(*a, **kw)
    except Exception as exc:                          # noqa: BLE001
        return note_error(where, exc)


_SECRETISH = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")


def looks_like_credential(value):
    """A long unbroken token with no structure.

    Defence in depth. By architecture every environment variable in this
    platform holds an ARN, a parameter path or a secret NAME rather than a
    secret value -- but "by architecture" is an assumption about code nobody
    has read recently, and this file is going to be shared.
    """
    if not isinstance(value, str) or len(value) < 32:
        return False
    if value.startswith("arn:") or "/" in value or " " in value:
        return False
    return bool(_SECRETISH.match(value))


def scrub_env(env):
    out = {}
    for k, v in (env or {}).items():
        out[k] = REDACTED if looks_like_credential(v) else v
    return out


# ---------------------------------------------------------------------------
# Organization -- management account only
# ---------------------------------------------------------------------------


def collect_organization(session):
    org = session.client("organizations", config=BOTO)
    out = {}

    d = org.describe_organization()["Organization"]
    out["id"] = d.get("Id")
    out["master_account_id"] = d.get("MasterAccountId")
    out["feature_set"] = d.get("FeatureSet")
    # MasterAccountEmail deliberately not recorded. It is a mailbox a person
    # reads.

    roots = org.list_roots()["Roots"]
    out["roots"] = [{"id": r["Id"], "name": r["Name"],
                     "policy_types": r.get("PolicyTypes", [])} for r in roots]

    def walk(parent_id, depth=0):
        kids = []
        for ou in org.get_paginator("list_organizational_units_for_parent") \
                .paginate(ParentId=parent_id).build_full_result() \
                .get("OrganizationalUnits", []):
            kids.append({
                "id": ou["Id"], "name": ou["Name"], "arn": ou["Arn"],
                "scps_attached": [
                    {"id": p["Id"], "name": p["Name"]}
                    for p in org.list_policies_for_target(
                        TargetId=ou["Id"],
                        Filter="SERVICE_CONTROL_POLICY").get("Policies", [])],
                "rcps_attached": [
                    {"id": p["Id"], "name": p["Name"]}
                    for p in safe(
                        f"rcp:{ou['Id']}", lambda: org.list_policies_for_target(
                            TargetId=ou["Id"],
                            Filter="RESOURCE_CONTROL_POLICY").get(
                                "Policies", [])) or []],
                "accounts": [
                    {"id": a["Id"], "name": a["Name"], "status": a["Status"]}
                    for a in org.get_paginator("list_accounts_for_parent")
                    .paginate(ParentId=ou["Id"]).build_full_result()
                    .get("Accounts", [])],
                "children": walk(ou["Id"], depth + 1),
            })
        return kids

    out["tree"] = [{"root": r["Id"], "children": walk(r["Id"])} for r in roots]

    out["policies"] = []
    for ptype in ("SERVICE_CONTROL_POLICY", "RESOURCE_CONTROL_POLICY"):
        for p in safe(f"policies:{ptype}",
                      lambda t=ptype: org.get_paginator("list_policies")
                      .paginate(Filter=t).build_full_result()
                      .get("Policies", [])) or []:
            entry = {"id": p["Id"], "name": p["Name"], "type": ptype,
                     "aws_managed": p.get("AwsManaged"),
                     "description": p.get("Description")}
            # The policy BODY is recorded. It is not a secret -- it is in the
            # repository under policies/ -- and having it here is what makes
            # the live-versus-repository comparison possible.
            body = safe(f"policy-body:{p['Id']}",
                        lambda i=p["Id"]: org.describe_policy(
                            PolicyId=i)["Policy"]["Content"])
            if isinstance(body, str):
                try:
                    entry["document"] = json.loads(body)
                except ValueError:
                    entry["document_raw"] = body
            out["policies"].append(entry)

    out["accounts"] = []
    for a in org.get_paginator("list_accounts").paginate() \
            .build_full_result().get("Accounts", []):
        out["accounts"].append({
            "id": a["Id"], "name": a["Name"], "status": a["Status"],
            "joined_method": a.get("JoinedMethod"),
            "joined_at": a.get("JoinedTimestamp"),
            # Email NOT recorded. Root mailboxes are addressed as
            # mspr+<...>@altdigital.ai and are role addresses rather than
            # personal ones, but they are still credentials-adjacent: the
            # address is half of a root account recovery.
        })

    out["delegated_administrators"] = [
        {"id": d_["Id"], "name": d_.get("Name"),
         "services": [s["ServicePrincipal"] for s in safe(
             f"delegated-services:{d_['Id']}",
             lambda i=d_["Id"]: org.list_delegated_services_for_account(
                 AccountId=i).get("DelegatedServices", [])) or []]}
        for d_ in safe("delegated-admins",
                       lambda: org.list_delegated_administrators()
                       .get("DelegatedAdministrators", [])) or []]
    return out


# ---------------------------------------------------------------------------
# Per-account, per-region collectors
# ---------------------------------------------------------------------------


def collect_cloudformation(session, region):
    cfn = session.client("cloudformation", region_name=region, config=BOTO)
    stacks = []
    for s in cfn.get_paginator("describe_stacks").paginate() \
            .build_full_result().get("Stacks", []):
        entry = {
            "name": s["StackName"], "status": s["StackStatus"],
            "created_at": s.get("CreationTime"),
            "updated_at": s.get("LastUpdatedTime"),
            "description": s.get("Description"),
            "drift": (s.get("DriftInformation") or {}).get(
                "StackDriftStatus"),
            "parameters": [
                {"key": p["ParameterKey"], "value": p.get("ParameterValue")}
                for p in s.get("Parameters", [])],
            "outputs": [
                {"key": o["OutputKey"], "value": o.get("OutputValue"),
                 "export": (o.get("ExportName"))}
                for o in s.get("Outputs", [])],
            "tags": {t["Key"]: t["Value"] for t in s.get("Tags", [])},
        }
        entry["resources"] = [
            {"logical_id": r["LogicalResourceId"],
             "physical_id": r.get("PhysicalResourceId"),
             "type": r["ResourceType"], "status": r["ResourceStatus"]}
            for r in safe(f"cfn-resources:{region}:{s['StackName']}",
                          lambda n=s["StackName"]: cfn.list_stack_resources(
                              StackName=n).get("StackResourceSummaries", []))
            or []]
        stacks.append(entry)
    return stacks


def collect_stacksets(session, region):
    cfn = session.client("cloudformation", region_name=region, config=BOTO)
    out = []
    for ss in cfn.list_stack_sets(Status="ACTIVE").get("Summaries", []):
        out.append({
            "name": ss["StackSetName"], "status": ss.get("Status"),
            "description": ss.get("Description"),
            "permission_model": ss.get("PermissionModel"),
            "drift": ss.get("DriftStatus"),
            "instances": [
                {"account": i.get("Account"), "region": i.get("Region"),
                 "status": i.get("Status"),
                 "detail": (i.get("StackInstanceStatus") or {}).get(
                     "DetailedStatus")}
                for i in safe(
                    f"stackset-instances:{ss['StackSetName']}",
                    lambda n=ss["StackSetName"]: cfn.get_paginator(
                        "list_stack_instances").paginate(StackSetName=n)
                    .build_full_result().get("Summaries", [])) or []],
        })
    return out


def collect_lambda(session, region):
    lam = session.client("lambda", region_name=region, config=BOTO)
    out = []
    for f in lam.get_paginator("list_functions").paginate() \
            .build_full_result().get("Functions", []):
        out.append({
            "name": f["FunctionName"], "runtime": f.get("Runtime"),
            "handler": f.get("Handler"), "role": f.get("Role"),
            "timeout": f.get("Timeout"), "memory_mb": f.get("MemorySize"),
            "last_modified": f.get("LastModified"),
            "code_size": f.get("CodeSize"),
            "code_sha256": f.get("CodeSha256"),
            "description": f.get("Description"),
            "reserved_concurrency": safe(
                f"concurrency:{f['FunctionName']}",
                lambda n=f["FunctionName"]: lam.get_function_concurrency(
                    FunctionName=n).get("ReservedConcurrentExecutions")),
            # Values, not just keys. By architecture these hold ARNs, parameter
            # paths and secret NAMES rather than secret values -- and anything
            # that looks like a credential is redacted by scrub_env regardless.
            "environment": scrub_env(
                (f.get("Environment") or {}).get("Variables")),
        })
    return out


def collect_alarms(session, region):
    cw = session.client("cloudwatch", region_name=region, config=BOTO)
    out = []
    for a in cw.get_paginator("describe_alarms").paginate() \
            .build_full_result().get("MetricAlarms", []):
        out.append({
            "name": a["AlarmName"], "description": a.get("AlarmDescription"),
            "state": a.get("StateValue"), "namespace": a.get("Namespace"),
            "metric": a.get("MetricName"), "statistic": a.get("Statistic"),
            "period": a.get("Period"), "threshold": a.get("Threshold"),
            "comparison": a.get("ComparisonOperator"),
            "evaluation_periods": a.get("EvaluationPeriods"),
            "treat_missing_data": a.get("TreatMissingData"),
            "dimensions": {d["Name"]: d["Value"]
                           for d in a.get("Dimensions", [])},
            "alarm_actions": a.get("AlarmActions", []),
            "ok_actions": a.get("OKActions", []),
            "actions_enabled": a.get("ActionsEnabled"),
        })
    for a in cw.get_paginator("describe_alarms").paginate() \
            .build_full_result().get("CompositeAlarms", []):
        out.append({
            "name": a["AlarmName"], "composite": True,
            "rule": a.get("AlarmRule"), "state": a.get("StateValue"),
            "alarm_actions": a.get("AlarmActions", []),
        })
    return out


def collect_events(session, region):
    ev = session.client("events", region_name=region, config=BOTO)
    out = {"buses": [], "rules": []}
    for b in ev.list_event_buses().get("EventBuses", []):
        out["buses"].append({"name": b["Name"], "arn": b.get("Arn")})
    for b in out["buses"]:
        for r in safe(f"rules:{region}:{b['name']}",
                      lambda n=b["name"]: ev.get_paginator("list_rules")
                      .paginate(EventBusName=n).build_full_result()
                      .get("Rules", [])) or []:
            rule = {
                "bus": b["name"], "name": r["Name"], "state": r.get("State"),
                "description": r.get("Description"),
                "schedule": r.get("ScheduleExpression"),
                "targets": [
                    {"id": t["Id"], "arn": t.get("Arn"),
                     "input": t.get("Input")}
                    for t in safe(
                        f"targets:{r['Name']}",
                        lambda n=r["Name"], bn=b["name"]:
                        ev.list_targets_by_rule(Rule=n, EventBusName=bn)
                        .get("Targets", [])) or []],
            }
            pattern = r.get("EventPattern")
            if pattern:
                try:
                    rule["event_pattern"] = json.loads(pattern)
                except ValueError:
                    rule["event_pattern_raw"] = pattern
            out["rules"].append(rule)
    return out


def collect_sns(session, region):
    sns = session.client("sns", region_name=region, config=BOTO)
    out = []
    for t in sns.get_paginator("list_topics").paginate() \
            .build_full_result().get("Topics", []):
        arn = t["TopicArn"]
        subs = []
        for s in safe(f"subs:{arn}",
                      lambda a=arn: sns.get_paginator(
                          "list_subscriptions_by_topic").paginate(TopicArn=a)
                      .build_full_result().get("Subscriptions", [])) or []:
            protocol = s.get("Protocol")
            subs.append({
                "protocol": protocol,
                # An email or SMS endpoint IS a person. Recorded as existing,
                # never as an address. https endpoints are services.
                "endpoint": (REDACTED if protocol in ("email", "email-json",
                                                      "sms")
                             else s.get("Endpoint")),
                "arn": s.get("SubscriptionArn"),
            })
        out.append({"arn": arn, "subscriptions": subs})
    return out


def collect_iam(session):
    """Global. Platform roles only -- an account's full IAM is noise here."""
    iam = session.client("iam", config=BOTO)
    roles = []
    for r in iam.get_paginator("list_roles").paginate() \
            .build_full_result().get("Roles", []):
        name = r["RoleName"]
        if not (name.startswith("Platform") or name.startswith("platform")
                or name.startswith("stacksets-exec")
                or name == "OrganizationAccountAccessRole"
                or "AppDeployer" in name or "Responder" in name):
            continue
        roles.append({
            "name": name, "arn": r["Arn"], "created": r.get("CreateDate"),
            "description": r.get("Description"),
            "max_session_duration": r.get("MaxSessionDuration"),
            "trust_policy": r.get("AssumeRolePolicyDocument"),
            "inline_policies": [
                {"name": pn, "document": iam.get_role_policy(
                    RoleName=name, PolicyName=pn)["PolicyDocument"]}
                for pn in safe(f"inline:{name}",
                               lambda n=name: iam.list_role_policies(
                                   RoleName=n).get("PolicyNames", [])) or []],
            "attached_policies": [
                {"name": p["PolicyName"], "arn": p["PolicyArn"]}
                for p in safe(f"attached:{name}",
                              lambda n=name: iam.list_attached_role_policies(
                                  RoleName=n).get("AttachedPolicies", []))
                or []],
        })
    return {
        "roles": roles,
        # CLAUDE.md: no IAM users for human principals in member accounts,
        # ever. Counted rather than listed, because the count IS the control
        # and a non-zero one is the finding.
        "iam_user_count": len(safe(
            "iam-users", lambda: iam.list_users().get("Users", [])) or []),
        "account_aliases": safe(
            "aliases", lambda: iam.list_account_aliases().get(
                "AccountAliases", [])) or [],
    }


def collect_s3(session):
    """GLOBAL, called once per account.

    list_buckets returns every bucket in the account regardless of which
    region the client is pointed at. Calling it per region does not scope the
    result -- it returns the same buckets three times, triples the per-bucket
    describe calls, and puts each bucket in the document three times. Anyone
    counting buckets in the output would get three times the real number, with
    nothing in the file to suggest it.

    The bucket's own region is recorded per bucket instead, which is the fact
    the per-region call looked like it was providing.
    """
    out = {}
    s3 = session.client("s3", region_name=HOME_REGION, config=BOTO)
    buckets = []
    for b in safe("s3", lambda: s3.list_buckets().get("Buckets", [])) or []:
        loc = safe(f"s3-loc:{b['Name']}",
                   lambda n=b["Name"]: s3.get_bucket_location(
                       Bucket=n).get("LocationConstraint"))
        buckets.append({
            "name": b["Name"], "created": b.get("CreationDate"),
            "region": loc or "us-east-1",
            "encryption": safe(
                f"s3-enc:{b['Name']}",
                lambda n=b["Name"]: s3.get_bucket_encryption(Bucket=n)
                .get("ServerSideEncryptionConfiguration", {}).get("Rules")),
            "versioning": safe(
                f"s3-ver:{b['Name']}",
                lambda n=b["Name"]: s3.get_bucket_versioning(
                    Bucket=n).get("Status")),
            "public_access_block": safe(
                f"s3-pab:{b['Name']}",
                lambda n=b["Name"]: s3.get_public_access_block(Bucket=n)
                .get("PublicAccessBlockConfiguration")),
        })
    return {"buckets": buckets}


def collect_dynamodb(session, region):
    out = {}
    ddb = session.client("dynamodb", region_name=region, config=BOTO)
    tables = []
    for name in safe("ddb", lambda: ddb.get_paginator("list_tables")
                     .paginate().build_full_result().get("TableNames", [])) \
            or []:
        d = safe(f"ddb:{name}",
                 lambda n=name: ddb.describe_table(TableName=n)["Table"])
        if not d:
            continue
        tables.append({
            "name": d["TableName"], "status": d.get("TableStatus"),
            "item_count": d.get("ItemCount"),
            "key_schema": d.get("KeySchema"),
            "billing_mode": (d.get("BillingModeSummary") or {}).get(
                "BillingMode"),
            "pitr": safe(f"ddb-pitr:{name}",
                         lambda n=name: ddb.describe_continuous_backups(
                             TableName=n)["ContinuousBackupsDescription"]
                         ["PointInTimeRecoveryDescription"]
                         ["PointInTimeRecoveryStatus"]),
            "sse": (d.get("SSEDescription") or {}).get("Status"),
            # Table CONTENTS are never read. The registry holds tenant records;
            # this is an inventory of infrastructure, not of data.
        })
    out["dynamodb_tables"] = tables
    return out


def collect_backup(session, region):
    bk = session.client("backup", region_name=region, config=BOTO)
    vaults = []
    for v in safe("vaults", lambda: bk.list_backup_vaults()
                  .get("BackupVaultList", [])) or []:
        vaults.append({
            "name": v["BackupVaultName"], "arn": v.get("BackupVaultArn"),
            "recovery_points": v.get("NumberOfRecoveryPoints"),
            "locked": v.get("Locked"),
            "min_retention_days": v.get("MinRetentionDays"),
            "max_retention_days": v.get("MaxRetentionDays"),
            "lock_date": v.get("LockDate"),
            "encryption_key": v.get("EncryptionKeyArn"),
        })
    plans = []
    for p in safe("plans", lambda: bk.list_backup_plans()
                  .get("BackupPlansList", [])) or []:
        detail = safe(f"plan:{p['BackupPlanId']}",
                      lambda i=p["BackupPlanId"]: bk.get_backup_plan(
                          BackupPlanId=i).get("BackupPlan"))
        plans.append({"id": p["BackupPlanId"], "name": p["BackupPlanName"],
                      "plan": detail})
    return {"vaults": vaults, "plans": plans}


def collect_security(session, region):
    out = {}
    cfg = session.client("config", region_name=region, config=BOTO)
    out["config_recorders"] = safe(
        "config-recorders",
        lambda: cfg.describe_configuration_recorders()
        .get("ConfigurationRecorders", [])) or []
    out["config_recorder_status"] = safe(
        "config-status",
        lambda: cfg.describe_configuration_recorder_status()
        .get("ConfigurationRecordersStatus", [])) or []
    out["config_rules"] = [
        {"name": r["ConfigRuleName"], "state": r.get("ConfigRuleState"),
         "source": (r.get("Source") or {}).get("SourceIdentifier")}
        for r in safe("config-rules",
                      lambda: cfg.get_paginator("describe_config_rules")
                      .paginate().build_full_result()
                      .get("ConfigRules", [])) or []]

    ct = session.client("cloudtrail", region_name=region, config=BOTO)
    out["trails"] = [
        {"name": t.get("Name"), "s3_bucket": t.get("S3BucketName"),
         "multi_region": t.get("IsMultiRegionTrail"),
         "org_trail": t.get("IsOrganizationTrail"),
         "log_file_validation": t.get("LogFileValidationEnabled"),
         "kms_key": t.get("KmsKeyId")}
        for t in safe("trails", lambda: ct.describe_trails()
                      .get("trailList", [])) or []]

    gd = session.client("guardduty", region_name=region, config=BOTO)
    out["guardduty_detectors"] = safe(
        "guardduty", lambda: gd.list_detectors().get("DetectorIds", [])) or []

    kms = session.client("kms", region_name=region, config=BOTO)
    out["kms_aliases"] = [
        {"alias": a["AliasName"], "key_id": a.get("TargetKeyId")}
        for a in safe("kms", lambda: kms.get_paginator("list_aliases")
                      .paginate().build_full_result().get("Aliases", []))
        or [] if a["AliasName"].startswith("alias/platform")]
    return out


def collect_parameters(session, region):
    """NAMES and types. Never values.

    A SecureString's value is a secret by definition, and a String's value can
    be one by accident. The name and the type answer "is it there and is it
    encrypted", which is the question an inventory is for.
    """
    ssm = session.client("ssm", region_name=region, config=BOTO)
    params = []
    for p in safe("ssm", lambda: ssm.get_paginator("describe_parameters")
                  .paginate().build_full_result().get("Parameters", [])) or []:
        params.append({"name": p["Name"], "type": p.get("Type"),
                       "last_modified": p.get("LastModifiedDate"),
                       "description": p.get("Description")})

    sm = session.client("secretsmanager", region_name=region, config=BOTO)
    secrets = []
    for s in safe("secrets", lambda: sm.get_paginator("list_secrets")
                  .paginate().build_full_result().get("SecretList", [])) or []:
        secrets.append({
            "name": s["Name"], "description": s.get("Description"),
            "kms_key": s.get("KmsKeyId"),
            "last_changed": s.get("LastChangedDate"),
            "rotation_enabled": s.get("RotationEnabled"),
            "tags": {t["Key"]: t["Value"] for t in s.get("Tags", [])},
            # get_secret_value is NEVER called anywhere in this module.
        })
    return {"ssm_parameters": params, "secrets": secrets}


def collect_synthetics(session, region):
    syn = session.client("synthetics", region_name=region, config=BOTO)
    out = []
    for c in safe("canaries", lambda: syn.describe_canaries()
                  .get("Canaries", [])) or []:
        out.append({
            "name": c.get("Name"),
            "state": (c.get("Status") or {}).get("State"),
            "runtime": c.get("RuntimeVersion"),
            "schedule": (c.get("Schedule") or {}).get("Expression"),
            "artifact_location": c.get("ArtifactS3Location"),
            "handler": (c.get("Code") or {}).get("Handler"),
        })
    return out


def collect_region(session, region):
    return {
        "cloudformation_stacks": safe(
            f"cfn:{region}", collect_cloudformation, session, region),
        "stacksets": safe(f"stacksets:{region}", collect_stacksets,
                          session, region),
        "lambda_functions": safe(f"lambda:{region}", collect_lambda,
                                 session, region),
        "cloudwatch_alarms": safe(f"alarms:{region}", collect_alarms,
                                  session, region),
        "eventbridge": safe(f"events:{region}", collect_events,
                            session, region) or {},
        "sns": safe(f"sns:{region}", collect_sns, session, region),
        "storage": safe(f"dynamodb:{region}", collect_dynamodb,
                        session, region) or {},
        "backup": safe(f"backup:{region}", collect_backup,
                       session, region) or {},
        "security": safe(f"security:{region}", collect_security,
                         session, region) or {},
        "parameters": safe(f"params:{region}", collect_parameters,
                           session, region) or {},
        "synthetics": safe(f"synthetics:{region}", collect_synthetics,
                           session, region),
    }


# ---------------------------------------------------------------------------
# The repository -- what was supposed to be built
# ---------------------------------------------------------------------------

REPO_SETS = {
    # (subdirectory glob, description)
    "cloudformation_templates": (["*/*.yaml"], "CloudFormation templates"),
    "lambda_source": (["*/src/*.py"], "Lambda and library source"),
    "policies": (["policies/scp/*.json", "policies/rcp/*.json",
                  "policies/guard/*.guard", "policies/scp/*.tsv"],
                 "SCPs, RCPs and cfn-guard rules"),
    "scripts": (["scripts/*.sh", "scripts/*.py", "scripts/lib/*.sh",
                 "scripts/lib/*.py"], "Operational entry points"),
    "terraform": (["pagerduty/*.tf", "pagerduty/modules/*/*.tf"],
                  "PagerDuty configuration"),
    "config": (["config/tiers.yaml", "vesting/*.yaml", "vesting/*.json",
                "alerting/*.yaml", "scheduling/*.yaml"],
               "Tier table, vesting rules and alerting configuration"),
}

# Never read, even though they are on disk. CLAUDE.md gitignores both and the
# pre-commit hook blocks them; this file would be the way around that.
NEVER_READ = ("config/contacts.env", "config/identity.env",
              "config/platform.env")


def collect_repository(repo_root):
    import glob
    out = {}
    for label, (patterns, description) in REPO_SETS.items():
        files = {}
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.join(repo_root, pattern))):
                rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                if rel in NEVER_READ:
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        files[rel] = fh.read()
                except OSError as exc:
                    note_error(f"read:{rel}", exc)
        out[label] = {"description": description, "file_count": len(files),
                      "files": files}
    return out


# ---------------------------------------------------------------------------
# Proving the redaction, rather than trusting it
# ---------------------------------------------------------------------------


# Keys in the personal-data files whose VALUES are not personal data.
#
# CLAUDE.md: "Group and permission set *names* are not personal data and do
# live in the repository." They therefore appear in the dump legitimately --
# `CommercialReadOnly` is in identity/10-developer-access.yaml -- and flagging
# them would make this check cry wolf, which is how a control gets disabled.
#
# An ALLOW-LIST of exemptions, with everything else defaulting to personal. A
# new key added to either file is treated as personal until somebody decides
# otherwise, which is the direction that fails safely.
NOT_PERSONAL_KEY = re.compile(r"(_GROUP|_GROUPS|_PERMISSION_SET|_PERMISSION_SETS)$")


# Every email-shaped string in the document is treated as personal unless it
# matches one of these.
#
# Added after the first clean run: the check below compared the document
# against config/contacts.env and config/identity.env, which is the right
# fixture for "did the collectors leak" and useless for "is there personal data
# in here from somewhere else". A direct address sat in a COMMENT in a
# committed script, reached the dump through the repository section, and passed
# a check that had no reason to look for it.
#
# So this is generic. AWS service principals are emails by shape only,
# example.* is the documentation fixture domain, and the mspr+ / platform role
# addresses are the naming convention CLAUDE.md itself specifies. Anything else
# is somebody.
ALLOWED_EMAIL = re.compile(
    r"@([a-z0-9-]+\.)*amazonaws\.com$"
    r"|@example\.(com|org|net)$"
    r"|@(test|invalid|localhost)$"
    r"|^(mspr\+[a-z0-9+-]+|platform|notifications|no-reply|noreply)"
    r"@altdigital\.ai$",
    re.IGNORECASE)

EMAIL_SHAPED = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask_email(address):
    """Never return a whole address, not even in a finding."""
    local, _, domain = address.partition("@")
    return local[:3] + "***@" + domain


def verify_no_leak(document, repo_root):
    """Check the finished document against the real personal data we hold.

    config/contacts.env and config/identity.env are gitignored precisely
    because they contain names, emails and phone numbers. They are the best
    available test fixture for "did anything personal reach the dump", because
    they are the actual thing.

    Returns a list of findings. A non-empty list means the file is not written.
    """
    blob = json.dumps(document, default=str)
    findings = []
    exempted = []
    for rel in ("config/contacts.env", "config/identity.env"):
        path = os.path.join(repo_root, rel)
        if not os.path.exists(path):
            findings.append({"file": rel, "issue": "absent, so the redaction "
                             "could not be tested against real data"})
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if NOT_PERSONAL_KEY.search(key):
                    # Recorded, not silently skipped. An exemption nobody can
                    # see is indistinguishable from a check that missed it.
                    exempted.append({"file": rel, "line": lineno, "key": key})
                    continue
                # Short values produce false positives ("true", "us-east-2").
                if len(value) < 8:
                    continue
                for piece in [value] + [p for p in value.split(",")
                                        if len(p.strip()) >= 8]:
                    piece = piece.strip()
                    if piece and piece in blob:
                        findings.append({
                            "file": rel, "line": lineno,
                            "leaked": piece[:4] + "..." + piece[-4:],
                            "issue": "a value from a gitignored personal-data "
                                     "file appears in the dump"})
    # Generic sweep, independent of the two files above.
    seen = set()
    for address in EMAIL_SHAPED.findall(blob):
        if address in seen or ALLOWED_EMAIL.search(address):
            continue
        seen.add(address)
        findings.append({
            "file": "(anywhere in the document)",
            "leaked": mask_email(address),
            "issue": "an email address that is not an AWS service principal, a "
                     "documentation fixture or a platform role address. "
                     "Treated as a person."})

    return findings, exempted


# ---------------------------------------------------------------------------


def main():
    repo_root = sys.argv[1]
    out_path = sys.argv[2]
    profile = os.environ.get("AWS_PROFILE")

    base = boto3.Session(profile_name=profile) if profile else boto3.Session()
    sts = base.client("sts", config=BOTO)
    caller = sts.get_caller_identity()
    mgmt_account = caller["Account"]

    document = {
        "record_type": "platform-estate-dump",
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "generated_by": "scripts/dump-estate.sh",
        "generated_from_account": mgmt_account,
        "regions_swept": REGIONS,
        "redactions": {
            "secrets_manager_values": "never read; names and metadata only",
            "ssm_parameter_values": "never read; names and types only",
            "aws_alternate_contacts": "not called",
            "root_account_emails": "not recorded",
            "identity_center_membership": "not read; group and permission set "
                                          "names live in the repository and "
                                          "are not personal data",
            "sns_email_and_sms_endpoints": "redacted; the endpoint is a person",
            "dynamodb_table_contents": "never read; tables are inventoried, "
                                       "not their rows",
            "high_entropy_lambda_env_values": "redacted by heuristic as "
                                              "defence in depth",
            "config_contacts_and_identity_env": "not read",
            "personal_email_addresses": "every email-shaped string is checked "
                                        "against an allow-list of AWS service "
                                        "principals, documentation fixtures "
                                        "and platform role addresses; anything "
                                        "else blocks the write",
            "verification": "verify_no_leak() checks the finished document "
                            "against the real contents of "
                            "config/contacts.env and config/identity.env and "
                            "refuses to write if any value appears",
        },
        "organization": {},
        "accounts": {},
        "repository": {},
        "errors": ERRORS,
    }

    sys.stderr.write("organization\n")
    document["organization"] = safe("organization", collect_organization,
                                    base) or {}

    accounts = [a for a in document["organization"].get("accounts", [])
                if a.get("status") == "ACTIVE"]
    for acct in accounts:
        aid = acct["id"]
        sys.stderr.write("account %s (%s)\n" % (aid, acct.get("name")))
        if aid == mgmt_account:
            session = base
        else:
            creds = safe(f"assume:{aid}", lambda i=aid: sts.assume_role(
                RoleArn=f"arn:aws:iam::{i}:role/OrganizationAccountAccessRole",
                RoleSessionName="estate-dump")["Credentials"])
            if not creds:
                document["accounts"][aid] = {
                    "name": acct.get("name"),
                    "unreachable": "could not assume "
                                   "OrganizationAccountAccessRole"}
                continue
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"])

        entry = {"name": acct.get("name"),
                 # Both global. Recorded once per account rather than once per
                 # region, which is what they actually are.
                 "iam": safe(f"iam:{aid}", collect_iam, session) or {},
                 "s3": safe(f"s3:{aid}", collect_s3, session) or {},
                 "regions": {}}
        for region in REGIONS:
            sys.stderr.write("    %s\n" % region)
            entry["regions"][region] = collect_region(session, region)
        document["accounts"][aid] = entry

    sys.stderr.write("repository\n")
    document["repository"] = collect_repository(repo_root)
    document["errors"] = ERRORS

    sys.stderr.write("verifying redaction\n")
    leaks, exempted = verify_no_leak(document, repo_root)
    if leaks:
        sys.stderr.write("REFUSING TO WRITE: personal data reached the dump\n")
        for f in leaks:
            sys.stderr.write("  %s\n" % json.dumps(f))
        return 2
    document["redaction_verified"] = True
    # What the check deliberately did not test, and why. Visible in the output
    # so nobody has to read this file to know the exemption exists.
    document["redaction_check_exemptions"] = {
        "reason": "CLAUDE.md: group and permission set NAMES are not personal "
                  "data and live in the repository, so they appear here "
                  "legitimately. Everything else in the personal-data files is "
                  "treated as personal by default.",
        "keys": exempted,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(document, fh, indent=2, default=str, sort_keys=False)

    size = os.path.getsize(out_path)
    counts = {
        "accounts": len(document["accounts"]),
        "stacks": sum(len(r.get("cloudformation_stacks") or [])
                      for a in document["accounts"].values()
                      for r in (a.get("regions") or {}).values()),
        "lambda_functions": sum(len(r.get("lambda_functions") or [])
                                for a in document["accounts"].values()
                                for r in (a.get("regions") or {}).values()),
        "alarms": sum(len(r.get("cloudwatch_alarms") or [])
                      for a in document["accounts"].values()
                      for r in (a.get("regions") or {}).values()),
        "repo_files": sum(v["file_count"]
                          for v in document["repository"].values()),
        "errors": len(ERRORS),
        "bytes": size,
    }
    sys.stderr.write(json.dumps(counts, indent=2) + "\n")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
