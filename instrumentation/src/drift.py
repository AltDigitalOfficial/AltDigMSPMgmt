"""Drift detection for platform-managed alarms.

Prompt 4.2: "If a platform-managed alarm is deleted or modified, recreate it
and log the event. Run on a schedule and on CloudWatch alarm deletion events.
Without this, monitoring coverage erodes quietly and we discover it during an
incident."

------------------------------------------------------------------------------
Deleted OR MODIFIED
------------------------------------------------------------------------------
Deletion is the easy half. Modification is the one that matters, because a
threshold quietly raised from 80 to 99 leaves an alarm that exists, evaluates,
shows green in every console, and will never fire. Counting alarms would report
full coverage. So this compares every field that decides whether an alarm
fires, not merely whether one is present.

------------------------------------------------------------------------------
Why it re-derives rather than remembers
------------------------------------------------------------------------------
There is no stored copy of what each alarm should be. The expected state is
computed fresh from design/06a-alarm-specification.yaml by handler.plan_alarms
— the SAME function the instrumentation Lambda uses.

A separate record of expected state would be a second source of truth, and the
failure mode is specific and bad: drift would "correct" alarms toward whichever
copy was edited last, silently, on a schedule, across every account. Re-deriving
means the specification is the only thing that can be wrong.

------------------------------------------------------------------------------
It never deletes
------------------------------------------------------------------------------
CLAUDE.md: "Never write code that terminates, deletes, deregisters, rolls back
or purges a resource as part of automated remediation."

So an alarm belonging to a resource that no longer exists is REPORTED as an
orphan and left alone. That is the conservative direction: a stale alarm is
clutter, and a deletion loop that misreads its input removes monitoring. The
execution role has no cloudwatch:DeleteAlarms, so this is enforced rather than
merely intended.

------------------------------------------------------------------------------
Enumeration is via AWS Config
------------------------------------------------------------------------------
Fifteen resource types would otherwise mean fifteen list APIs with fifteen
paginators and fifteen shapes. Config's ListDiscoveredResources takes a type
and returns identifiers uniformly, and Config is already a hard dependency —
it is what triggers the instrumentation Lambda in the first place.

The cost is the same one the instrumentation Lambda accepts: Config lags
reality by minutes. For a sweep that runs hourly this is irrelevant.
"""

import datetime
import json
import os

import boto3

import handler as instr

ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "platform-auto")
DRIFT_LOG_GROUP = os.environ.get("DRIFT_LOG_GROUP", "/aws/platform/alarm-drift")

cloudwatch = boto3.client("cloudwatch")
config = boto3.client("config")
logs = boto3.client("logs")

# Fields that decide whether an alarm fires, and therefore the fields whose
# modification is drift.
#
# Tags are NOT compared. They are metadata; changing one does not change when
# the alarm fires, and describe_alarms does not return them — checking would
# cost a ListTagsForResource call per alarm to detect something harmless.
#
# AlarmDescription is also excluded. It carries prose that changes whenever the
# specification's `note` is reworded, and treating a comment edit as drift
# would produce a sweep that "corrects" hundreds of alarms after a docs change.
COMPARED = (
    "Threshold",
    "ComparisonOperator",
    "Period",
    "EvaluationPeriods",
    "DatapointsToAlarm",
    "Statistic",
    "TreatMissingData",
    "Namespace",
    "MetricName",
    "Unit",
    "AlarmActions",
    "OKActions",
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def record(event_type, detail):
    """Write a drift record.

    A separate log group from the instrumentation exceptions. They answer
    different questions — "a resource came up unmonitored" versus "monitoring
    that existed was removed or changed" — and the second is a security-relevant
    event about someone's action, not an operational gap.
    """
    rec = {
        "record_type": "alarm-drift",
        "recorded_at": _now().isoformat(),
        "event": event_type,
        "detail": detail,
    }
    stream = _now().strftime("%Y/%m/%d")
    try:
        try:
            logs.create_log_stream(logGroupName=DRIFT_LOG_GROUP, logStreamName=stream)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_log_events(
            logGroupName=DRIFT_LOG_GROUP,
            logStreamName=stream,
            logEvents=[{"timestamp": int(_now().timestamp() * 1000),
                        "message": json.dumps(rec)}],
        )
    except Exception:
        print(json.dumps({"FATAL_could_not_record_drift": rec}))
        raise
    print(json.dumps(rec))


def live_alarms():
    """Every platform-auto alarm in the account, by name."""
    out = {}
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=ALARM_PREFIX,
                                   AlarmTypes=["MetricAlarm"]):
        for a in page.get("MetricAlarms", []):
            out[a["AlarmName"]] = a
    return out


