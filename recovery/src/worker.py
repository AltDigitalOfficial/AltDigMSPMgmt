"""The restore workflow's steps. One function, one code path, a mode parameter.

Prompt 6.1, design principle P4 (design/01-design-principles.md lines 50-59):

    Any recovery, restore or containment procedure is a single artifact with a
    mode parameter. Test mode and production mode differ in target and
    teardown, never in sequence or logic.

    Why: a recovery procedure exercised only during an incident is a procedure
    that has never been tested.

So there is exactly one place in this file that reads the mode —
select_target() — and exactly one place in the state machine that branches on
it, the Choice in front of the teardown state. Everything between them runs
identically whether this is October's scheduled exercise or a real restore at
3am. If a future change needs `if mode == ...` anywhere else, that is the
signal prompt 6.1 asks for: stop and flag it rather than writing it.

What design/05-resilience.md would have told us
-----------------------------------------------
Prompt 6.1 opens "Read design/05-resilience.md, section 'The unified restore
path'". THAT FILE IS NOT IN THE DESIGN PACKAGE. The mechanism here is built
from P4 and the prompt; everywhere 05 would have supplied content the gap is
marked `DESIGN 05` in a comment rather than filled with a guess. The precedent
is baseline/50-instrumentation.yaml, which was built against a missing design
06 and needed only a data edit when 06 arrived.

The largest of those gaps, stated once here rather than repeated:

  * There is no backup infrastructure. No BackupPlan, no BackupSelection, no
    vault outside recovery/00-restore-fixture.yaml's scaffold. This workflow
    therefore does not assume a recovery point exists — "none found" is a
    first-class OUTCOME that reports the true state of the account, not an
    error. See resolve(). B-043.
  * In-guest validation is not implemented and cannot be, from here. See
    VALIDATIONS. B-044.
  * The restore duration thresholds (open-items.md B3) were undecided when this
    was built and were DECIDED on 2026-09-18: warning at 50% of the RTO, breach
    at 80%. They remain parameters, because the RTO they are a percentage of is
    per tenant. The TREND notice period was not part of B3 and is still unset —
    it carries its own placeholder flag. See evaluate_thresholds() and trend().
"""
import datetime
import json
import os

import boto3

backup = boto3.client("backup")
cloudwatch = boto3.client("cloudwatch")
ec2 = boto3.client("ec2")
events = boto3.client("events")
rds = boto3.client("rds")

ENVIRONMENT_TIER = os.environ.get("ENVIRONMENT_TIER", "dev")
BACKUP_VAULT_NAME = os.environ.get("BACKUP_VAULT_NAME", "")
SOURCE_RESOURCE_ARN = os.environ.get("SOURCE_RESOURCE_ARN", "")
RESTORE_ROLE_ARN = os.environ.get("RESTORE_ROLE_ARN", "")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AltDigital/Recovery")
RESTORE_TIMEOUT_SECONDS = int(os.environ.get("RESTORE_TIMEOUT_SECONDS", "10800"))

# questionnaire 4.1: "RTO | Default 4 hours; drives restore test thresholds".
RTO_SECONDS = int(os.environ.get("RTO_SECONDS", "14400"))

# open-items.md B3 — DECIDED 2026-09-18 by Jamie: warning at 50% of the RTO,
# breach at 80%. Prompt 6.2 required these to be parameters rather than
# constants and to default to obvious placeholders until the values were set;
# they are set, and they remain parameters because the RTO they are a
# percentage of is per tenant (questionnaire 4.1).
#
# The reasoning, kept because it is what a later reader will want to argue
# with:
#
#   warning at 50% of RTO — half the budget spent is the last point at which
#   there is still room to do something about it before the commitment is at
#   risk. A warning at 90% is a notification that it is already too late.
#
#   breach at 80% rather than 100% — crossing 100% means the RTO has already
#   been missed. An alert that fires at the moment of failure has no value as
#   an early warning; the breach tier has to precede the breach it is named
#   for.
#
# Jamie's note on adopting them: "our testing will help inform client
# discussions". So these are the PLATFORM's decided thresholds, and the monthly
# restore tests are expected to produce the evidence that shapes what is
# eventually offered to a given client. That is a reason to keep them
# per-tenant parameters rather than to keep calling them undecided.
WARNING_PERCENT_OF_RTO = float(os.environ.get("WARNING_PERCENT_OF_RTO", "50"))
BREACH_PERCENT_OF_RTO = float(os.environ.get("BREACH_PERCENT_OF_RTO", "80"))
THRESHOLDS_ARE_PLACEHOLDERS = (
    os.environ.get("THRESHOLDS_ARE_PLACEHOLDERS", "false").lower() == "true")

# Trend. Prompt 6.2: "duration trend tracking, so creep toward the RTO target
# is visible months before it breaches."
TREND_MIN_POINTS = int(os.environ.get("TREND_MIN_POINTS", "3"))
TREND_LOOKBACK_DAYS = int(os.environ.get("TREND_LOOKBACK_DAYS", "400"))

# How much notice prompt 6.2's "visible months before it breaches" should give.
#
# STILL A PLACEHOLDER, and deliberately not swept in with B3. B3 asked for the
# two DURATION tiers and Jamie decided those; it says nothing about the trend,
# and he did not decide this one. It carries its own flag
# (TREND_MONTHS_IS_PLACEHOLDER) rather than riding the duration flag, which
# would otherwise have started claiming this value was agreed the moment the
# duration percentages were.
#
# The warning is raised HERE rather than by a CloudWatch alarm, and that is not
# a stylistic choice. CloudWatch refuses any alarm whose EvaluationPeriods x
# Period exceeds 604800 seconds once the period is an hour or more, so the
# longest window an alarm can watch is ONE WEEK. This metric is written once a
# MONTH. Every alarm over it would spend three weeks in four looking at an empty
# window, which is either permanently breaching or permanently blind depending
# on TreatMissingData — and both are worse than no alarm. Found by deploying;
# the stack rolled back with "Metrics cannot be checked across more than a
# week". B-050.
#
# The workflow already has the projection in hand at the moment it records, so
# it says so in the record and lets the router decide who hears about it. Same
# single routing layer as everything else, and no metric-freshness problem.
TREND_WARN_MONTHS = float(os.environ.get("TREND_WARN_MONTHS", "6"))
TREND_MONTHS_IS_PLACEHOLDER = (
    os.environ.get("TREND_MONTHS_IS_PLACEHOLDER", "true").lower() == "true")


