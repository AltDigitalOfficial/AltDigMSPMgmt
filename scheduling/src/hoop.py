"""Hours of Operation. Prompt 13.2, design/14-environment-tiers.md "HOOPs".

Why this is allowed to stop things at all
------------------------------------------
CLAUDE.md: "Never write code that terminates, deletes, deregisters, rolls back
or purges a resource as part of automated remediation. Operational automation
is additive or reversible."

Every action here is reversible and none of it is remediation. An EC2 instance
is stopped, not terminated. An ASG's desired count is set to zero, not the
group deleted. The EBS volume, the load balancer, the NAT gateway and the
Elastic IP all persist and all keep billing -- design 14 is explicit that a
HOOP reduces compute spend, not total spend, "and the reporting should say so
plainly rather than implying a proportional saving".

Reversible is a claim that has to be true, not a category the code is filed
under. It is true here only because turndown RECORDS what it changed before
changing it. An ASG whose desired count was 4 and is now 0 carries no memory of
the 4; turnup would have to guess, and a guess that lands on 1 is an outage
that looks like a successful turnup. So prior state is written to tags on the
resource itself, before the change, and turnup restores from them or refuses.

Production is refused structurally, and so is "I do not know"
---------------------------------------------------------------
prompt 13.2: "Never production -- enforce that, do not rely on configuration."

`assert_schedulable` refuses `prod`. It also refuses a tier it cannot
determine, and that second refusal is the one that matters. An account whose
tier tag is missing, misspelled or unreadable is not a non-production account;
it is an account nobody can vouch for, and the failure mode of guessing is
stopping production. The safe default is to do nothing, loudly.

Enforcement is layered rather than singular, because a single check is a single
edit away from being removed:

  1. this function refuses prod and unknown
  2. the execution role is granted no ec2:StopInstances, rds:Stop* or
     autoscaling:UpdateAutoScalingGroup in a production account, because the
     stack is not deployed there
  3. the resolved tier comes from the account's own registry record, not from
     the invocation payload -- a caller cannot assert its way into a turndown

The seven-day problem is a reporting problem, not just a re-stop
------------------------------------------------------------------
AWS restarts a stopped RDS instance or Aurora cluster after 7 days. design 14:
"the scheduler must re-stop instances that AWS has auto-started, and must
record each occurrence -- otherwise the cost report will not match expectation
and nobody will know why."

Re-stopping is the easy half and, on its own, actively harmful: it makes the
symptom disappear while the cost stays. An environment that silently
auto-restarts twice a month is an environment that should be TORN DOWN rather
than scheduled, and nobody can reach that conclusion from a graph that shows
it obediently stopped every night. So every auto-restart is recorded as an
event, counted per month, and surfaced with the recommendation design 14 asks
for.

Time zone comes from the customer, and is never assumed
---------------------------------------------------------
prompt 13.2: "Time zone comes from questionnaire 3a.7. Do not assume UTC."

If the zone cannot be resolved, this REFUSES rather than falling back. A
fallback to UTC does not fail visibly -- it turns a US-Central environment down
at noon, which reads as an outage rather than as a configuration error, and the
person debugging it has no reason to suspect the scheduler.
"""
import datetime
import json
import os
import zoneinfo

import boto3

ec2 = boto3.client("ec2")
rds = boto3.client("rds")
ecs = boto3.client("ecs")
autoscaling = boto3.client("autoscaling")
events_client = boto3.client("events")
dynamodb = boto3.client("dynamodb")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")

# Tags carrying the state turndown must restore. On the resource rather than in
# a table on purpose: they travel with the thing, survive this function being
# redeployed, and are visible to somebody looking at the resource wondering why
# its desired count is zero.
TAG_MANAGED = "platform:hoop:managed"
TAG_PRIOR_DESIRED = "platform:hoop:prior-desired"
TAG_PRIOR_MIN = "platform:hoop:prior-min"
TAG_STOPPED_AT = "platform:hoop:stopped-at"

# design 14: available for all three non-production tiers, never production.
SCHEDULABLE_TIERS = ("dev", "test", "uat")


