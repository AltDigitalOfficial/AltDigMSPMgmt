"""Gather steps and remediations.

Split from engine.py because they answer different questions. The engine knows
how to stage and revalidate; this file knows how to talk to AWS. Neither knows
which alarm it is working on — that is the catalog's job.

------------------------------------------------------------------------------
Only four remediations exist here, and that is deliberate
------------------------------------------------------------------------------
Doc 07 lists roughly a dozen across stages 2 and 3. Four were permitted on
2026-09-18 (D-012b); the rest were refused with recorded reasons in
runbooks/catalog.yaml.

An action absent from this file cannot be invoked no matter what the catalog
says, which is the right direction for the failure: a catalog entry naming an
unimplemented action raises rather than silently doing nothing. A gap that
throws is findable; a gap that no-ops is not.
"""

import datetime
import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-2")

cloudwatch = boto3.client("cloudwatch")
cloudtrail = boto3.client("cloudtrail")
config = boto3.client("config")
ecs = boto3.client("ecs")
ssm = boto3.client("ssm")
application_autoscaling = boto3.client("application-autoscaling")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ===========================================================================
# Stage 1 — gather. Read-only, always safe, always runs.
# ===========================================================================


def gather(item, ctx):
    fn = _GATHERERS.get(item)
    if fn is None:
        # Not an exception. A catalog naming a gather step nobody implemented
        # should show as a gap in the dossier, where a human reads it, rather
        # than aborting the incident response over a missing observation.
        return {"unimplemented": True,
                "note": f"no gatherer for '{item}'. The dossier is missing this "
                        "observation; everything else still ran."}
    return fn(ctx)


def _recent_changes(ctx):
    """Doc 07: "Was there a deployment or configuration change in the last 30
    minutes? That deployment question resolves a large share of real incidents
    on its own."

    Write events only. A read against the resource in the last half hour is
    almost always the monitoring itself, and including them would bury the one
    event that matters under a list of describe calls.
    """
    since = _now() - datetime.timedelta(minutes=30)
    resp = cloudtrail.lookup_events(
        LookupAttributes=[{"AttributeKey": "ResourceName",
                           "AttributeValue": ctx["resource_id"]}],
        StartTime=since, MaxResults=25)
    changes = []
    for e in resp.get("Events", []):
        name = e.get("EventName", "")
        if name.startswith(("Describe", "List", "Get", "Lookup")):
            continue
        changes.append({
            "event": name,
            "at": e.get("EventTime").isoformat() if e.get("EventTime") else None,
            "by": e.get("Username"),
        })
    return {"window_minutes": 30, "changes": changes,
            "note": "Write events only; reads are almost all the monitoring itself."}


def _metrics_window(ctx):
    """The alarm's own metric either side of the trigger."""
    alarms = cloudwatch.describe_alarms(AlarmNames=[ctx["alarm_name"]])["MetricAlarms"]
    if not alarms:
        return {"error": "alarm not found"}
    a = alarms[0]
    if not a.get("MetricName"):
        # A metric-math alarm. Its components are in Metrics[], and rendering a
        # useful window would mean evaluating the expression — which CloudWatch
        # will do, but through GetMetricData rather than GetMetricStatistics.
        return {"metric_math": True,
                "expression": next((m.get("Expression") for m in a.get("Metrics", [])
                                    if m.get("ReturnData")), None),
                "note": "Metric-math alarm; component metrics not expanded here."}
    end = _now()
    stats = cloudwatch.get_metric_statistics(
        Namespace=a["Namespace"], MetricName=a["MetricName"],
        Dimensions=a.get("Dimensions", []),
        StartTime=end - datetime.timedelta(hours=2), EndTime=end,
        Period=a.get("Period", 300),
        Statistics=[a.get("Statistic", "Average")])
    points = sorted(stats.get("Datapoints", []), key=lambda d: d["Timestamp"])
    return {
        "metric": a["MetricName"], "threshold": a.get("Threshold"),
        "comparison": a.get("ComparisonOperator"),
        "points": [{"at": p["Timestamp"].isoformat(),
                    "value": p.get(a.get("Statistic", "Average"))} for p in points[-24:]],
    }


def _alarm_history(ctx):
    """Has this fired recently, and how did it resolve."""
    resp = cloudwatch.describe_alarm_history(
        AlarmName=ctx["alarm_name"], HistoryItemType="StateUpdate",
        StartDate=_now() - datetime.timedelta(days=7), MaxRecords=20)
    return {"transitions": [
        {"at": h["Timestamp"].isoformat(), "summary": h.get("HistorySummary")}
        for h in resp.get("AlarmHistoryItems", [])]}


