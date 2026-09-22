"""Dormant, and the way back. Prompt 13.5, design 14 "Seasonal lifecycle".

Dormant is deprovisioning doing useful work outside of exit
-------------------------------------------------------------
A customer releasing quarterly may want UAT for two weeks and gone for ten.
design 14: "for genuinely seasonal environments, teardown is cheaper and
cleaner than scheduling -- and it sidesteps the RDS seven-day problem
entirely."

That last clause is the whole argument. A HOOP cannot hold an environment down
for ten weeks: AWS restarts stopped databases after seven days, so a scheduled
environment idle for a season comes back up on day eight and bills normally,
twice a quarter, for nothing. Scheduling is the wrong tool past about a week
and teardown is the right one.

The account is NOT deleted, and that is the load-bearing constraint
---------------------------------------------------------------------
design 14: "Deleting and recreating an AWS account loses the account ID, the
evidence continuity and the Truveon linkage. Dormant accounts retain
everything and cost almost nothing."

Three things break if the account goes, and none of them is obvious at the
moment somebody decides to tidy up:

  the account id      every evidence record, every CloudTrail entry and every
                      cost line ever written for this tenant names it. A new
                      account is a new tenant as far as the audit trail is
                      concerned, and the old one's history is orphaned.
  evidence continuity the retention clock keeps running while dormant. A gap
                      in it is not recoverable by explaining the gap.
  Truveon linkage     the tenant is keyed on the account. Re-creating means
                      re-linking, and re-linking means a manual step during a
                      re-vest that is already under time pressure.

So there is no account-closing code path here. Not a guarded one -- an absent
one. `scripts/publish-provisioning.sh` refuses to package a call to
`close_account` or `remove_account_from_organization` for the same reason the
scheduler refuses to package `terminate_instances`: a failing destructive call
is still a destructive call somebody wrote.

The way down has a gate the way up depends on
-----------------------------------------------
design 14 step 1: "Final backup taken and verified." Not taken -- VERIFIED.

An unverified final backup is the single worst artefact this lifecycle can
produce, because it is indistinguishable from a good one until the re-vest
that needs it. By then the environment has been gone for ten weeks and the
data it held is whatever the backup turns out to contain.

That gate is not satisfiable today (B-043: design 05 is absent and there is no
backup infrastructure), so `teardown` BLOCKS. The same reasoning as B-107's
exit refusal, and the same conclusion: refusing to tear down is recoverable,
and tearing down without a verified backup is not.

Re-vest must be TESTED, not exercised when a customer needs it
----------------------------------------------------------------
design 14: "A seasonal environment that cannot be brought back is a much worse
failure than one that was never torn down."

The failure mode is specific. A teardown succeeds, looks clean, and produces
no signal for ten weeks. The re-vest is attempted on the morning the customer
expects their environment, discovers the gap, and there is no time to fix it.
Everything between those two moments was silent and appeared healthy.

So `scheduled_test` exists to run the cycle against a disposable environment
on a cadence, and its absence is reported rather than assumed.
"""
import datetime
import json
import os

import boto3

import saga

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")

# design 14 step 1, and the gate everything else depends on.
BACKUP_VERIFIED_FIELD = "final_backup_verified_at"
BACKUP_RECOVERY_POINT_FIELD = "final_recovery_point_arn"