class NotSchedulable(Exception):
    """This environment may not be scheduled. Refusal, not failure."""


class ZoneUnknown(Exception):
    """The HOOP's time zone could not be resolved, so no window can be computed."""


class PriorStateMissing(Exception):
    """Turnup cannot restore what turndown did not record."""


def _now(tz=None):
    return datetime.datetime.now(tz or datetime.timezone.utc)


def _iso(when):
    return when.astimezone(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def account_tier(account_id):
    """The tier, from the registry, not from the caller.

    Read here rather than taken from the invocation payload so that a caller
    cannot assert its way into stopping production by passing tier='dev'.
    """
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "META"}})
    item = got.get("Item")
    if not item:
        return None
    return (item.get("environment") or {}).get("S")


def assert_schedulable(account_id):
    """Refuse production, and refuse not knowing. Returns the tier."""
    tier = account_tier(account_id)

    if tier is None:
        raise NotSchedulable(
            f"account {account_id} has no registry record, or its record "
            "carries no environment. An account whose tier cannot be "
            "determined is not a non-production account -- it is one nobody "
            "can vouch for, and the cost of guessing wrong is stopping "
            "production. Register it before scheduling it.")

    if tier not in SCHEDULABLE_TIERS:
        raise NotSchedulable(
            f"account {account_id} is '{tier}'. design 14 makes HOOPs "
            f"available for {', '.join(SCHEDULABLE_TIERS)} and never for "
            "production. This refusal does not consult any HOOP "
            "configuration, because configuration is what a mistake looks "
            "like.")

    return tier


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def zone(hoop):
    """The HOOP's time zone, or a refusal.

    questionnaire 3a.7. Never defaulted: see the module docstring on why a
    silent fall back to UTC is worse than stopping.
    """
    name = (hoop or {}).get("timezone")
    if not name:
        raise ZoneUnknown(
            "the HOOP declares no timezone. questionnaire 3a.7 collects it, "
            "and it is not defaulted here -- assuming UTC would turn a "
            "US-Central environment down at noon, which reads as an outage "
            "rather than as a misconfiguration.")
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception as exc:                          # noqa: BLE001
        raise ZoneUnknown(
            f"timezone '{name}' could not be resolved ({type(exc).__name__}). "
            "On Lambda this usually means the tzdata package is absent from "
            "the runtime image. Refusing rather than falling back.") from exc


def within_window(hoop, when=None):
    """Is the environment supposed to be UP right now?

    A HOOP is a list of windows, each naming days and a local start and end.
    Absence of any window means the environment is never up on a schedule,
    which is a legitimate configuration and not an error -- it is what a
    manually-operated environment looks like.
    """
    tz = zone(hoop)
    local = (when or _now()).astimezone(tz)
    day = local.strftime("%a").lower()

    for window in hoop.get("windows") or []:
        days = [d.lower()[:3] for d in window.get("days") or []]
        if day not in days:
            continue
        start = datetime.time.fromisoformat(window["start"])
        end = datetime.time.fromisoformat(window["end"])
        if start <= local.time() < end:
            return True, {"matched": window, "local_time": local.isoformat()}
    return False, {"matched": None, "local_time": local.isoformat()}


# ---------------------------------------------------------------------------
# Capture before change
# ---------------------------------------------------------------------------


def _tag_ec2(resource_id, pairs):
    ec2.create_tags(Resources=[resource_id],
                    Tags=[{"Key": k, "Value": str(v)} for k, v in pairs.items()])


def _asg_tags(group):
    return {t["Key"]: t["Value"] for t in group.get("Tags") or []}