def _resource_configuration(ctx):
    try:
        resp = config.get_resource_config_history(
            resourceType=ctx["resource_type"], resourceId=ctx["resource_id"], limit=1)
        items = resp.get("configurationItems") or []
        if not items:
            return {"note": "no Config history for this resource"}
        item = items[0]
        return {"captured_at": item["configurationItemCaptureTime"].isoformat(),
                "status": item.get("configurationItemStatus"),
                "configuration": item.get("configuration")}
    except Exception as exc:
        return {"error": str(exc)}


def _related_health(ctx):
    """Other platform alarms on the same resource — is this one symptom or many."""
    resp = cloudwatch.describe_alarms(AlarmNamePrefix=f"platform-auto-{ctx['resource_id']}")
    return {"alarms": [{"name": a["AlarmName"], "state": a["StateValue"]}
                       for a in resp.get("MetricAlarms", [])]}


def _disk_usage_by_mount(ctx):
    """Which filesystem, not just "a filesystem".

    Needs the CloudWatch agent, which publishes disk_used_percent per path. If
    the agent is absent this returns nothing useful — and that absence is
    itself the finding, because ec2-disk-high cannot have fired without it.
    """
    end = _now()
    resp = cloudwatch.list_metrics(Namespace="CWAgent", MetricName="disk_used_percent",
                                   Dimensions=[{"Name": "InstanceId",
                                                "Value": ctx["resource_id"]}])
    out = []
    for m in resp.get("Metrics", [])[:10]:
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        stats = cloudwatch.get_metric_statistics(
            Namespace="CWAgent", MetricName="disk_used_percent",
            Dimensions=m["Dimensions"],
            StartTime=end - datetime.timedelta(minutes=30), EndTime=end,
            Period=300, Statistics=["Average"])
        pts = sorted(stats.get("Datapoints", []), key=lambda d: d["Timestamp"])
        out.append({"path": dims.get("path"), "device": dims.get("device"),
                    "used_percent": pts[-1]["Average"] if pts else None})
    return {"mounts": out} if out else {
        "note": "No CWAgent disk metrics. The agent is not reporting, which means "
                "this alarm should not have been able to fire — worth checking "
                "before anything else."}


def _ecs_stopped_task_reasons(ctx):
    """Why tasks are dying. For the flapping alarms this IS the answer."""
    cluster, service = _split_ecs(ctx["resource_id"])
    if not cluster:
        return {"error": "could not determine cluster"}
    arns = ecs.list_tasks(cluster=cluster, serviceName=service,
                          desiredStatus="STOPPED", maxResults=10).get("taskArns", [])
    if not arns:
        return {"stopped_tasks": []}
    desc = ecs.describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
    return {"stopped_tasks": [
        {"stopped_at": t["stoppedAt"].isoformat() if t.get("stoppedAt") else None,
         "reason": t.get("stoppedReason"),
         "exit_codes": [c.get("exitCode") for c in t.get("containers", [])]}
        for t in desc]}


def _ecs_service_events(ctx):
    cluster, service = _split_ecs(ctx["resource_id"])
    if not cluster:
        return {"error": "could not determine cluster"}
    svc = ecs.describe_services(cluster=cluster, services=[service])["services"]
    if not svc:
        return {"error": "service not found"}
    return {"events": [{"at": e["createdAt"].isoformat(), "message": e["message"]}
                       for e in svc[0].get("events", [])[:10]]}


def _unimplemented_gather(name):
    def _inner(ctx):
        return {"unimplemented": True,
                "note": f"'{name}' is named in the catalog and not implemented. The "
                        "dossier is missing this observation."}
    return _inner


_GATHERERS = {
    "metrics_window": _metrics_window,
    "recent_changes": _recent_changes,
    "resource_configuration": _resource_configuration,
    "alarm_history": _alarm_history,
    "related_health": _related_health,
    "disk_usage_by_mount": _disk_usage_by_mount,
    "ecs_stopped_task_reasons": _ecs_stopped_task_reasons,
    "ecs_service_events": _ecs_service_events,
    # Named in the catalog, not yet built. Present explicitly so the dossier
    # says "missing" rather than the engine saying "unknown gatherer".
    "top_processes_by_memory": _unimplemented_gather("top_processes_by_memory"),
    "ecs_task_memory_by_task": _unimplemented_gather("ecs_task_memory_by_task"),
    "ecs_task_cpu_by_task": _unimplemented_gather("ecs_task_cpu_by_task"),
    "storage_trend": _unimplemented_gather("storage_trend"),
    "connection_trend": _unimplemented_gather("connection_trend"),
    "recent_invocation_errors": _unimplemented_gather("recent_invocation_errors"),
}


# ===========================================================================
# Stages 2 and 3 — remediation.
# ===========================================================================


