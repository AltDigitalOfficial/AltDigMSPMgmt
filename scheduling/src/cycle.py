"""Turndown and turnup, verified. Prompt 13.3, design 14 "must be tested".

Design principle P4 applied to scheduling: the scheduled path and the manual
path are the same code, and an untested turnup is an outage waiting for a
Monday morning.

Turnup is the higher risk, and the asymmetry is the whole design
------------------------------------------------------------------
design 14: "A failed turndown costs money. A failed turnup costs a QA team or a
customer's user group their working day, and in UAT that is a customer-facing
failure."

So the two validations are not mirror images. Turndown completeness is checked
and reported. Turnup is checked, and a failure is an INCIDENT -- paging in
UAT, and in dev and test raising a ticket that notifies the dev contact
immediately rather than waiting for the next digest. A developer who discovers
at 09:00 that their environment did not come up has already lost the morning.

Partial turnup is the state this module exists for
----------------------------------------------------
design 14: "Partial turnup -- some resources up, some not -- is the dangerous
state, because it looks available and is not. The validation step must assert
the full expected set, not just absence of errors."

"Absence of errors" is the trap, and it is the same one as B-086 and the
cadence sweep: a turnup that started three of five services and hit no
exception reports success. Every API call returned 200. The environment serves
some requests, fails others, and the failure looks like an application bug to
whoever is using it.

Asserting a full set requires knowing what the set IS
-------------------------------------------------------
Which is the problem the cadence sweep also had, and it has the same answer:
the expectation must be DECLARED, and the declaration must already exist
rather than being maintained beside the thing it describes.

Here it does. Turndown knows exactly what it stopped, because it had to record
prior capacity in order to be reversible at all. That record -- the manifest --
is the expected set. Turnup is complete when every resource in the manifest is
back at the capacity the manifest names.

Nothing new is declared, nothing can drift, and a resource added to the
environment between turndown and turnup is correctly absent from the
expectation rather than reported as missing.
"""
import datetime
import json
import os

import boto3

import hoop

cloudwatch = boto3.client("cloudwatch")
events_client = boto3.client("events")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
METRIC_NAMESPACE = os.environ.get("HOOP_METRIC_NAMESPACE", "Platform/HOOP")

# design 14: turnup failure pages in UAT and raises an immediate ticket in dev
# and test. Not a digest item in any tier -- the distinction is between waking
# somebody and telling them now, never between now and tomorrow.
FAILURE_ROUTING = {
    "uat": "pagerduty",
    "test": "jira_immediate",
    "dev": "jira_immediate",
}