def turndown_asgs(dry_run=False):
    """ASG min and desired to zero, recording what they were.

    The recording is the part that makes this reversible. design 14 stops the
    group; it does not say how it comes back, and a turnup that guessed would
    restore an ASG at desired=1 and call it success.
    """
    done, skipped = [], []
    paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
    for page in paginator.paginate():
        for group in page.get("AutoScalingGroups") or []:
            name = group["AutoScalingGroupName"]
            tags = _asg_tags(group)
            if group["DesiredCapacity"] == 0 and group["MinSize"] == 0:
                skipped.append({"asg": name, "why": "already at zero"})
                continue
            prior = {"desired": group["DesiredCapacity"],
                     "min": group["MinSize"]}
            if dry_run:
                done.append({"asg": name, "prior": prior, "applied": False})
                continue
            # Tag FIRST. If the update succeeds and the tagging does not, the
            # group is at zero with no record of what it was -- unrecoverable
            # without a human guessing.
            autoscaling.create_or_update_tags(Tags=[
                {"ResourceId": name, "ResourceType": "auto-scaling-group",
                 "Key": TAG_PRIOR_DESIRED, "Value": str(prior["desired"]),
                 "PropagateAtLaunch": False},
                {"ResourceId": name, "ResourceType": "auto-scaling-group",
                 "Key": TAG_PRIOR_MIN, "Value": str(prior["min"]),
                 "PropagateAtLaunch": False},
                {"ResourceId": name, "ResourceType": "auto-scaling-group",
                 "Key": TAG_MANAGED, "Value": "true",
                 "PropagateAtLaunch": False},
            ])
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=name, MinSize=0, DesiredCapacity=0)
            done.append({"asg": name, "prior": prior, "applied": True})
    return done, skipped


def turnup_asgs(dry_run=False):
    """Restore each ASG to what turndown recorded, or refuse."""
    done, problems = [], []
    paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
    for page in paginator.paginate():
        for group in page.get("AutoScalingGroups") or []:
            tags = _asg_tags(group)
            if tags.get(TAG_MANAGED) != "true":
                continue
            if TAG_PRIOR_DESIRED not in tags or TAG_PRIOR_MIN not in tags:
                # Refuses rather than picking a number. An ASG brought up at a
                # capacity nobody chose is an outage that looks like a
                # successful turnup.
                problems.append({
                    "asg": group["AutoScalingGroupName"],
                    "problem": "marked as HOOP-managed with no recorded prior "
                               "capacity. Refusing to guess -- restore it by "
                               "hand and remove the tag."})
                continue
            prior = {"desired": int(tags[TAG_PRIOR_DESIRED]),
                     "min": int(tags[TAG_PRIOR_MIN])}
            if not dry_run:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=group["AutoScalingGroupName"],
                    MinSize=prior["min"], DesiredCapacity=prior["desired"])
            done.append({"asg": group["AutoScalingGroupName"],
                         "restored": prior, "applied": not dry_run})
    return done, problems


def turndown_ec2(dry_run=False):
    """Stop running instances. Never terminate."""
    done = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for reservation in page.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                # An instance in an ASG is handled by the ASG. Stopping it
                # directly would have the group replace it, which costs money
                # and looks like the scheduler not working.
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags") or []}
                if "aws:autoscaling:groupName" in tags:
                    continue
                ident = instance["InstanceId"]
                if not dry_run:
                    _tag_ec2(ident, {TAG_MANAGED: "true",
                                     TAG_STOPPED_AT: _iso(_now())})
                    ec2.stop_instances(InstanceIds=[ident])
                done.append({"instance": ident, "applied": not dry_run})
    return done