class Blocked(Exception):
    """A refusal, with the reason. Not a failure."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(when):
    return when.astimezone(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def final_backup(tenant_id, record=None):
    """Is there a final backup, and has it been VERIFIED?

    Three outcomes, and the middle one is the point. A backup that exists and
    has not been restored from is not a backup, it is a file -- and the
    difference only becomes visible at the moment it matters most.
    """
    record = record or saga.record(tenant_id) or {}
    point = record.get(BACKUP_RECOVERY_POINT_FIELD)
    verified = record.get(BACKUP_VERIFIED_FIELD)

    if not point:
        return {"state": "absent",
                "why": "no final recovery point is recorded for this tenant."}
    if not verified:
        return {"state": "unverified", "recovery_point": point,
                "why": "a recovery point exists and has never been restored "
                       "from. design 14 asks for the final backup to be taken "
                       "AND VERIFIED, and an unverified backup is "
                       "indistinguishable from a good one until the re-vest "
                       "that needs it."}
    return {"state": "verified", "recovery_point": point,
            "verified_at": verified}


def assert_can_teardown(tenant_id, record=None):
    """Refuse a dormant teardown that cannot be undone."""
    record = record or saga.record(tenant_id)
    if not record:
        raise Blocked(
            f"no registry record for {tenant_id}. The record is what a "
            "re-vest rebuilds FROM, so tearing down without one produces a "
            "dormant environment nothing can bring back.")

    state = record.get("state")
    if state not in ("live", "provisioned", "verifying"):
        raise Blocked(
            f"{tenant_id} is '{state}'. Dormancy is for an environment that "
            "is up and will be wanted again; there is nothing to tear down "
            "from any other state.")

    backup = final_backup(tenant_id, record)
    if backup["state"] != "verified":
        # The B-107 shape, applied to the seasonal path. Refusing is
        # recoverable; tearing down without a verified backup is not, and the
        # discovery is ten weeks late.
        raise Blocked(
            f"final backup is {backup['state']}: {backup['why']} Backup "
            "infrastructure does not exist (B-043 -- design 05 is absent from "
            "the package), so this gate cannot currently be satisfied by any "
            "tenant. Refusing is recoverable: the environment stays up and "
            "costs money. Tearing down a seasonal environment whose backup "
            "turns out to be unreadable is not, and the discovery arrives ten "
            "weeks late, on the morning the customer expects it back.")

    return record, backup


# ---------------------------------------------------------------------------
# The way back
# ---------------------------------------------------------------------------


def revest_plan(tenant_id):
    """What a re-vest would do, read from the registry record.

    From the RECORD, not from a stored copy of the infrastructure. design 14:
    "Re-vest from the same registry record when the next cycle begins." The
    record is the declaration of what this tenant is; a snapshot of what it
    looked like would drift from the platform's current baseline, and a
    re-vest that rebuilt a ten-week-old baseline would reintroduce whatever
    has been fixed since.
    """
    record = saga.record(tenant_id)
    if not record:
        raise Blocked(
            f"no registry record for {tenant_id}; there is nothing to rebuild "
            "from. This is the failure the teardown gate exists to prevent.")

    if record.get("state") != "dormant":
        raise Blocked(
            f"{tenant_id} is '{record.get('state')}', not dormant. Re-vest "
            "returns a dormant environment to service and is not a way to "
            "rebuild a live one.")

    backup = final_backup(tenant_id, record)

    return {
        "tenant_id": tenant_id,
        "partner": record.get("partner"),
        # The account is the thing that survived, and the reason dormancy
        # works at all.
        "account_ids": record.get("account_ids"),
        "steps": [
            {"step": 1, "what": "re-run the vesting pipeline against the "
                                "RETAINED accounts",
             "from": "registry record",
             "note": "The accounts already exist. This applies the CURRENT "
                     "baseline to them, which is the point of rebuilding from "
                     "the record rather than from a snapshot."},
            {"step": 2, "what": "restore data from the retained recovery point",
             "from": backup.get("recovery_point"),
             "blocked": backup["state"] != "verified",
             "note": backup["why"] if backup["state"] != "verified"
                     else "restored from the backup taken at teardown"},
            {"step": 3, "what": "re-instrument and re-attach schedules",
             "from": "registry record"},
            {"step": 4, "what": "run the full verification sweep",
             "from": "provisioning/src/verify.py",
             "note": "design 14 and prompt 13.5 both require this BEFORE "
                     "returning to live. A re-vested environment is a new "
                     "deployment of an old promise, and the sweep is what "
                     "distinguishes the two."},
            {"step": 5, "what": "transition dormant -> live",
             "blocked": True,
             "note": "Only after the sweep passes. saga.transition refuses "
                     "dormant -> live directly, so this cannot be "
                     "short-circuited by calling it."},
        ],
        "backup": backup,
    }


def revest(event, context=None):
    """Return a dormant environment to service.

    Not implemented as an executing path, and the reason is not effort: every
    step it would perform belongs to a pipeline that does not exist yet.
    Step 1 is the vesting pipeline (phase 3), step 2 needs backup
    infrastructure (B-043), step 3 is per-tenant instrumentation (B-106).

    What IS implemented is the plan and the refusals, because those are what
    make the gap legible. A re-vest that ran three of five steps and reported
    success would produce an environment the customer is told is back.
    """
    tenant_id = (event or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("revest needs tenant_id")

    plan = revest_plan(tenant_id)
    blocked = [s for s in plan["steps"] if s.get("blocked")]

    out = {"record_type": "revest-plan", "executed": False, **plan,
           "blocked_steps": [s["step"] for s in blocked],
           "why": "re-vest is a plan and not yet an execution. Steps depend "
                  "on the vesting pipeline (phase 3), backup infrastructure "
                  "(B-043) and per-tenant instrumentation (B-106). Running "
                  "the steps that DO exist would return an environment to "
                  "service with no data in it."}
    print(json.dumps(out, default=str))
    return out


# ---------------------------------------------------------------------------
# Testing the way back, on a schedule
# ---------------------------------------------------------------------------


def scheduled_test(event=None, context=None):
    """Has the re-vest path been exercised recently enough to trust?

    design 14: "Re-vest must be tested, on the same principle as everything
    else. A seasonal environment that cannot be brought back is a much worse
    failure than one that was never torn down."

    The failure this guards against is silent by construction. A teardown
    succeeds, looks clean, and emits nothing for ten weeks; the re-vest is
    attempted on the morning the customer expects their environment, and there
    is no time left to fix what it finds. Everything in between looked healthy.

    So the absence of a recent test is itself the finding -- reported rather
    than inferred from the fact that nobody has complained.
    """
    max_age_days = int((event or {}).get("max_age_days") or 90)

    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": "PLATFORM#revest"}, "SK": {"S": "LAST_TEST"}})
    item = got.get("Item")

    if not item:
        finding = {
            "state": "never",
            "why": "the re-vest path has never been exercised. It is not "
                   "implemented either (see revest()), so this is expected "
                   "today and must not become expected tomorrow: the first "
                   "seasonal customer makes an untested re-vest a commitment.",
        }
    else:
        when = (item.get("tested_at") or {}).get("S")
        age = None
        try:
            then = datetime.datetime.fromisoformat(
                str(when).replace("Z", "+00:00"))
            age = round((_now() - then).total_seconds() / 86400.0, 1)
        except (ValueError, TypeError):
            pass
        if age is None:
            finding = {"state": "unreadable",
                       "why": f"the last-test record exists and its timestamp "
                              f"({when}) cannot be parsed, so its age is "
                              "unknown. Unknown is not recent."}
        elif age > max_age_days:
            finding = {"state": "stale", "age_days": age,
                       "why": f"the re-vest path was last exercised {age} days "
                              f"ago, over the {max_age_days}-day limit."}
        else:
            finding = {"state": "current", "age_days": age}

    out = {"record_type": "revest-test-status",
           "checked_at": _iso(_now()),
           "max_age_days": max_age_days,
           **finding}

    if finding["state"] != "current":
        events_client.put_events(Entries=[{
            "EventBusName": ALERT_BUS_NAME,
            "Source": "platform.provisioning",
            "DetailType": "revest-test",
            "Detail": json.dumps({
                "record_type": "revest-test-finding",
                "recorded_at": _iso(_now()),
                "current_state": "ALARM",
                # Not a page. Nobody is woken because a quarterly exercise is
                # overdue -- but it must not wait for somebody to wonder.
                "routing": "digest_and_jira",
                "alarm": {"name": "platform-revest-untested",
                          "resource_id": "platform"},
                "assessment": {"probable_cause": finding["why"]},
                "revest": out,
            }, default=str),
        }])

    print(json.dumps(out, default=str))
    return out


HANDLERS = {
    "plan": revest,
    "test-status": scheduled_test,
}


def handler(event, context=None):
    action = (event or {}).get("action") or "plan"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(f"unknown action '{action}'. Known: {', '.join(HANDLERS)}.")
    return fn(event or {}, context)