class StepError(Exception):
    """Raised for conditions the workflow must record rather than retry."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        # Step Functions renders $$.Execution.StartTime as an ISO string, but a
        # hand-built test event may carry epoch millis. Both are accepted,
        # because rejecting one of them would fail at record() — the last step,
        # after the restore has already happened and the evidence is the only
        # thing left to lose.
        return datetime.datetime.fromtimestamp(value / 1000.0,
                                               datetime.timezone.utc)
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _split(name):
    raw = os.environ.get(name, "")
    return [v for v in raw.replace(",", " ").split() if v]


# ---------------------------------------------------------------------------
# Targets — the first of the two things mode is allowed to change
# ---------------------------------------------------------------------------
#
# Each target is DATA, and the code below never asks which one it got. Note
# `isolation_required`: the test target declares that it must be isolated and
# the recovery target declares that it must not be, so assert_target() runs the
# same check for both and reads what to assert off the target itself. Writing
# that as `if mode == "test": check_isolation()` would have been a second place
# the mode is read, and P4's whole point is that there is one.
#
# DESIGN 05 would specify what "isolated" means for a restore test VPC.
# open-items.md V4 — "Restore test VPC is genuinely isolated from production" —
# lists the test as "Scheduled, per 05". What is asserted here is the checkable
# floor: no internet gateway, no active peering. Transit gateway attachments,
# VPC endpoints onto production services, and shared-subnet arrangements are
# NOT checked, because 05 is where the list of what counts would come from.


def targets():
    return {
        "test": {
            "name": "test",
            "vpc_id": os.environ.get("TEST_VPC_ID", ""),
            "subnet_ids": _split("TEST_SUBNET_IDS"),
            "security_group_ids": _split("TEST_SECURITY_GROUP_IDS"),
            "db_subnet_group": os.environ.get("TEST_DB_SUBNET_GROUP", ""),
            "isolation_required": True,
        },
        "recovery": {
            "name": "recovery",
            "vpc_id": os.environ.get("RECOVERY_VPC_ID", ""),
            "subnet_ids": _split("RECOVERY_SUBNET_IDS"),
            "security_group_ids": _split("RECOVERY_SECURITY_GROUP_IDS"),
            "db_subnet_group": os.environ.get("RECOVERY_DB_SUBNET_GROUP", ""),
            # Production networking, reachable by definition. Asserting
            # isolation here would fail every real recovery.
            "isolation_required": False,
        },
    }


def select_target(mode):
    """The ONE place the mode is read. P4's "differ in target"."""
    available = targets()
    if mode not in available:
        raise StepError(
            f"mode '{mode}' is neither 'test' nor 'recovery'. The workflow has "
            "exactly two targets and refuses to guess which was meant.")
    target = available[mode]
    if not target["vpc_id"] and not target["subnet_ids"]:
        raise StepError(
            f"the '{mode}' target has no VPC and no subnets configured. A "
            "restore into an unspecified network is how a test restore lands "
            "in production; this stops rather than choosing a default.")
    return target


# A route that leads off the VPC. Checked by ROUTE rather than by the presence
# of a gateway, and the distinction is not academic — it was found by deploying.
#
# baseline/40-network.yaml attaches an internet gateway to every VPC it builds,
# including one whose egress profile is `isolated`, and implements that profile
# as "no NAT gateway, no elastic IP and no default route out of the PRIVATE
# subnets". So the sandbox has a VPC named `platform-isolated` with an IGW
# attached and a public subnet routed to it, and a check for "does this VPC
# have an internet gateway" calls it not isolated when its private subnets are
# exactly that. B-047.
#
# Reachability is a property of the route table the resource's subnet uses, not
# of what is attached to the VPC somewhere. Testing the route table is both
# stricter and more accurate: it catches a transit gateway and a peering
# connection in the same pass, which a gateway-presence check had to concede it
# did not.
_ESCAPE_KEYS = (
    ("GatewayId", "igw-"),                    # internet gateway
    ("NatGatewayId", None),
    ("TransitGatewayId", None),
    ("VpcPeeringConnectionId", None),
    ("EgressOnlyInternetGatewayId", None),
    ("CarrierGatewayId", None),
)


def _escape_routes(table):
    """Routes off the VPC, split into the ones that fail isolation and the ones
    that are worth reporting.

    A GATEWAY VPC ENDPOINT (`vpce-` in a route table — S3 or DynamoDB) is
    reported and does not fail. It is not another network: it reaches an AWS
    service, through an endpoint policy, and whether a restore test VPC that
    can reach S3 counts as isolated is a design 05 question rather than one
    this code should answer by itself.

    Found by deploying. The sandbox's only private subnet routes to an S3
    gateway endpoint, and counting that as an escape would have made the
    account's correctly-isolated subnet unusable for the restore test — which
    would have read as "the isolation check works" while actually meaning
    "nowhere passes". B-047.

    Returns (escapes, service_endpoints).
    """
    escapes, endpoints = [], []
    for route in table.get("Routes", []):
        # A blackholed route reaches nothing — its target is gone. Counting it
        # would fail an isolation check on the evidence of a route that is
        # definitionally unusable.
        if route.get("State") == "blackhole":
            continue
        where = (route.get("DestinationCidrBlock")
                 or route.get("DestinationPrefixListId"))
        gateway = str(route.get("GatewayId") or "")
        if gateway.startswith("vpce-"):
            endpoints.append({"destination": where, "via": gateway})
            continue
        for key, prefix in _ESCAPE_KEYS:
            value = route.get(key)
            if not value:
                continue
            if prefix and not str(value).startswith(prefix):
                continue
            escapes.append({"destination": where, "via": value})
            break
    return escapes, endpoints