def turnup_ec2(dry_run=False):
    """Start only what this scheduler stopped.

    Scoped by tag rather than starting everything stopped. An instance somebody
    stopped deliberately -- to investigate it, or because it should not be
    running at all -- must not be started by a schedule.
    """
    done = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[
            {"Name": "instance-state-name", "Values": ["stopped"]},
            {"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]}]):
        for reservation in page.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                ident = instance["InstanceId"]
                if not dry_run:
                    ec2.start_instances(InstanceIds=[ident])
                done.append({"instance": ident, "applied": not dry_run})
    return done


# ---------------------------------------------------------------------------
# RDS and Aurora
# ---------------------------------------------------------------------------


def turndown_rds(dry_run=False):
    """Stop databases. Clusters as clusters, instances as instances.

    Three things are deliberately skipped, and each would fail loudly at the
    API if they were not:

      a cluster member   stopped by stopping the CLUSTER. Stopping a member
                         directly is rejected, and looping over the rejection
                         is how a scheduler ends up retrying forever.
      a read replica     cannot be stopped at all while it is a replica.
      anything not
      'available'        already stopping, backing up or modifying. Trying
                         again next run is free; forcing it is not possible.
    """
    done, skipped = [], []

    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances") or []:
            ident = db["DBInstanceIdentifier"]
            if db.get("DBClusterIdentifier"):
                skipped.append({"db": ident, "why": "cluster member"})
                continue
            if db.get("ReadReplicaSourceDBInstanceIdentifier"):
                skipped.append({"db": ident, "why": "read replica"})
                continue
            if db.get("DBInstanceStatus") != "available":
                skipped.append({"db": ident,
                                "why": f"status {db.get('DBInstanceStatus')}"})
                continue
            if not dry_run:
                # Tagged before stopping, for the same reason the ASGs are:
                # the tag is how the seven-day sweep later tells a database
                # this scheduler stopped from one somebody else did.
                rds.add_tags_to_resource(
                    ResourceName=db["DBInstanceArn"],
                    Tags=[{"Key": TAG_MANAGED, "Value": "true"},
                          {"Key": TAG_STOPPED_AT, "Value": _iso(_now())}])
                rds.stop_db_instance(DBInstanceIdentifier=ident)
            done.append({"db": ident, "kind": "instance",
                         "applied": not dry_run})

    for page in rds.get_paginator("describe_db_clusters").paginate():
        for cluster in page.get("DBClusters") or []:
            ident = cluster["DBClusterIdentifier"]
            if cluster.get("Status") != "available":
                skipped.append({"db": ident,
                                "why": f"status {cluster.get('Status')}"})
                continue
            if not dry_run:
                rds.add_tags_to_resource(
                    ResourceName=cluster["DBClusterArn"],
                    Tags=[{"Key": TAG_MANAGED, "Value": "true"},
                          {"Key": TAG_STOPPED_AT, "Value": _iso(_now())}])
                rds.stop_db_cluster(DBClusterIdentifier=ident)
            done.append({"db": ident, "kind": "cluster",
                         "applied": not dry_run})

    return done, skipped


def turnup_rds(dry_run=False):
    """Start only databases this scheduler stopped."""
    done = []
    for db in stopped_databases():
        if db["status"] != "stopped":
            continue
        if not dry_run:
            if db["kind"] == "instance":
                rds.start_db_instance(DBInstanceIdentifier=db["id"])
            else:
                rds.start_db_cluster(DBClusterIdentifier=db["id"])
        done.append({"db": db["id"], "kind": db["kind"],
                     "applied": not dry_run})
    return done


# ---------------------------------------------------------------------------
# ECS
# ---------------------------------------------------------------------------


def turndown_ecs(dry_run=False):
    """Desired count to zero, recording what it was.

    Same reversibility argument as the ASGs, and the same failure if the
    recording is skipped: a service brought back at desired=1 when it was 3 is
    an outage that reports success.
    """
    done, skipped = [], []
    for cluster_page in ecs.get_paginator("list_clusters").paginate():
        for cluster in cluster_page.get("clusterArns") or []:
            for service_page in ecs.get_paginator("list_services").paginate(
                    cluster=cluster):
                arns = service_page.get("serviceArns") or []
                if not arns:
                    continue
                described = ecs.describe_services(
                    cluster=cluster, services=arns, include=["TAGS"])
                for service in described.get("services") or []:
                    arn = service["serviceArn"]
                    desired = service.get("desiredCount", 0)
                    if desired == 0:
                        skipped.append({"service": arn,
                                        "why": "already at zero"})
                        continue
                    if not dry_run:
                        ecs.tag_resource(resourceArn=arn, tags=[
                            {"key": TAG_MANAGED, "value": "true"},
                            {"key": TAG_PRIOR_DESIRED, "value": str(desired)},
                        ])
                        ecs.update_service(cluster=cluster, service=arn,
                                           desiredCount=0)
                    done.append({"service": arn, "prior_desired": desired,
                                 "applied": not dry_run})
    return done, skipped


def turnup_ecs(dry_run=False):
    """Restore each service's recorded desired count, or refuse."""
    done, problems = [], []
    for cluster_page in ecs.get_paginator("list_clusters").paginate():
        for cluster in cluster_page.get("clusterArns") or []:
            for service_page in ecs.get_paginator("list_services").paginate(
                    cluster=cluster):
                arns = service_page.get("serviceArns") or []
                if not arns:
                    continue
                described = ecs.describe_services(
                    cluster=cluster, services=arns, include=["TAGS"])
                for service in described.get("services") or []:
                    tags = {t["key"]: t["value"]
                            for t in (service.get("tags") or [])}
                    if tags.get(TAG_MANAGED) != "true":
                        continue
                    if TAG_PRIOR_DESIRED not in tags:
                        problems.append({
                            "service": service["serviceArn"],
                            "problem": "marked as HOOP-managed with no "
                                       "recorded desired count. Refusing to "
                                       "guess -- restore it by hand and "
                                       "remove the tag."})
                        continue
                    prior = int(tags[TAG_PRIOR_DESIRED])
                    if not dry_run:
                        ecs.update_service(cluster=cluster,
                                           service=service["serviceArn"],
                                           desiredCount=prior)
                    done.append({"service": service["serviceArn"],
                                 "restored_desired": prior,
                                 "applied": not dry_run})
    return done, problems


# ---------------------------------------------------------------------------
# The seven-day problem
# ---------------------------------------------------------------------------


def stopped_databases():
    """Every RDS instance and Aurora cluster this scheduler has stopped."""
    found = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances") or []:
            tags = {t["Key"]: t["Value"]
                    for t in (db.get("TagList") or [])}
            if tags.get(TAG_MANAGED) == "true":
                found.append({"kind": "instance",
                              "id": db["DBInstanceIdentifier"],
                              "status": db["DBInstanceStatus"],
                              "stopped_at": tags.get(TAG_STOPPED_AT)})
    for page in rds.get_paginator("describe_db_clusters").paginate():
        for cluster in page.get("DBClusters") or []:
            tags = {t["Key"]: t["Value"]
                    for t in (cluster.get("TagList") or [])}
            if tags.get(TAG_MANAGED) == "true":
                found.append({"kind": "cluster",
                              "id": cluster["DBClusterIdentifier"],
                              "status": cluster["Status"],
                              "stopped_at": tags.get(TAG_STOPPED_AT)})
    return found


def sweep_auto_restarts(account_id, dry_run=False):
    """Find databases AWS restarted on us, re-stop them, and RECORD it.

    The recording is the point. Re-stopping alone makes the symptom disappear
    while the cost stays, and an environment auto-restarting twice a month is
    one that should be torn down rather than scheduled -- a conclusion nobody
    can reach from a graph showing it obediently stopped every night.
    """
    restarted, restopped, problems = [], [], []

    for db in stopped_databases():
        # 'available' on something this scheduler stopped means AWS started
        # it. Nothing else has the tag.
        if db["status"] != "available":
            continue

        stopped_at = db.get("stopped_at")
        age_days = None
        if stopped_at:
            try:
                then = datetime.datetime.fromisoformat(
                    stopped_at.replace("Z", "+00:00"))
                age_days = round((_now() - then).total_seconds() / 86400.0, 1)
            except ValueError:
                pass

        restarted.append({**db, "days_since_stopped": age_days})

        if dry_run:
            continue
        try:
            if db["kind"] == "instance":
                rds.stop_db_instance(DBInstanceIdentifier=db["id"])
            else:
                rds.stop_db_cluster(DBClusterIdentifier=db["id"])
            restopped.append(db["id"])
        except Exception as exc:                      # noqa: BLE001
            # A database mid-backup or mid-modification cannot be stopped.
            # Reported rather than retried into a loop.
            problems.append({"id": db["id"],
                             "problem": f"{type(exc).__name__}: {str(exc)[:160]}"})

    if restarted and not dry_run:
        _emit(account_id, "platform-hoop-auto-restart",
              f"AWS auto-restarted {len(restarted)} stopped database(s) in "
              f"{account_id} after the seven-day limit. They have been "
              "re-stopped. Each occurrence bills a full day at minimum.",
              {"restarted": restarted, "restopped": restopped,
               "problems": problems,
               # design 14 asks for this to reach the cost report with a
               # recommendation attached.
               "recommendation":
                   "An environment that auto-restarts repeatedly is idle for "
                   "longer than scheduling can handle. design 14: long idle "
                   "periods require teardown, not scheduling. Consider a "
                   "seasonal teardown and re-vest instead."})

    return {"restarted": restarted, "restopped": restopped,
            "problems": problems}


def _emit(account_id, name, cause, detail):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.scheduling",
        "DetailType": "hoop",
        "Detail": json.dumps({
            "record_type": "hoop-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            # Cost, not availability. Nobody is woken for a database that came
            # back up; somebody reads it with the month's spend.
            "routing": "digest_and_jira",
            "alarm": {"name": name, "resource_id": account_id},
            "assessment": {"probable_cause": cause},
            "hoop": detail,
        }, default=str),
    }])