def run(action, ctx, params):
    fn = _ACTIONS.get(action)
    if fn is None:
        # Raises, unlike a missing gatherer. A catalog naming an unimplemented
        # REMEDIATION must not look like a remediation that ran and did
        # nothing — the engine would record success and revalidate against an
        # unchanged resource.
        raise NotImplementedError(
            f"action '{action}' is in the catalog but not implemented in actions.py")
    return fn(ctx, params)


def _clear_log_files(ctx, params):
    """Rotated logs only, via SSM Run Command.

    NEVER the live log. Truncating an open file breaks the writer's handle and
    destroys the diagnostic evidence for the incident currently being
    diagnosed — the automation would be deleting the thing the dossier needs.

    Deleting FILES is not deleting a RESOURCE, which is why this survives
    CLAUDE.md's rule.
    """
    paths = params.get("paths") or ["/var/log/*.gz", "/var/log/*.1"]
    min_age = int(params.get("min_age_days", 3))
    vacuum = params.get("journald_vacuum_size", "200M")

    # -mtime guards against deleting something rotated moments ago, which on a
    # busy host may still be the most recent complete log.
    finds = " ; ".join(
        f"find {p.rsplit('/', 1)[0]} -maxdepth 1 -name '{p.rsplit('/', 1)[1]}' "
        f"-mtime +{min_age} -print -delete" for p in paths)
    script = (f"set -e; df -h /var/log; {finds} ; "
              f"journalctl --vacuum-size={vacuum} || true; df -h /var/log")

    resp = ssm.send_command(
        InstanceIds=[ctx["resource_id"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
        Comment=f"platform automation: {ctx['alarm_id']}")
    return {"command_id": resp["Command"]["CommandId"], "paths": paths,
            "min_age_days": min_age}


def _restart_service(ctx, params):
    """Restart a process via SSM. Destroys no AWS resource."""
    tag = params.get("service_name_tag", "platform-managed-service")
    tags = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [ctx["resource_id"]]}])
    if not tags.get("InstanceInformationList"):
        raise RuntimeError(
            f"{ctx['resource_id']} is not managed by SSM, so the service cannot be "
            "restarted. That is a finding in itself: a platform-managed instance "
            "should have the agent.")
    # The service NAME comes from an instance tag rather than the catalog,
    # because it is per-workload. A catalog that named services would be a
    # per-tenant catalog, which is the thing this format exists to avoid.
    ec2 = boto3.client("ec2")
    inst = ec2.describe_instances(InstanceIds=[ctx["resource_id"]])
    names = [t["Value"] for r in inst["Reservations"] for i in r["Instances"]
             for t in i.get("Tags", []) if t["Key"] == tag]
    if not names:
        raise RuntimeError(
            f"instance carries no '{tag}' tag, so there is no service to restart. "
            "Tag the instance or remove this action from the runbook.")
    service = names[0]
    resp = ssm.send_command(
        InstanceIds=[ctx["resource_id"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [f"systemctl restart {service} && systemctl is-active {service}"]},
        Comment=f"platform automation: {ctx['alarm_id']}")
    return {"command_id": resp["Command"]["CommandId"], "service": service}


def _scale_out(ctx, params):
    """Add one task. The scale-in policy was verified by the rails."""
    cluster, service = _split_ecs(ctx["resource_id"])
    increment = int(params.get("increment", 1))
    svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    current = svc["desiredCount"]
    ecs.update_service(cluster=cluster, service=service,
                       desiredCount=current + increment)
    return {"from": current, "to": current + increment}


def _cycle_ecs_task(ctx, params):
    """Stop ONE task. The service's desired count brings a replacement.

    one-at-a-time is doc 07's blast radius rail and it is enforced here as well
    as declared in the catalog, because max_tasks_per_run is a parameter and a
    parameter can be edited. The rail should not be editable from the data.
    """
    cluster, service = _split_ecs(ctx["resource_id"])
    arns = ecs.list_tasks(cluster=cluster, serviceName=service,
                          desiredStatus="RUNNING").get("taskArns", [])
    if len(arns) < 2:
        raise RuntimeError(
            f"{service} has {len(arns)} running task(s). Cycling requires at least "
            "two so the service keeps serving — the redundancy rail should have "
            "caught this, and its disagreeing with reality is worth investigating.")
    target = arns[0]
    ecs.stop_task(cluster=cluster, task=target,
                  reason=f"platform automation: {ctx['alarm_id']}")
    return {"stopped_task": target, "running_before": len(arns), "stopped_count": 1}


_ACTIONS = {
    "clear_log_files": _clear_log_files,
    "restart_service": _restart_service,
    "scale_out": _scale_out,
    "cycle_ecs_task": _cycle_ecs_task,
}


def _split_ecs(resource_id):
    if "/" in resource_id:
        parts = resource_id.split("/")
        return parts[-2], parts[-1]
    return None, resource_id