def assert_target(target):
    """Assert the target is the kind of network it claims to be.

    Returns the evidence, which is recorded. A check that runs and is not
    written down proves nothing to an auditor three months later —
    open-items.md V4 asks for a scheduled test that the restore test VPC is
    "genuinely isolated", and this is the run-time half of it.
    """
    if not target["isolation_required"]:
        return {"checked": False,
                "reason": "target declares isolation_required: false — "
                          "production networking is reachable by definition"}

    subnet_ids = target.get("subnet_ids") or []
    if not subnet_ids:
        raise StepError(
            "the test target requires isolation but names no subnets, so "
            "isolation cannot be asserted. Refusing to restore. Isolation is a "
            "property of the route tables the restored resource's subnets use; "
            "a VPC id alone does not establish it.")

    subnets = ec2.describe_subnets(SubnetIds=subnet_ids).get("Subnets", [])
    found_ids = {s["SubnetId"] for s in subnets}
    missing = [s for s in subnet_ids if s not in found_ids]
    if missing:
        raise StepError(
            f"target subnets do not exist: {', '.join(missing)}. Refusing to "
            "restore into a network that cannot be examined.")

    vpc_ids = {s["VpcId"] for s in subnets}
    if len(vpc_ids) > 1:
        raise StepError(
            f"the test target spans more than one VPC ({', '.join(sorted(vpc_ids))}). "
            "Isolation would have to hold for all of them and the target is "
            "meant to name one network.")
    vpc_id = next(iter(vpc_ids))

    tables = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", [])
    explicit = {}
    main = None
    for table in tables:
        for assoc in table.get("Associations", []):
            if assoc.get("Main"):
                main = table
            elif assoc.get("SubnetId"):
                explicit[assoc["SubnetId"]] = table

    per_subnet = []
    escapes = []
    endpoints = []
    for subnet in subnets:
        # A subnet with no explicit association uses the VPC's MAIN route
        # table. Missing that is how a subnet that looks unrouted turns out to
        # follow whatever the main table says, which is the default route in
        # most VPCs.
        table = explicit.get(subnet["SubnetId"]) or main
        if table is None:
            raise StepError(
                f"subnet {subnet['SubnetId']} has no explicit route table and "
                f"VPC {vpc_id} has no main route table, so where its traffic "
                "goes cannot be determined.")
        leaks, svc = _escape_routes(table)
        per_subnet.append({"subnet_id": subnet["SubnetId"],
                           "availability_zone": subnet["AvailabilityZone"],
                           "route_table_id": table["RouteTableId"],
                           "association": ("explicit"
                                           if subnet["SubnetId"] in explicit
                                           else "main"),
                           "escape_routes": leaks,
                           "service_endpoints": svc})
        escapes.extend(leaks)
        endpoints.extend(svc)

    evidence = {
        "checked": True,
        "vpc_id": vpc_id,
        "subnets": per_subnet,
        "escape_routes": escapes,
        # Reported, not failed on. See _escape_routes() — whether reaching S3
        # from a restore test VPC counts as isolated is design 05's call, and
        # recording it is how that decision gets made on evidence rather than
        # discovered later.
        "service_endpoints": endpoints,
        "basis": "route reachability from the target subnets. DESIGN 05 is "
                 "absent and is where the full definition would come from — "
                 "open-items.md V4",
        # Still narrower than "genuinely isolated". Said out loud so the record
        # does not read as a broader assurance than it is.
        "not_checked": ["interface VPC endpoints", "security group rules",
                        "shared subnets owned by another account",
                        "DNS resolution paths",
                        "gateway endpoint POLICIES (the endpoints themselves "
                        "are listed under service_endpoints)"],
    }
    if escapes:
        raise StepError(
            f"the restore test target is not isolated: {len(escapes)} route(s) "
            f"lead off the VPC. Evidence: {json.dumps(evidence)}")
    return evidence


# ---------------------------------------------------------------------------
# Step: resolve
# ---------------------------------------------------------------------------


def resolve(event):
    """Pick the target, pick the recovery point. Nothing is created yet.

    "No recovery point" is an OUTCOME, not an error. The platform has no backup
    plans (B-043), so in every account but the sandbox fixture this is the
    honest answer, and it is the finding that matters — an account with nothing
    to restore from is a far more serious thing to report than a slow restore.
    Raising here instead would turn a true statement about the account into a
    Lambda error, which is how a real gap becomes indistinguishable from a bug.
    """
    state = event.get("state") or {}
    mode = state.get("mode") or "test"
    target = select_target(mode)
    isolation = assert_target(target)

    source_arn = state.get("source_resource_arn") or SOURCE_RESOURCE_ARN
    explicit = state.get("recovery_point_arn") or ""

    if explicit:
        point = describe_recovery_point(explicit)
        if point is None:
            return {"outcome": "no_recovery_point", "mode": mode,
                    "target": target, "isolation": isolation,
                    "source_resource_arn": source_arn,
                    "reason": "the recovery point named in the execution input "
                              f"does not exist or is not readable: {explicit}"}
        return {"outcome": "resolved", "mode": mode, "target": target,
                "isolation": isolation, "source_resource_arn": source_arn,
                "recovery_point_arn": explicit, "recovery_point": point,
                "selection": "explicit — supplied in the execution input"}

    if not source_arn:
        return {"outcome": "no_recovery_point", "mode": mode, "target": target,
                "isolation": isolation, "source_resource_arn": "",
                "reason": "no source resource is configured for this account, "
                          "so there is nothing to look for a recovery point "
                          "of. Set SourceResourceArn, or supply one per "
                          "execution."}

    point = latest_recovery_point(source_arn)
    if point is None:
        return {"outcome": "no_recovery_point", "mode": mode, "target": target,
                "isolation": isolation, "source_resource_arn": source_arn,
                "reason": f"no completed recovery point exists for {source_arn}. "
                          "The platform has no backup plans (B-043); until one "
                          "exists this is the expected state of every account."}

    return {"outcome": "resolved", "mode": mode, "target": target,
            "isolation": isolation, "source_resource_arn": source_arn,
            "recovery_point_arn": point["RecoveryPointArn"],
            "recovery_point": point,
            "selection": "latest completed recovery point for the source"}


def describe_recovery_point(arn):
    if not BACKUP_VAULT_NAME:
        return None
    try:
        got = backup.describe_recovery_point(
            BackupVaultName=BACKUP_VAULT_NAME, RecoveryPointArn=arn)
    except backup.exceptions.ResourceNotFoundException:
        return None
    return _point_summary(got)


def latest_recovery_point(source_arn):
    """Most recent COMPLETED recovery point for a resource.

    ListRecoveryPointsByResource returns newest first, but the API has no
    filter for status, so the test is here. Restoring from a PARTIAL recovery
    point is the failure that looks like success: the job completes, and the
    data is short.
    """
    paginator = backup.get_paginator("list_recovery_points_by_resource")
    # ManagedByAWSBackupOnly, and it is not an optimisation.
    #
    # It defaults to FALSE, which asks AWS Backup to also enumerate recovery
    # points it does not manage — raw EBS snapshots taken by anything else. To
    # do that Backup queries EC2 using the CALLER's permissions, so the call
    # fails with AccessDenied unless the role also holds ec2:DescribeSnapshots.
    # That is what happened on the first live run: the policy granted every
    # backup:* action the workflow needs and the call was still denied, with
    # CloudTrail recording errorMessage "An unknown error occurred" and naming
    # nothing. B-048.
    #
    # Granting ec2:DescribeSnapshots would have made the error go away and been
    # the wrong fix. A recovery point that AWS Backup does not manage cannot be
    # restored by backup:StartRestoreJob at all, so listing one only defers the
    # failure to the restore step, where it costs a started job and a worse
    # message. The workflow restores through Backup; it should only ever
    # consider what Backup can restore.
    for page in paginator.paginate(ResourceArn=source_arn,
                                   ManagedByAWSBackupOnly=True):
        for point in page.get("RecoveryPoints", []):
            if point.get("Status") != "COMPLETED":
                continue
            summary = _point_summary(point)
            summary["ResourceArn"] = summary.get("ResourceArn") or source_arn
            if summary.get("ResourceType"):
                return summary
            # ListRecoveryPointsByResource does not return ResourceType — it is
            # implied by the resource you asked about, so the API leaves it out.
            # DescribeRecoveryPoint does return it, along with the sizes and the
            # encryption key, so the authoritative record is fetched for the ONE
            # point that was selected rather than for every point listed.
            #
            # Without this the type arrives empty and start() refuses the
            # restore with "no restore metadata override for resource type ''",
            # which reads as an unsupported resource rather than a missing
            # field. B-049.
            detailed = describe_recovery_point(summary["RecoveryPointArn"])
            if detailed and detailed.get("ResourceType"):
                detailed["ResourceArn"] = detailed.get("ResourceArn") or source_arn
                return detailed
            # Last resort: the source ARN says what the resource is. Reached
            # only if the recovery point is in a vault other than the configured
            # one, where DescribeRecoveryPoint cannot see it.
            summary["ResourceType"] = _resource_type_from_arn(source_arn)
            return summary
    return None