class ManifestMissing(Exception):
    """Turnup cannot be validated against a turndown that was not recorded."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(when):
    return when.astimezone(datetime.timezone.utc).isoformat()


# The manifest lives in hoop.py, next to the turndown that produces it.
# Re-exported here so a reader of this module can see where it comes from.
build_manifest = hoop.build_manifest
read_manifest = hoop.read_manifest
write_manifest = hoop.write_manifest


# ---------------------------------------------------------------------------
# Test 1 — turndown completeness
# ---------------------------------------------------------------------------


def validate_turndown(account_id, manifest):
    """Everything that should stop stopped; nothing that should persist gone.

    The second half is the one worth having. A turndown that terminated an
    instance instead of stopping it would look identical in the cost report --
    better, even -- and be discovered when somebody tried to turn it back up.
    """
    problems, checked = [], {"stopped": 0, "persisted": 0}

    for instance_id in manifest.get("ec2") or []:
        described = hoop.ec2.describe_instances(InstanceIds=[instance_id])
        for reservation in described.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                state = (instance.get("State") or {}).get("Name")
                checked["stopped"] += 1
                if state in ("terminated", "shutting-down"):
                    problems.append({
                        "resource": instance_id, "state": state,
                        "problem": "TERMINATED, not stopped. A HOOP stops "
                                   "compute; it never destroys it. This is "
                                   "unrecoverable and the environment cannot "
                                   "be turned back up."})
                elif state not in ("stopped", "stopping"):
                    problems.append({
                        "resource": instance_id, "state": state,
                        "problem": "did not stop"})

    # Storage and network must still be there. design 14: "Storage and network
    # baseline continue to cost money. HOOPs reduce compute spend, not total
    # spend." A volume that vanished is data loss wearing a cost saving.
    volumes = hoop.ec2.describe_volumes().get("Volumes") or []
    checked["persisted"] = len(volumes)
    expected_volumes = (manifest.get("persisted") or {}).get("volumes")
    if expected_volumes is not None and len(volumes) < expected_volumes:
        problems.append({
            "resource": "ebs",
            "problem": f"{expected_volumes - len(volumes)} volume(s) present "
                       "at turndown are gone. Storage must persist through a "
                       "HOOP; this is data loss, not a saving."})

    return {"test": "turndown-completeness",
            "passed": not problems,
            "checked": checked,
            "problems": problems}


# ---------------------------------------------------------------------------
# Test 2 — turnup success, asserted against the full set
# ---------------------------------------------------------------------------


def validate_turnup(account_id, manifest):
    """Every resource in the manifest, back at the capacity it named.

    Reports what it examined alongside what it concluded. "0 problems" over an
    empty manifest and "0 problems" over a manifest of forty are the same
    sentence and opposite facts -- so `expected` is always in the output, and
    a caller that only reads `passed` gets the wrong answer loudly rather than
    quietly.
    """
    problems = []
    expected = {"asgs": len(manifest.get("asgs") or {}),
                "ecs": len(manifest.get("ecs") or {}),
                "rds": len(manifest.get("rds") or []),
                "ec2": len(manifest.get("ec2") or [])}
    found = {"asgs": 0, "ecs": 0, "rds": 0, "ec2": 0}

    for name, prior in (manifest.get("asgs") or {}).items():
        described = hoop.autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[name])
        groups = described.get("AutoScalingGroups") or []
        if not groups:
            problems.append({"resource": name,
                             "problem": "auto scaling group is gone"})
            continue
        group = groups[0]
        found["asgs"] += 1
        if group.get("DesiredCapacity") != prior.get("desired"):
            problems.append({
                "resource": name,
                "problem": f"desired capacity is "
                           f"{group.get('DesiredCapacity')}, manifest says "
                           f"{prior.get('desired')}"})
        healthy = sum(1 for i in group.get("Instances") or []
                      if i.get("HealthStatus") == "Healthy"
                      and i.get("LifecycleState") == "InService")
        if healthy < prior.get("desired", 0):
            # Running is not the same as in service. An instance that launched
            # and failed its health check is the partial-turnup shape.
            problems.append({
                "resource": name,
                "problem": f"{healthy} of {prior.get('desired')} instances in "
                           "service. The group is up and not serving."})

    for arn, prior in (manifest.get("ecs") or {}).items():
        described = hoop.ecs.describe_services(
            cluster=arn.split("/")[1] if "/" in arn else arn, services=[arn])
        services = described.get("services") or []
        if not services:
            problems.append({"resource": arn, "problem": "service is gone"})
            continue
        service = services[0]
        found["ecs"] += 1
        if service.get("runningCount") != prior:
            problems.append({
                "resource": arn,
                "problem": f"{service.get('runningCount')} task(s) running, "
                           f"manifest says {prior}"})

    for db_id in manifest.get("rds") or []:
        for db in hoop.stopped_databases():
            if db["id"] != db_id:
                continue
            found["rds"] += 1
            if db["status"] != "available":
                problems.append({
                    "resource": db_id,
                    "problem": f"database is '{db['status']}', not available. "
                               "Applications will start and fail."})

    for instance_id in manifest.get("ec2") or []:
        described = hoop.ec2.describe_instances(InstanceIds=[instance_id])
        for reservation in described.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                found["ec2"] += 1
                state = (instance.get("State") or {}).get("Name")
                if state != "running":
                    problems.append({"resource": instance_id,
                                     "problem": f"state is '{state}'"})

    missing = {k: expected[k] - found[k] for k in expected
               if found[k] < expected[k]}
    for kind, count in missing.items():
        problems.append({
            "resource": kind,
            "problem": f"{count} {kind} resource(s) in the manifest could not "
                       "be found at all. Absent is worse than wrong: the "
                       "environment is missing something it had."})

    return {"test": "turnup-success",
            "passed": not problems,
            # Both numbers, always. See the docstring.
            "expected": expected,
            "found": found,
            "problems": problems,
            "partial": bool(problems) and bool(found != {k: 0 for k in found})}


# ---------------------------------------------------------------------------
# Test 3 — duration, trended
# ---------------------------------------------------------------------------


def record_duration(account_id, tier, action, seconds):
    """Publish elapsed time so creep is visible before it is a complaint.

    A metric rather than a log line, because design 14 asks for it TRENDED. A
    turnup that has quietly gone from four minutes to nineteen is not an
    incident on any single morning and is a serious problem by the end of the
    quarter.
    """
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[{
            "MetricName": "CycleSeconds",
            "Dimensions": [{"Name": "Action", "Value": action},
                           {"Name": "Tier", "Value": tier}],
            "Value": float(seconds),
            "Unit": "Seconds",
            "Timestamp": _now(),
        }])


# ---------------------------------------------------------------------------
# Test 4 — functional validation
# ---------------------------------------------------------------------------


def functional_validation(account_id, tier, canary_name=None):
    """The canary passes. Running is not the same as working.

    design 14 requires this after every scheduled turnup, and it is the check
    that distinguishes an environment that responds from one that serves. Every
    other test here asks whether resources are present at the right capacity;
    a canary asks whether the journey works.

    Not implemented: reading a canary result needs a cross-account read this
    function does not hold, which is the same gap B-112 records for the
    verification sweep. Reported as undeterminable rather than passed -- a
    functional check that returns success without running is worse than no
    functional check, because it is quoted in a go-live decision.
    """
    return {"test": "functional-validation",
            "passed": None,
            "why": "canary results are not readable from here (B-112). This "
                   "is UNDETERMINABLE, not passed: an environment can be "
                   "fully up by every count in this module and still not "
                   "serve a request."}


# ---------------------------------------------------------------------------
# Test 6 — data integrity across the cycle
# ---------------------------------------------------------------------------


def data_integrity(account_id, tier):
    """Row counts and checksums consistent before and after.

    Not implemented, and not implementable generically. design 14 asks for
    "row counts and checksums consistent before and after", which requires
    knowing which tables matter in an application the platform did not build
    and cannot introspect safely.

    Reported as undeterminable. The honest implementation is a per-tenant
    query set supplied at onboarding -- questionnaire territory -- and
    inventing one here would check whatever happened to be easy to count.
    B-122.
    """
    return {"test": "data-integrity",
            "passed": None,
            "why": "requires a per-tenant query set naming the tables that "
                   "matter; the platform cannot choose them (B-122)."}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def _emit_failure(account_id, tier, results):
    """design 14: turnup failure is an incident, not a scheduling glitch."""
    routing = FAILURE_ROUTING.get(tier, "jira_immediate")
    failed = [r for r in results if r.get("passed") is False]
    problems = [p for r in failed for p in r.get("problems") or []]

    partial = any(r.get("partial") for r in results)
    cause = (
        "PARTIAL TURNUP. Some resources are up and some are not, so the "
        "environment looks available and is not -- requests will succeed and "
        "fail unpredictably and it will read as an application fault."
        if partial else
        f"Turnup failed in {account_id}. The environment is not usable.")

    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.scheduling",
        "DetailType": "hoop-cycle",
        "Detail": json.dumps({
            "record_type": "hoop-cycle-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            # Never a digest item. design 14 distinguishes waking somebody
            # from telling them now, not now from tomorrow: a developer who
            # finds out at 09:00 has lost the morning either way.
            "routing": routing,
            "alarm": {"name": "platform-hoop-turnup-failed",
                      "resource_id": account_id},
            "assessment": {"probable_cause": cause},
            "hoop_cycle": {"tier": tier, "partial": partial,
                           "failed_tests": [r["test"] for r in failed],
                           "problems": problems,
                           "results": results},
        }, default=str),
    }])
    return routing


def verify(event, context=None):
    """Run the applicable tests for one cycle and route any failure."""
    account_id = (event or {}).get("account_id")
    action = (event or {}).get("action")
    seconds = (event or {}).get("elapsed_seconds")
    if not account_id or action not in ("turndown", "turnup"):
        raise ValueError("verify needs account_id and action")

    tier = hoop.assert_schedulable(account_id)
    manifest, recorded_at = read_manifest(account_id)

    if manifest is None:
        # A turnup with no manifest cannot be verified at all. Distinct from a
        # failed turnup and reported as such: the environment may be perfectly
        # fine, and nothing here can say so.
        raise ManifestMissing(
            f"no HOOP manifest for {account_id}. Turnup cannot be validated "
            "against a turndown that was not recorded, so 'complete' is not "
            "a conclusion available here. If this environment was turned down "
            "outside the scheduler, turn it up the same way.")

    results = []
    if action == "turndown":
        results.append(validate_turndown(account_id, manifest))
    else:
        results.append(validate_turnup(account_id, manifest))
        results.append(functional_validation(account_id, tier))

    if seconds is not None:
        record_duration(account_id, tier, action, seconds)

    routing = None
    if any(r.get("passed") is False for r in results):
        routing = _emit_failure(account_id, tier, results)

    out = {"record_type": "hoop-cycle-verify",
           "account_id": account_id, "tier": tier, "action": action,
           "manifest_recorded_at": recorded_at,
           "results": results,
           "passed": all(r.get("passed") is not False for r in results),
           "undeterminable": [r["test"] for r in results
                              if r.get("passed") is None],
           "routed": routing}
    print(json.dumps(out, default=str))
    return out


def handler(event, context=None):
    return verify(event, context)