def discovered(resource_type):
    """Resource ids of a type, via Config."""
    ids = []
    paginator = config.get_paginator("list_discovered_resources")
    for page in paginator.paginate(resourceType=resource_type,
                                   includeDeletedResources=False):
        for r in page.get("resourceIdentifiers", []):
            ids.append(r["resourceId"])
    return ids


def differences(expected, actual):
    """Fields where a live alarm disagrees with what the spec says.

    Returns a list of (field, expected, actual).
    """
    diffs = []
    for field in COMPARED:
        want = expected.get(field)
        have = actual.get(field)
        if field in ("AlarmActions", "OKActions"):
            # Order is not meaningful and absence and empty are the same thing.
            want, have = sorted(want or []), sorted(have or [])
        if want is None and have is None:
            continue
        # CloudWatch returns Threshold as a float even when an integer was
        # supplied, so 80 and 80.0 must not read as drift.
        if isinstance(want, (int, float)) and isinstance(have, (int, float)):
            if float(want) == float(have):
                continue
        elif want == have:
            continue
        diffs.append({"field": field, "expected": want, "actual": have})
    return diffs


def handler(event, context):
    trigger = "schedule"
    if (event or {}).get("detail-type") == "AWS API Call via CloudTrail":
        trigger = "alarm-deleted"

    # Both triggers run the SAME full sweep.
    #
    # The alternative for the delete event was parsing the deleted alarm names
    # back into resource ids — but the name is
    # `platform-auto-<resource-id>-<alarm-id>` and both halves may contain
    # dashes, so parsing is ambiguous and would fail on exactly the resources
    # with awkward names. A full sweep is unambiguous, costs a handful of API
    # calls at this scale, and removes a whole class of parsing bug.
    live = live_alarms()
    expected_names = set()
    recreated, corrected, intact, failures = [], [], [], []

    for resource_type in sorted(instr.IN_SCOPE):
        try:
            ids = discovered(resource_type)
        except Exception as exc:
            failures.append({"resource_type": resource_type, "error": str(exc)})
            continue

        for resource_id in ids:
            planned, _classes, terminal = instr.plan_alarms(resource_type, resource_id)
            if terminal is not None or not planned:
                continue

            for aid, params in planned:
                name = params["AlarmName"]
                expected_names.add(name)
                actual = live.get(name)

                if actual is None:
                    try:
                        cloudwatch.put_metric_alarm(**params)
                        recreated.append({"alarm": name, "resource": resource_id})
                        record("recreated-deleted-alarm", {
                            "alarm": name, "alarm_id": aid,
                            "resource_type": resource_type, "resource_id": resource_id,
                            "message": "A platform-managed alarm was missing and has "
                                       "been recreated from the specification."})
                    except Exception as exc:
                        failures.append({"alarm": name, "error": str(exc)})
                    continue

                diffs = differences(params, actual)
                if not diffs:
                    intact.append(name)
                    continue

                try:
                    cloudwatch.put_metric_alarm(**params)
                    corrected.append({"alarm": name, "fields": [d["field"] for d in diffs]})
                    record("corrected-modified-alarm", {
                        "alarm": name, "alarm_id": aid,
                        "resource_type": resource_type, "resource_id": resource_id,
                        "differences": diffs,
                        "message": "A platform-managed alarm had been modified away "
                                   "from the specification and has been put back. The "
                                   "previous values are in `differences` — a threshold "
                                   "raised far enough leaves an alarm that exists, "
                                   "evaluates and can never fire."})
                except Exception as exc:
                    failures.append({"alarm": name, "error": str(exc)})

    # Alarms carrying the platform prefix that nothing expects. Usually the
    # resource is gone; occasionally the specification changed. Reported, never
    # deleted.
    orphans = sorted(set(live) - expected_names)
    if orphans:
        record("orphaned-alarms", {
            "alarms": orphans,
            "message": "Alarms with the platform prefix that no current resource and "
                       "no current specification entry accounts for. NOT deleted — "
                       "automated remediation in this platform never removes anything, "
                       "and the execution role has no DeleteAlarms. Usually the "
                       "resource was decommissioned; check before clearing by hand."})

    if failures:
        record("drift-sweep-failures", {"failures": failures})

    result = {
        "trigger": trigger,
        "recreated": recreated,
        "corrected": corrected,
        "intact": len(intact),
        "orphaned": orphans,
        "failures": failures,
    }
    print(json.dumps(result))
    return result