def _resource_type_from_arn(arn):
    """AWS Backup's resource type for a source ARN.

    Only the types this workflow can place into a target — anything else is
    refused by start() with the list of what it handles, which is a better
    failure than restoring into whatever network the source was in.
    """
    # Field 5 is the resource segment in both ARN layouts, and that is the
    # whole subtlety. EC2 uses `.../volume/vol-123` (slash) while RDS uses
    # `...:cluster:name` (colon), so splitting on ':' and reading the LAST
    # field gives "vol-123" for one and the cluster's NAME for the other —
    # which made every Aurora cluster look like a plain RDS instance.
    parts = str(arn).split(":")
    if len(parts) < 6:
        return ""
    service = parts[2]
    resource = parts[5]
    if service == "ec2" and resource.startswith("volume/"):
        return "EBS"
    if service == "rds":
        return "Aurora" if resource.startswith("cluster") else "RDS"
    return ""


def _point_summary(point):
    """Only the fields the workflow uses, converted to JSON-safe types.

    Step Functions carries this between states, and a datetime in a Lambda
    return value fails the STATE TRANSITION rather than the function — an error
    that names the state machine and never the offending field.
    """
    created = point.get("CreationDate") or point.get("CompletionDate")
    return {
        "RecoveryPointArn": point.get("RecoveryPointArn"),
        "ResourceArn": point.get("ResourceArn"),
        "ResourceType": point.get("ResourceType"),
        "Status": point.get("Status"),
        "BackupSizeBytes": (point.get("BackupSizeBytes")
                            or point.get("BackupSizeInBytes")),
        "EncryptionKeyArn": point.get("EncryptionKeyArn"),
        "CreationDate": (created.isoformat()
                         if hasattr(created, "isoformat") else created),
    }


# ---------------------------------------------------------------------------
# Step: start
# ---------------------------------------------------------------------------
#
# The restore metadata comes from AWS Backup itself
# (GetRecoveryPointRestoreMetadata) and then has the TARGET fields overridden.
# Building it by hand would mean maintaining a copy of every resource type's
# required keys, and AWS adds to those; the override functions below only need
# to know which of those keys are about placement.


def _override_ebs(metadata, target, subnet_azs_list):
    az = subnet_azs_list[0] if subnet_azs_list else metadata.get("availabilityZone")
    metadata["availabilityZone"] = az
    # A restored volume must not carry the source volume's id forward. AWS
    # ignores the key on restore, but leaving it in makes the recorded metadata
    # read as though the original were being overwritten.
    metadata.pop("volumeId", None)
    return metadata, {"availability_zone": az}


def _override_rds(metadata, target, subnet_azs_list):
    placed = {}
    if target.get("db_subnet_group"):
        metadata["DBSubnetGroupName"] = target["db_subnet_group"]
        placed["db_subnet_group"] = target["db_subnet_group"]
    if target.get("security_group_ids"):
        metadata["VpcSecurityGroupIds"] = ",".join(target["security_group_ids"])
        placed["security_group_ids"] = target["security_group_ids"]
    # Never publicly addressable, in either mode. A restored copy of production
    # data reachable from the internet is a worse outcome than a failed test.
    # This is not a target-selection question — it is true of both targets,
    # which is exactly why it is here and not in the target data.
    metadata["PubliclyAccessible"] = "false"
    return metadata, placed


OVERRIDES = {
    "EBS": _override_ebs,
    "RDS": _override_rds,
    "Aurora": _override_rds,
}


def subnet_azs(subnet_ids):
    if not subnet_ids:
        return []
    got = ec2.describe_subnets(SubnetIds=subnet_ids).get("Subnets", [])
    return [s["AvailabilityZone"] for s in got]