# ---------------------------------------------------------------------------
# The manifest: what turndown did, so turnup can be verified against it
# ---------------------------------------------------------------------------
# scheduling/src/cycle.py asserts the FULL expected set after a turnup,
# because "no errors" is satisfied by a turnup that started three of five
# services. Asserting a full set needs the set declared, and this is the
# declaration -- derived from what turndown actually recorded rather than
# maintained beside it, so it cannot drift and a resource added between
# turndown and turnup is correctly absent from the expectation.

def write_manifest(account_id, manifest):
    """Record what turndown stopped, and what it left alone.

    Written to the registry rather than held in the invocation, because turnup
    happens hours later in a different execution and frequently on a different
    day.
    """
    dynamodb.update_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "META"}},
        UpdateExpression=("SET hoop_manifest = :m, hoop_manifest_at = :t, "
                          "updated_at = :t"),
        ExpressionAttributeValues={
            ":m": {"S": json.dumps(manifest, default=str, sort_keys=True)},
            ":t": {"S": _iso(_now())}},
        ConditionExpression="attribute_exists(PK)")


def read_manifest(account_id):
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "META"}})
    item = got.get("Item") or {}
    raw = (item.get("hoop_manifest") or {}).get("S")
    if not raw:
        return None, None
    return json.loads(raw), (item.get("hoop_manifest_at") or {}).get("S")


