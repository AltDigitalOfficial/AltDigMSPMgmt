"""Test-mode teardown. Removes what this execution created, and nothing else.

Why this exists at all, given the hard rule
-------------------------------------------
CLAUDE.md: "Never write code that terminates, deletes, deregisters, rolls back
or purges a resource as part of automated remediation. Security automation
isolates and preserves. Operational automation is additive or reversible."

This function deletes. That is not a departure from the rule, and the
distinction is worth stating precisely rather than waving at:

  * It is not remediation. Nothing here responds to a finding, an alarm or an
    incident. It removes a copy that this same workflow made ninety seconds
    earlier for the sole purpose of being looked at.
  * It only ever touches a resource that carries THIS execution's id in a tag
    that this workflow applied. It has no way to name any other resource, and
    the IAM policy behind it says the same thing a second time with a
    condition key, so a bug in this file still cannot reach an untagged
    resource.
  * It never runs in recovery mode. The state machine's only mode branch is the
    Choice in front of this function, and prompt 6.1 is explicit that recovery
    mode validates, records and STOPS.
  * It does not delete backups. Ever. A recovery point is the thing the whole
    phase exists to protect; the restored copy is disposable, the recovery
    point is not.

So: bookkeeping of the workflow's own artifact, not remediation.

What happens when provenance cannot be established
--------------------------------------------------
It reports an orphan and deletes nothing. That is the important half of this
file. A restored resource whose tag is missing, or whose tag names a different
execution, is either something another run created or something this run failed
to claim — and in both cases a human should look at it. The cost of leaving it
is a bill and a ticket; the cost of deleting it is unbounded.
"""
import datetime
import json
import os

import boto3

ec2 = boto3.client("ec2")
events = boto3.client("events")
rds = boto3.client("rds")

ENVIRONMENT_TIER = os.environ.get("ENVIRONMENT_TIER", "dev")

# Which resource types this function is ARMED to delete.
#
# Defaults to EBS alone, and not because the RDS path is unwritten — it is
# below, and it is the shorter of the two. It is off because the hard rule
# makes an untested delete path unacceptable, and EBS is the only type that can
# be exercised end to end against recovery/00-restore-fixture.yaml today. A
# delete call for a database that has never once run is exactly the code that
# should not be armed by default.
#
# Same idiom as ResponseEnabled in baseline/60-response.yaml: the capability
# exists, is reviewable, and is turned on per account by somebody who has
# looked at what it would do there. A type that is not armed is reported as an
# orphan, which costs a ticket and a disk, and that is the cheap side of this
# trade. B-045.
TEARDOWN_RESOURCE_TYPES = {
    t.strip().upper()
    for t in os.environ.get("TEARDOWN_RESOURCE_TYPES", "EBS").split(",")
    if t.strip()
}

EXECUTION_TAG = "platform-restore-execution"
MODE_TAG = "platform-restore-mode"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def read_tags(resource_type, ident):
    if resource_type == "EBS":
        got = ec2.describe_volumes(VolumeIds=[ident]).get("Volumes", [])
        if not got:
            return None
        return {t["Key"]: t["Value"] for t in got[0].get("Tags", [])}
    if resource_type in ("RDS", "AURORA"):
        got = rds.describe_db_instances(
            DBInstanceIdentifier=ident).get("DBInstances", [])
        if not got:
            return None
        arn = got[0]["DBInstanceArn"]
        listed = rds.list_tags_for_resource(ResourceName=arn).get("TagList", [])
        return {t["Key"]: t["Value"] for t in listed}
    return None


def delete(resource_type, ident):
    if resource_type == "EBS":
        ec2.delete_volume(VolumeId=ident)
        return {"api": "ec2:DeleteVolume", "resource": ident}
    if resource_type in ("RDS", "AURORA"):
        rds.delete_db_instance(
            DBInstanceIdentifier=ident,
            # No final snapshot: a snapshot of a restored test copy is a second
            # copy of the same data with nobody responsible for it.
            SkipFinalSnapshot=True,
            # FALSE, deliberately. Deleting automated backups is deleting
            # backups, and this function does not do that under any
            # circumstances. Any that exist are reported below so a human can
            # decide, rather than being purged by a workflow whose whole
            # purpose is proving backups work.
            DeleteAutomatedBackups=False,
        )
        return {"api": "rds:DeleteDBInstance", "resource": ident,
                "note": "automated backups of the restored copy, if any, are "
                        "LEFT IN PLACE. This function does not delete backups. "
                        "Check for them if the account's cost moves."}
    raise RuntimeError(f"no delete implementation for {resource_type}")