def start(event):
    state = event.get("state") or {}
    resolved = state.get("resolved") or {}
    target = resolved["target"]
    point = resolved["recovery_point"]
    resource_type = point.get("ResourceType") or ""

    override = OVERRIDES.get(resource_type)
    if override is None:
        raise StepError(
            f"no restore metadata override for resource type '{resource_type}'. "
            "The workflow can only place a restore into a chosen target for "
            "types whose placement keys it knows; restoring without the "
            "override would put it wherever the source was, which in test mode "
            "means production. Types handled: " + ", ".join(sorted(OVERRIDES)))

    if not RESTORE_ROLE_ARN:
        raise StepError("no restore role configured; AWS Backup cannot restore "
                        "without a role to assume.")

    metadata = backup.get_recovery_point_restore_metadata(
        BackupVaultName=BACKUP_VAULT_NAME,
        RecoveryPointArn=resolved["recovery_point_arn"],
    ).get("RestoreMetadata", {})

    metadata, placement = override(dict(metadata), target,
                                   subnet_azs(target.get("subnet_ids") or []))

    job = backup.start_restore_job(
        RecoveryPointArn=resolved["recovery_point_arn"],
        # Every value a string. The API rejects a bool or an int with
        # "Invalid type for parameter Metadata", naming the map and not the key.
        Metadata={k: str(v) for k, v in metadata.items()},
        IamRoleArn=RESTORE_ROLE_ARN,
        ResourceType=resource_type,
    )
    return {
        "restore_job_id": job["RestoreJobId"],
        "resource_type": resource_type,
        "placement": placement,
        "started_at": _now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Step: poll
# ---------------------------------------------------------------------------


TERMINAL_OK = {"COMPLETED"}
TERMINAL_BAD = {"ABORTED", "FAILED"}


def poll(event):
    """One look at the restore job. The waiting is the state machine's job.

    A Lambda that sleeps until the restore finishes bills for the sleep and
    dies at fifteen minutes. A Wait state costs nothing and has no ceiling,
    which is what lets the same workflow cover a 1 GiB test volume and a
    multi-terabyte production database without a second code path.
    """
    state = event.get("state") or {}
    restore = state.get("restore") or {}
    job_id = restore.get("restore_job_id")
    job = backup.describe_restore_job(RestoreJobId=job_id)

    started = _parse_time(restore.get("started_at")) or _now()
    elapsed = (_now() - started).total_seconds()
    status = job.get("Status", "UNKNOWN")

    if status not in TERMINAL_OK and status not in TERMINAL_BAD \
            and elapsed > RESTORE_TIMEOUT_SECONDS:
        # Not an error, and not a failure: the job is still running and may yet
        # succeed. The workflow stops waiting and says so, because an execution
        # that waits forever produces no record at all — and a missing record
        # is the one failure mode nothing downstream can see.
        return {"status": "TIMED_OUT", "aws_status": status,
                "elapsed_seconds": elapsed,
                "created_resource_arn": job.get("CreatedResourceArn"),
                "reason": (f"restore job {job_id} was still {status} after "
                           f"{int(elapsed)}s (limit {RESTORE_TIMEOUT_SECONDS}s). "
                           "The job is NOT cancelled — it continues, and any "
                           "resource it creates will be reported as an orphan "
                           "by teardown rather than deleted.")}

    return {
        "status": status,
        "aws_status": status,
        "elapsed_seconds": elapsed,
        "created_resource_arn": job.get("CreatedResourceArn"),
        "percent_done": job.get("PercentDone"),
        "reason": job.get("StatusMessage"),
    }


# ---------------------------------------------------------------------------
# Step: claim
# ---------------------------------------------------------------------------


EXECUTION_TAG = "platform-restore-execution"
MODE_TAG = "platform-restore-mode"


def claim(event):
    """Stamp the restored resource with the execution that created it.

    This is what makes teardown safe, and it runs in BOTH modes — a resource
    restored during a real recovery should say where it came from just as
    clearly as a test copy, and an auditor asking "what is this volume" gets an
    answer either way. Only teardown's use of the tag is test-only, not the tag
    itself, so this is not a second place the mode is read: the value differs,
    the behaviour does not.

    A failure to tag is NOT fatal. Validation still runs and the record is still
    written; what is lost is teardown's ability to prove provenance, so the
    resource is reported as an orphan instead of removed. Raising here would
    trade a disk left running for a restore test with no result at all.
    """
    state = event.get("state") or {}
    resolved = state.get("resolved") or {}
    mode = resolved.get("mode") or "test"
    arn = (state.get("poll") or {}).get("created_resource_arn") or ""
    resource_type = (resolved.get("recovery_point") or {}).get("ResourceType") or ""
    execution_id = event.get("execution_id") or ""

    if not arn:
        return {"tagged": False, "reason": "the restore created no resource"}

    tags = {
        EXECUTION_TAG: execution_id,
        MODE_TAG: mode,
        "platform-managed": "true",
        "platform-purpose": f"restore-{mode}",
    }
    ident = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    try:
        if resource_type == "EBS":
            ec2.create_tags(Resources=[ident],
                            Tags=[{"Key": k, "Value": v}
                                  for k, v in tags.items()])
        elif resource_type in ("RDS", "Aurora"):
            rds.add_tags_to_resource(
                ResourceName=arn,
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
        else:
            return {"tagged": False, "tags": tags,
                    "reason": f"no tagging implementation for {resource_type}"}
    except Exception as exc:                       # noqa: BLE001
        return {"tagged": False, "tags": tags,
                "reason": f"{type(exc).__name__}: {exc}"}
    return {"tagged": True, "tags": tags, "resource": ident}


# ---------------------------------------------------------------------------
# Step: validate
# ---------------------------------------------------------------------------
#
# Prompt 6.1 names five validation steps: "resource health, database connects,
# row counts plausible, volumes mount, checksums match."
#
# All five appear below BY NAME. Four of them are NOT IMPLEMENTED, and each
# says why rather than being quietly dropped. The reason is the same for all
# four: they require code running INSIDE the restored guest, or with network
# reachability to it, and this Lambda runs in neither place.
#
# The obvious fix — attach this Lambda to the test VPC — breaks P4. A Lambda's
# VPC configuration is static, so it would need two functions, one per target,
# and "two functions that must stay identical" is precisely the two-procedures
# arrangement P4 exists to prevent. The mode-neutral answer is an executor
# launched INTO the target subnet at run time (a Fargate task taking the subnet
# as a parameter): one state, one task definition, genuinely P4-clean. But
# choosing its shape, its image, and above all where the database credentials
# come from is design 05's decision, not a build decision.
#
# It is NOT built speculatively. There is no disabled branch and no placeholder
# state in the ASL, because a mechanism that does not exist must not be
# described as though it does.
#
# What this means for the run's verdict: a partially validated restore is not
# reported as a passing restore test. `coverage` says control-plane, every
# not_implemented check is listed, and the dossier carries both. It does not
# raise a ticket per run — the gap is one backlog item (B-044), not a monthly
# reminder that the same thing is still true, which is how a real signal gets
# trained out of a rotation.


def _check_resource_exists(ctx):
    if not ctx["arn"]:
        return "fail", "the restore job reported no created resource"
    if ctx["described"] is None:
        return "fail", f"{ctx['arn']} was reported created and cannot be read back"
    return "pass", f"{ctx['arn']} exists"


def _check_resource_health(ctx):
    """Prompt 6.1's "resource health". The one of the five that runs."""
    if ctx["described"] is None:
        return "fail", "no resource to check"
    if ctx["type"] == "EBS":
        state = ctx["described"].get("State")
        return ("pass" if state in ("available", "in-use") else "fail",
                f"volume state is '{state}'")
    if ctx["type"] in ("RDS", "Aurora"):
        status = ctx["described"].get("DBInstanceStatus")
        return ("pass" if status == "available" else "fail",
                f"instance status is '{status}'")
    return "not_applicable", f"no health definition for resource type {ctx['type']}"


def _check_target_placement(ctx):
    """Did the restore land where the mode asked for?

    This is the check that proves target selection worked, and in TEST mode it
    is the most important assertion here: a test restore that quietly landed in
    production networking is the exact failure the isolated-VPC arrangement
    exists to prevent, and without this check it would look like a clean pass.
    """
    if ctx["described"] is None:
        return "fail", "no resource to check"
    placement = (ctx["state"].get("restore") or {}).get("placement") or {}
    if ctx["type"] == "EBS":
        want = placement.get("availability_zone")
        got = ctx["described"].get("AvailabilityZone")
        if not want:
            return "not_applicable", "no availability zone was overridden"
        return ("pass" if want == got else "fail",
                f"restored into {got}; target was {want}")
    if ctx["type"] in ("RDS", "Aurora"):
        want = placement.get("db_subnet_group")
        got = (ctx["described"].get("DBSubnetGroup") or {}).get("DBSubnetGroupName")
        if not want:
            return "not_applicable", "no DB subnet group was overridden"
        return ("pass" if want == got else "fail",
                f"restored into subnet group {got}; target was {want}")
    return "not_applicable", f"no placement definition for {ctx['type']}"


def _check_encryption_preserved(ctx):
    if ctx["described"] is None:
        return "fail", "no resource to check"
    if ctx["type"] == "EBS":
        enc = bool(ctx["described"].get("Encrypted"))
    elif ctx["type"] in ("RDS", "Aurora"):
        enc = bool(ctx["described"].get("StorageEncrypted"))
    else:
        return "not_applicable", f"no encryption definition for {ctx['type']}"
    # A restore that silently drops encryption produces an unencrypted copy of
    # production data — a finding in its own right, regardless of whether the
    # restore otherwise "worked".
    return ("pass" if enc else "fail",
            "encrypted at rest" if enc else "RESTORED UNENCRYPTED")


def _check_geometry_matches_source(ctx):
    """Size and shape against the recovery point.

    The weakest form of "the data is all there" available without reading it,
    and worth having for one reason: a restore that produces a correctly
    configured, healthy, EMPTY resource passes every other control-plane check.
    """
    if ctx["described"] is None:
        return "fail", "no resource to check"
    point = ctx["point"] or {}
    if ctx["type"] == "EBS":
        size_gib = ctx["described"].get("Size")
        backup_bytes = point.get("BackupSizeBytes")
        if not backup_bytes:
            return "not_applicable", "the recovery point reports no backup size"
        # Deliberately a floor, not an equality. A snapshot is compressed and
        # sparse, so backup bytes are routinely far below volume bytes; the only
        # sound assertion is that the volume is not SMALLER than the data.
        ok = (size_gib or 0) * (1024 ** 3) >= backup_bytes
        return ("pass" if ok else "fail",
                f"volume is {size_gib} GiB; recovery point holds "
                f"{backup_bytes} bytes")
    return "not_applicable", f"no geometry definition for {ctx['type']}"


_NOT_IMPLEMENTED = (
    "requires an executor inside the target subnet. This Lambda runs in neither "
    "target VPC, and attaching it to one would need a second function per mode, "
    "which breaks P4. design/05-resilience.md is absent and is where the "
    "executor's shape and the credential source would be specified. B-044."
)


def _check_database_connects(ctx):
    if ctx["type"] not in ("RDS", "Aurora"):
        return "not_applicable", f"{ctx['type']} is not a database"
    return "not_implemented", _NOT_IMPLEMENTED


def _check_row_counts_plausible(ctx):
    if ctx["type"] not in ("RDS", "Aurora"):
        return "not_applicable", f"{ctx['type']} holds no rows"
    return "not_implemented", (
        "depends on 'database connects', and additionally on a per-tenant "
        "expectation of what a plausible row count IS — which design 05 would "
        "define and the questionnaire does not collect. " + _NOT_IMPLEMENTED)


def _check_volumes_mount(ctx):
    if ctx["type"] != "EBS":
        return "not_applicable", f"{ctx['type']} is not a volume"
    return "not_implemented", (
        "a restored volume is created unattached. Mounting it requires an "
        "instance to attach it to and an agent to run mount inside. "
        + _NOT_IMPLEMENTED)


def _check_checksums_match(ctx):
    return "not_implemented", (
        "a checksum requires reading the restored bytes, and nothing in the "
        "control plane can. It also requires a checksum recorded AT BACKUP "
        "TIME to compare against, which no backup plan exists to produce. "
        + _NOT_IMPLEMENTED)


# The third element marks the five checks prompt 6.1 names, so a reader of the
# record can see at a glance which of the prompt's own checks actually ran.
VALIDATIONS = [
    ("resource_exists", _check_resource_exists, False),
    ("resource_health", _check_resource_health, True),
    ("target_placement", _check_target_placement, False),
    ("encryption_preserved", _check_encryption_preserved, False),
    ("geometry_matches_source", _check_geometry_matches_source, False),
    ("database_connects", _check_database_connects, True),
    ("row_counts_plausible", _check_row_counts_plausible, True),
    ("volumes_mount", _check_volumes_mount, True),
    ("checksums_match", _check_checksums_match, True),
]


def describe_restored(resource_type, arn):
    """Read the restored resource back. None if there is nothing to read."""
    if not arn:
        return None
    ident = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    try:
        if resource_type == "EBS":
            got = ec2.describe_volumes(VolumeIds=[ident]).get("Volumes", [])
            return got[0] if got else None
        if resource_type in ("RDS", "Aurora"):
            got = rds.describe_db_instances(
                DBInstanceIdentifier=ident).get("DBInstances", [])
            return got[0] if got else None
    except Exception as exc:                       # noqa: BLE001
        # Raised with the API's own error, not swallowed into None. B-042 was
        # exactly this shape: two describe calls denied by a missing permission,
        # swallowed into "cannot tell", and two alarms that were never created
        # in any account as a result. The message has to name the permission
        # problem when that is what it is.
        raise StepError(f"could not read back {arn}: {type(exc).__name__}: {exc}")
    return None


def validate(event):
    state = event.get("state") or {}
    resolved = state.get("resolved") or {}
    poll_result = state.get("poll") or {}
    resource_type = (resolved.get("recovery_point") or {}).get("ResourceType") or ""
    arn = poll_result.get("created_resource_arn")

    try:
        described = describe_restored(resource_type, arn)
        read_error = None
    except StepError as exc:
        described = None
        read_error = str(exc)

    ctx = {"arn": arn, "type": resource_type, "described": described,
           "point": resolved.get("recovery_point"), "state": state}

    results = []
    for name, fn, from_prompt in VALIDATIONS:
        try:
            status, detail = fn(ctx)
        except Exception as exc:                   # noqa: BLE001
            status, detail = "fail", f"{type(exc).__name__}: {exc}"
        results.append({"check": name, "status": status, "detail": detail,
                        "named_in_prompt_6_1": from_prompt})

    failed = [r["check"] for r in results if r["status"] == "fail"]
    skipped = [r["check"] for r in results if r["status"] == "not_implemented"]

    return {
        "passed": not failed and read_error is None,
        "failed": failed,
        "read_error": read_error,
        "results": results,
        # Stated on every run, so no readout can imply more was checked than was.
        "coverage": "control-plane",
        "not_implemented": skipped,
        "coverage_note": (
            "Control-plane checks only. In-guest validation (database connects, "
            "row counts, mount, checksums) is not implemented — B-044. A pass "
            "here means the resource came back correctly configured and "
            "healthy; it does NOT mean the data inside it was read."),
    }


# ---------------------------------------------------------------------------
# Step: record
# ---------------------------------------------------------------------------


def classify(state):
    """One outcome for the run, from whatever the earlier steps left behind.

    Written as a cascade over what IS in the state rather than as a flag each
    step sets, so that a step which died before setting its flag still
    classifies — the Catch path arrives here with $.error and nothing else.
    """
    if state.get("error"):
        return "engine_error", _error_text(state["error"])

    resolved = state.get("resolved") or {}
    if resolved.get("outcome") == "no_recovery_point":
        return "no_recovery_point", resolved.get("reason") or "no recovery point"

    poll_result = state.get("poll") or {}
    status = poll_result.get("status")
    if status == "TIMED_OUT":
        return "restore_timed_out", poll_result.get("reason") or "timed out"
    if status in TERMINAL_BAD:
        return "restore_failed", (
            f"restore job ended {status}: "
            f"{poll_result.get('reason') or 'no reason given'}")
    if status not in TERMINAL_OK:
        return "engine_error", (
            f"recording was reached with the restore job in state '{status}', "
            "which no path through the state machine should allow")

    validation = state.get("validation") or {}
    if not validation:
        return "engine_error", "the restore completed and validation never ran"
    if not validation.get("passed"):
        return "validation_failed", (
            "restore completed but validation failed: "
            + ", ".join(validation.get("failed") or [])
            + (f"; {validation['read_error']}"
               if validation.get("read_error") else ""))
    return "success", "restored and validated"


def _error_text(err):
    if isinstance(err, dict):
        return f"{err.get('Error', 'Error')}: {err.get('Cause', '')}"[:1000]
    return str(err)[:1000]


def evaluate_thresholds(elapsed_seconds):
    """Prompt 6.2's two tiers. 50% and 80% of the RTO, decided 2026-09-18 (B3)."""
    warn_at = RTO_SECONDS * (WARNING_PERCENT_OF_RTO / 100.0)
    breach_at = RTO_SECONDS * (BREACH_PERCENT_OF_RTO / 100.0)
    if elapsed_seconds >= breach_at:
        tier = "breach"
    elif elapsed_seconds >= warn_at:
        tier = "warning"
    else:
        tier = "within"
    return {
        "tier": tier,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "rto_seconds": RTO_SECONDS,
        "percent_of_rto": (round(100.0 * elapsed_seconds / RTO_SECONDS, 1)
                           if RTO_SECONDS else None),
        "warning_at_seconds": round(warn_at, 1),
        "breach_at_seconds": round(breach_at, 1),
        "warning_percent_of_rto": WARNING_PERCENT_OF_RTO,
        "breach_percent_of_rto": BREACH_PERCENT_OF_RTO,
        # Travels with every threshold result, all the way to the Jira ticket
        # and the PagerDuty payload.
        "thresholds_are_placeholders": THRESHOLDS_ARE_PLACEHOLDERS,
        "decided": "B3 — 50%/80% of RTO, decided 2026-09-18",
    }


def duration_history(mode, resource_type):
    """Past durations for this account, this mode, this resource type.

    Read BEFORE the current run is published, and the current value appended in
    memory afterwards. Reading back a datapoint written seconds earlier is a
    race against CloudWatch's own ingestion, and losing that race would silently
    drop the newest point — the one that matters most to a trend.
    """
    end = _now()
    start = end - datetime.timedelta(days=TREND_LOOKBACK_DAYS)
    got = cloudwatch.get_metric_statistics(
        Namespace=METRIC_NAMESPACE,
        MetricName="RestoreDurationSeconds",
        Dimensions=[{"Name": "Mode", "Value": mode},
                    {"Name": "ResourceType", "Value": resource_type}],
        StartTime=start, EndTime=end,
        Period=86400, Statistics=["Maximum"],
    ).get("Datapoints", [])
    return sorted(({"at": d["Timestamp"], "seconds": d["Maximum"]} for d in got),
                  key=lambda d: d["at"])


def trend(history):
    """Least squares over (day, duration).

    Prompt 6.2's "creep toward the RTO target is visible months before it
    breaches". A monthly test produces twelve points a year, which is too few
    for anything clever and exactly enough for a slope. What it answers is the
    only question worth asking of it: at this rate of change, how long until a
    run crosses the RTO?
    """
    if len(history) < TREND_MIN_POINTS:
        return {"status": "insufficient_history",
                "points": len(history),
                "needed": TREND_MIN_POINTS,
                "note": "a trend from fewer points would be noise presented as "
                        "a projection"}

    base = history[0]["at"]
    xs = [(p["at"] - base).total_seconds() / 86400.0 for p in history]
    ys = [p["seconds"] for p in history]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return {"status": "insufficient_history", "points": n,
                "note": "every datapoint carries the same timestamp"}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom

    result = {"status": "computed", "points": n,
              "slope_seconds_per_day": round(slope, 4),
              "slope_seconds_per_month": round(slope * 30.0, 2),
              "latest_seconds": round(ys[-1], 1),
              "rto_seconds": RTO_SECONDS}
    if slope <= 0:
        result["direction"] = "flat or improving"
        result["projected_months_to_rto"] = None
        return result
    result["direction"] = "increasing"
    headroom = max(RTO_SECONDS - ys[-1], 0.0)
    months = round(headroom / (slope * 30.0), 1)
    result["projected_months_to_rto"] = months
    result["warn_below_months"] = TREND_WARN_MONTHS
    result["warn_months_is_placeholder"] = TREND_MONTHS_IS_PLACEHOLDER
    # The finding prompt 6.2 asks for: creep that is visible months before it
    # breaches. Nothing is wrong yet, which is the entire point — it is worth
    # acting on precisely while there is still time to act.
    result["creep_warning"] = months < TREND_WARN_MONTHS
    return result


# Outcome -> the routing class this workflow DECLARES, or None to let the router
# decide from the account's tier.
#
# Declaring is only ever a DOWNGRADE, and the router enforces that: an emitter
# may say "this is less than a page", never "this is a page". Escalation stays
# with the tier table in design/06a, which is the single place that decides who
# gets woken. B-041 is what that rule is written against — a boolean standing in
# for 06a's five classes sent non-paging alarms to a PagerDuty-subscribed topic
# and produced five false pages.
#
# So breach, failure and error declare NOTHING and are routed by tier, which is
# what makes prompt 6.2's "breach tier: PagerDuty page" true wherever the tier
# pages. Warning declares digest_and_jira, which is 6.2's warning tier exactly:
# a ticket and the digest email, no page.
DECLARED_ROUTING = {
    ("success", "within"): "digest",
    ("success", "warning"): "digest_and_jira",
    ("success", "breach"): None,
    "no_recovery_point": "digest_and_jira",
    "restore_failed": None,
    "restore_timed_out": None,
    "validation_failed": None,
    "engine_error": None,
}


def declared_routing(outcome, duration_tier):
    if outcome == "success":
        return DECLARED_ROUTING.get((outcome, duration_tier))
    return DECLARED_ROUTING.get(outcome)


def record(event):
    state = event.get("state") or {}
    resolved = state.get("resolved") or {}
    mode = resolved.get("mode") or state.get("mode") or "test"
    point = resolved.get("recovery_point") or {}
    resource_type = point.get("ResourceType") or "unknown"

    started = _parse_time(event.get("started_at"))
    now = _now()
    # From execution start, not from the restore job's start. The RTO clock in a
    # real recovery begins when the decision to restore is made, and every
    # second spent resolving which recovery point to use is inside it.
    elapsed = (now - started).total_seconds() if started else None

    outcome, reason = classify(state)
    if outcome == "success" and elapsed is not None:
        thresholds = evaluate_thresholds(elapsed)
    else:
        thresholds = {
            "tier": "not_evaluated",
            "reason": "duration thresholds are only meaningful for a restore "
                      "that completed and validated; this run did not",
            "thresholds_are_placeholders": THRESHOLDS_ARE_PLACEHOLDERS,
        }

    history = duration_history(mode, resource_type)
    if outcome == "success" and elapsed is not None:
        history = history + [{"at": now, "seconds": elapsed}]
    trend_result = trend(history)

    publish_metrics(mode, resource_type, outcome, elapsed, trend_result)

    resource_id = point.get("ResourceArn") or "no-recovery-point"
    record_body = {
        "record_type": "restore-test-result",
        "recorded_at": now.isoformat(),
        "mode": mode,
        "environment_tier": ENVIRONMENT_TIER,
        "execution_id": event.get("execution_id"),
        "outcome": outcome,
        "reason": reason,
        "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        "rto": thresholds,
        "trend": _jsonable(trend_result),
        "target": resolved.get("target"),
        "isolation": resolved.get("isolation"),
        "recovery_point": point,
        "restore": state.get("restore"),
        "restore_job": state.get("poll"),
        "claim": state.get("claim"),
        "validation": state.get("validation"),
    }
    routing = declared_routing(outcome, thresholds.get("tier"))
    if outcome == "success" and trend_result.get("creep_warning") \
            and routing == "digest":
        # A run that was comfortably inside the RTO but is trending at it. Only
        # ever raises `digest` to `digest_and_jira` — a dated ticket — and never
        # touches a run that already warned or breached, because those are
        # already being routed on the stronger signal.
        routing = "digest_and_jira"
        record_body["reason"] = (
            f"{reason}; duration is trending toward the RTO and is projected to "
            f"cross it in {trend_result.get('projected_months_to_rto')} months")

    emit(record_body, routing, resource_id)
    print(json.dumps(record_body, default=str))

    return {
        "outcome": outcome,
        "reason": reason,
        "routing": routing,
        "elapsed_seconds": record_body["elapsed_seconds"],
        "duration_tier": thresholds.get("tier"),
        "created_resource_arn": (state.get("poll") or {}).get("created_resource_arn"),
    }


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def publish_metrics(mode, resource_type, outcome, elapsed, trend_result):
    """Two dimension sets, deliberately.

    The DURATION metric carries ResourceType, because the trend reads it back
    and a trend that silently mixed an EBS volume's restore times with a
    database's would be arithmetic on two unrelated series.

    Everything an ALARM watches carries Mode alone. An alarm has to name its
    dimensions exactly, and `resource_type` comes from the recovery point — so
    it is "unknown" on precisely the runs that found no recovery point, which
    are the runs most worth alarming on. An alarm dimensioned on it would go
    blind at the moment it mattered. There is one source resource per account
    (SourceResourceArn is singular), so the dimension buys nothing here anyway.
    """
    by_mode = [{"Name": "Mode", "Value": mode}]
    by_type = by_mode + [{"Name": "ResourceType", "Value": resource_type}]
    data = [
        # Published on EVERY run, including failures. This is what the "the
        # monthly test stopped happening" alarm watches: a metric written only
        # on success cannot distinguish a failing test from an absent one, and
        # the absent one is the more dangerous of the two.
        {"MetricName": "RestoreRunsRecorded", "Dimensions": by_mode,
         "Value": 1, "Unit": "Count"},
        {"MetricName": "RestoreSucceeded", "Dimensions": by_mode,
         "Value": 1 if outcome == "success" else 0, "Unit": "Count"},
    ]
    if outcome == "success" and elapsed is not None:
        data.append({"MetricName": "RestoreDurationSeconds",
                     "Dimensions": by_type, "Value": elapsed, "Unit": "Seconds"})
    projected = trend_result.get("projected_months_to_rto")
    if projected is not None:
        data.append({"MetricName": "ProjectedMonthsToRtoBreach",
                     "Dimensions": by_mode, "Value": projected, "Unit": "Count"})
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def emit(record_body, routing, resource_id):
    """Onto the account's default bus, shaped as a dossier.

    NOT a second routing path. alerting/src/router.py is the single routing
    layer; baseline/70-event-forwarding.yaml carries this to the central bus in
    Audit, and the router decides what happens to it using the same five
    routing classes from design/06a that everything else uses.

    The dossier shape is the response engine's (runbooks/src/engine.py),
    because the router's from_dossier() already reads it and a new shape would
    have needed a new normaliser — a second thing to keep in step with the
    first.
    """
    outcome = record_body["outcome"]
    dossier = {
        "record_type": record_body["record_type"],
        "recorded_at": record_body["recorded_at"],
        # The router resolves a PagerDuty incident when it sees OK, so a
        # successful run closes the incident a previous failing run opened.
        "current_state": "OK" if outcome == "success" else "ALARM",
        "routing": routing,
        "alarm": {
            "name": f"platform-restore-{record_body['mode']}",
            "resource_id": resource_id,
            "environment_tier": record_body["environment_tier"],
        },
        "assessment": {"probable_cause": record_body["reason"]},
        "restore_test": record_body,
    }
    events.put_events(Entries=[{
        "Source": "platform.recovery",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "Resources": [resource_id] if resource_id.startswith("arn:") else [],
    }])


# ---------------------------------------------------------------------------


STEPS = {"resolve": resolve, "start": start, "poll": poll, "claim": claim,
         "validate": validate, "record": record}


def handler(event, context):
    step = event.get("step")
    fn = STEPS.get(step)
    if fn is None:
        # The else. An unrecognised step means the state machine has been edited
        # without the worker, and returning quietly would let the workflow carry
        # on and report a result it never produced.
        raise StepError(
            f"unknown step '{step}'. The state machine and this function have "
            f"diverged. Known steps: {', '.join(sorted(STEPS))}")
    return fn(event)