def build_manifest(turndown_result):
    """The expected set, derived from what turndown actually did.

    Includes resources turndown SKIPPED because they were already stopped:
    they are still expected to be running after a turnup if they were running
    before... which they were not. So they are recorded separately and
    explicitly excluded from the turnup expectation, because bringing up
    something that was deliberately stopped is its own kind of wrong.
    """
    return {
        "recorded_at": _iso(_now()),
        "asgs": {a["asg"]: a["prior"] for a in turndown_result.get("asgs") or []},
        "ecs": {s["service"]: s["prior_desired"]
                for s in turndown_result.get("ecs") or []},
        "rds": [d["db"] for d in turndown_result.get("rds") or []],
        "ec2": [i["instance"] for i in turndown_result.get("ec2") or []],
        # Not expected back up. Recorded so the difference between "we did not
        # start it" and "we failed to start it" is answerable later.
        "left_stopped": turndown_result.get("skipped") or {},
    }


# ---------------------------------------------------------------------------
# The two operations, which are one code path
# ---------------------------------------------------------------------------


def run(event, context=None):
    """Turndown or turnup, scheduled or manual, through the same path.

    design 14 (P4): "the scheduled path and the manual path are the same code,
    and an untested turnup is an outage waiting for a" -- the manual trigger is
    not a convenience wrapper, it is the same function with a different caller.
    prompt 13.2 requires the customer's nominated contact to be able to invoke
    it, so a team working an unplanned weekend does not need a ticket.
    """
    account_id = (event or {}).get("account_id")
    action = (event or {}).get("action")
    dry_run = bool((event or {}).get("dry_run"))
    hoop = (event or {}).get("hoop") or {}

    if not account_id:
        raise ValueError("hoop needs account_id")

    tier = assert_schedulable(account_id)

    # A scheduled invocation says nothing about direction; it asks what the
    # window implies. A manual one states it outright.
    reason = "manual"
    if action not in ("turndown", "turnup"):
        up, detail = within_window(hoop)
        action = "turnup" if up else "turndown"
        reason = f"schedule ({detail['local_time']})"

    started = _now()
    out = {"record_type": "hoop-run", "account_id": account_id, "tier": tier,
           "action": action, "reason": reason, "dry_run": dry_run,
           "ran_at": _iso(started)}

    if action == "turndown":
        # ASGs before their instances: zeroing the group first stops it
        # replacing instances that are about to be stopped underneath it.
        asgs, asg_skipped = turndown_asgs(dry_run)
        ecs_done, ecs_skipped = turndown_ecs(dry_run)
        rds_done, rds_skipped = turndown_rds(dry_run)
        out["asgs"] = asgs
        out["ecs"] = ecs_done
        out["rds"] = rds_done
        out["ec2"] = turndown_ec2(dry_run)
        out["skipped"] = {"asgs": asg_skipped, "ecs": ecs_skipped,
                          "rds": rds_skipped}
    else:
        # Databases first on the way up: an application that starts before its
        # database spends its first minutes crash-looping, which looks like a
        # failed deployment to whoever is waiting for the environment.
        rds_done = turnup_rds(dry_run)
        asgs, asg_problems = turnup_asgs(dry_run)
        ecs_done, ecs_problems = turnup_ecs(dry_run)
        out["rds"] = rds_done
        out["asgs"] = asgs
        out["ecs"] = ecs_done
        out["ec2"] = turnup_ec2(dry_run)
        problems = asg_problems + ecs_problems
        out["problems"] = problems
        if problems:
            _emit(account_id, "platform-hoop-turnup-incomplete",
                  f"{len(problems)} resource(s) could not be restored because "
                  "no prior capacity was recorded. The environment is "
                  "partially up, which is worse than being down: it will "
                  "serve some requests.",
                  {"problems": problems})

    # Always, in both directions. An auto-restart is discovered by looking, and
    # the only reliable time to look is whenever this runs.
    out["auto_restart"] = sweep_auto_restarts(account_id, dry_run)

    # Elapsed time is carried out so the verification step can trend it.
    # design 14 asks for turnup duration to be trended so that creep is
    # visible before it is a complaint -- a turnup that has gone from four
    # minutes to nineteen is not an incident on any single morning.
    out["elapsed_seconds"] = round((_now() - started).total_seconds(), 1)

    # The manifest is written on TURNDOWN only, and only when something was
    # actually done. It is what the turnup verification asserts against: see
    # scheduling/src/cycle.py on why "no errors" is not a sufficient check.
    #
    # Written after the work rather than before, because a manifest describing
    # a turndown that did not happen would have the verification looking for
    # resources that were never stopped.
    if action == "turndown" and not dry_run:
        manifest = build_manifest(out)
        # What must SURVIVE, recorded alongside what was stopped. Without this
        # the completeness check has no baseline to compare against, and a
        # volume that disappeared during the HOOP is undetectable.
        try:
            manifest["persisted"] = {
                "volumes": len(ec2.describe_volumes().get("Volumes") or [])}
        except Exception as exc:                      # noqa: BLE001
            # Recorded as unknown rather than omitted. An absent key reads as
            # "nothing to check"; an explicit null reads as "could not look".
            manifest["persisted"] = {"volumes": None,
                                     "why": f"{type(exc).__name__}"}
        write_manifest(account_id, manifest)
        out["manifest_written"] = True

    print(json.dumps(out, default=str))
    return out


HANDLERS = {"run": run, "sweep": lambda e, c=None: {
    "record_type": "hoop-sweep",
    "auto_restart": sweep_auto_restarts(e.get("account_id"),
                                        bool(e.get("dry_run")))}}


def handler(event, context=None):
    action = (event or {}).get("handler") or "run"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(f"unknown handler '{action}'")
    return fn(event or {}, context)