def orphan(reason, detail):
    """Report, delete nothing.

    Routed digest_and_jira — a dated ticket and the digest, not a page. An
    orphaned test resource is real work and costs real money, and it is not
    worth waking anybody for at 3am.
    """
    body = {
        "record_type": "restore-teardown-orphan",
        "recorded_at": _now().isoformat(),
        "environment_tier": ENVIRONMENT_TIER,
        "reason": reason,
        **detail,
    }
    dossier = {
        "record_type": body["record_type"],
        "recorded_at": body["recorded_at"],
        "current_state": "ALARM",
        "routing": "digest_and_jira",
        "alarm": {
            "name": "platform-restore-teardown-orphan",
            "resource_id": detail.get("created_resource_arn") or "unknown",
            "environment_tier": ENVIRONMENT_TIER,
        },
        "assessment": {"probable_cause": reason},
        "teardown": body,
    }
    events.put_events(Entries=[{
        "Source": "platform.recovery",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
    }])
    print(json.dumps(body, default=str))
    return {"deleted": False, "orphan": True, "reason": reason}


def handler(event, context):
    state = event.get("state") or {}
    execution_id = event.get("execution_id") or ""
    resolved = state.get("resolved") or {}
    mode = resolved.get("mode") or state.get("mode") or ""
    arn = (state.get("poll") or {}).get("created_resource_arn") or ""
    resource_type = ((resolved.get("recovery_point") or {}).get("ResourceType")
                     or "").upper()

    # Defence in depth against an ASL edit. The Choice in the state machine is
    # what is supposed to stop this running in recovery mode; this is what
    # stops it if somebody rewires the Choice. Two independent gates, because
    # the failure they guard against is deleting a just-recovered production
    # resource during an outage.
    if mode != "test":
        return orphan(
            f"teardown was invoked in mode '{mode}'. It runs in test mode only "
            "— prompt 6.1: recovery mode validates, records and STOPS. Nothing "
            "was deleted.",
            {"created_resource_arn": arn, "mode": mode})

    if not arn:
        # Nothing was created — the usual case for a run that found no recovery
        # point, or whose restore failed before producing anything.
        print(json.dumps({"record_type": "restore-teardown",
                          "recorded_at": _now().isoformat(),
                          "deleted": False,
                          "reason": "the run created no resource"}))
        return {"deleted": False, "orphan": False,
                "reason": "the run created no resource"}

    ident = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]

    if resource_type not in TEARDOWN_RESOURCE_TYPES:
        return orphan(
            f"teardown is not armed for resource type '{resource_type}' in this "
            f"account (armed: {', '.join(sorted(TEARDOWN_RESOURCE_TYPES)) or 'none'}). "
            "The resource is left running and needs a human. See B-045.",
            {"created_resource_arn": arn, "resource_type": resource_type,
             "execution_id": execution_id})

    try:
        tags = read_tags(resource_type, ident)
    except Exception as exc:                       # noqa: BLE001
        return orphan(
            f"could not read the tags of {arn}, so its provenance cannot be "
            f"established: {type(exc).__name__}: {exc}",
            {"created_resource_arn": arn, "execution_id": execution_id})

    if tags is None:
        # Already gone. Not a problem, and not silently ignored either.
        print(json.dumps({"record_type": "restore-teardown",
                          "recorded_at": _now().isoformat(), "deleted": False,
                          "reason": f"{arn} no longer exists"}))
        return {"deleted": False, "orphan": False,
                "reason": f"{arn} no longer exists"}

    claimed = tags.get(EXECUTION_TAG)
    tagged_mode = tags.get(MODE_TAG)
    if claimed != execution_id or tagged_mode != "test":
        # THE important branch. Provenance failed, so nothing is deleted.
        return orphan(
            f"{arn} does not carry this execution's claim. Expected "
            f"{EXECUTION_TAG}={execution_id} and {MODE_TAG}=test; found "
            f"{EXECUTION_TAG}={claimed!r} and {MODE_TAG}={tagged_mode!r}. "
            "Nothing was deleted — a resource this workflow cannot prove it "
            "created is not one it may remove.",
            {"created_resource_arn": arn, "execution_id": execution_id,
             "found_tags": {EXECUTION_TAG: claimed, MODE_TAG: tagged_mode}})

    result = delete(resource_type, ident)
    record = {
        "record_type": "restore-teardown",
        "recorded_at": _now().isoformat(),
        "deleted": True,
        "execution_id": execution_id,
        "created_resource_arn": arn,
        "provenance": {EXECUTION_TAG: claimed, MODE_TAG: tagged_mode},
        **result,
    }
    print(json.dumps(record, default=str))
    return {"deleted": True, "orphan": False, **result}
