"""Deprovisioning. Prompt 12.6, design 13 "Deprovisioning — built at the same time".

    Same machine, reverse order, reading the same registry record.

Built before the remaining saga steps, deliberately. design 13 gives three
reasons it is not a "later" item, and the second is the operative one here:

    It is how failed provisioning is cleaned up. Without it, a run that dies at
    step 6 leaves debris someone unpicks by hand, badly.

A first provisioning run is far more likely to halt than to complete. The unwind
path is therefore not the exceptional case -- it is the expected one, and
building further steps before it means building more things to unpick.

The one rule that makes this safe
-----------------------------------
It removes only what the REGISTRY RECORD names. It never discovers resources.

There is an obvious, tempting alternative: list everything tagged for this
tenant and remove it. That version is shorter, catches resources the record
missed, and is the reason not to write it -- a discovery-based teardown removes
whatever it finds, and what it finds depends on a tag, a filter and an account
boundary all being right at once. The record is a list of things this platform
created, written by the code that created them.

So the failure direction is: something created outside provisioning survives a
teardown. That is recoverable by deleting it later. The opposite failure --
removing something nobody asked about -- is not.

CLAUDE.md's rule against destructive automation
-------------------------------------------------
    Never write code that terminates, deletes, deregisters, rolls back or
    purges a resource as part of automated remediation.

This is the second place in the platform that removes anything, and the reading
is the same as for the quorum executor. The rule forbids automation DECIDING to
destroy something in response to a condition it observed. Nothing here observes
or decides: a human asks for an exit, a cleanup or a seasonal teardown, and the
machine removes exactly what the registry says was created for that tenant.

The distinction is not a loophole. Containment must never delete because it
reacts to a signal that might be wrong. Deprovisioning acts on an instruction
that is not a signal at all.

Three modes, three target states
----------------------------------
    exit     contractual exit. Extraction first, verified, then removal, then
             an evidence record. design 10 makes this a commitment.
    cleanup  unwind a halted provisioning. Removes what that run created.
    dormant  seasonal teardown. The account is RETAINED, the registry record
             and the evidence are preserved, and the tenant is re-vestable from
             the same record. design 14.

`dormant` is the one most easily got wrong by treating teardown as one thing:
deleting the record would make re-vesting impossible, which is the opposite of
what it is for.
"""
import datetime
import json
import os

import boto3

import saga

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
EVIDENCE_TABLE = os.environ.get("EXIT_EVIDENCE_TABLE", "")

MODES = ("exit", "cleanup", "dormant")


class Refused(Exception):
    """The teardown will not proceed, and says why.

    Distinct from a failure. A refusal means the machine declined; a failure
    means it tried and could not. Somebody reading the second needs to check
    what state things are in, and somebody reading the first does not.
    """


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


# What each saga step created, and how to remove it. Keyed by the registry
# field the step wrote back, which is the whole safety property: if the field
# is absent, the step never completed and there is nothing to remove.
#
# `preserve_in` lists modes where the artefact is KEPT rather than removed.
UNDO = [
    {
        "step": 7, "field": "cost_category_synced_at",
        "what": "Cost Category membership and budgets",
        # Cost history is evidence. Removing a tenant from a Cost Category
        # rewrites what last quarter cost, which is a billing record somebody
        # may still need.
        "preserve_in": ("dormant", "exit"),
        "note": "Cost Categories are edited to drop the account, never "
                "deleted -- their history is attached to the ARN and an "
                "auditor asking what a closed month cost needs it.",
    },
    {
        "step": 6, "field": "instrumented_at",
        "what": "instrumentation, canaries, backup and HOOP schedules",
        "preserve_in": (),
    },
    {
        "step": 5, "field": "account_ids",
        "what": "AWS accounts",
        # design 14: dormant retains the account. Seasonal teardown removes
        # the workload, not the account -- re-vesting a closed account is not
        # possible, so treating dormant like exit here would be permanent.
        "preserve_in": ("dormant",),
        "note": "Accounts are CLOSED on exit and RETAINED when dormant. "
                "Closing is irreversible on a 90-day clock; a seasonal "
                "teardown that closed the account could never be re-vested.",
    },
    {
        "step": 4, "field": "jira_tenant_label",
        "what": "Jira tenant label and the issues carrying it",
        # Change records are the evidence that change management operated.
        "preserve_in": ("dormant", "exit"),
        "note": "Preserved, not archived, and the distinction now matters. "
                "Provisioning could not create a per-tenant project (B-108), "
                "so the tenant's artefacts are LABELLED issues inside the "
                "SHARED ops project. Nothing here may touch that project: "
                "removing it would take every other tenant's change records "
                "with it. Only issues carrying this label are in scope, and "
                "even those are preserved -- they are design 08's evidence "
                "that change management operated, and they outlive the "
                "tenant.",
    },
    {
        "step": 3, "field": "pagerduty_service_id",
        "what": "PagerDuty service, escalation policy and routing keys",
        "preserve_in": (),
    },
    {
        "step": 2, "field": "truveon_tenant_id",
        "what": "Truveon tenant",
        # The evidence store. Never removed by this path in any mode.
        "preserve_in": ("dormant", "exit", "cleanup"),
        "note": "NEVER removed here. Truveon holds the evidence that the "
                "platform operated, including the evidence about this "
                "teardown. Retention is a contractual question answered "
                "elsewhere, and a teardown that deleted it would destroy the "
                "record of itself.",
    },
    {
        "step": 1, "field": "tenant_id",
        "what": "registry record",
        # The record is the only list of what existed. It survives everything
        # except a cleanup of a tenant that never got past step 1.
        "preserve_in": ("dormant", "exit"),
        "note": "Preserved on exit and dormant. It is the only description of "
                "what was created, which is what an exit evidence record and "
                "a re-vest both read.",
    },
]


# ---------------------------------------------------------------------------
# Refusals, which are the control
# ---------------------------------------------------------------------------


def authorise(rec, mode, authorised_by=None):
    """Decide whether this teardown may proceed at all.

    Every refusal here is cheaper than the thing it prevents, and none of them
    is recoverable by apologising.
    """
    state = rec.get("state")

    if mode not in MODES:
        raise Refused(f"'{mode}' is not a mode. Known: {', '.join(MODES)}.")

    # A live tenant is one with an SLA, billing and a place in the on-call
    # rotation. Tearing one down because a script was pointed at the wrong id
    # is the worst outcome this module has, so it costs an explicit name.
    if state == "live" and not authorised_by:
        raise Refused(
            f"{rec.get('tenant_id')} is LIVE. design 13 starts SLA "
            "measurement, billing and on-call rotation on that transition, so "
            "a teardown needs an explicit authorised_by. If this is a "
            "contractual exit, pass one; if it is not, this is the wrong "
            "tenant.")

    # cleanup is for unwinding a halted provisioning, not for removing a
    # working tenant. Using it on a live tenant would skip the extraction that
    # exit performs -- the customer's data would be gone with no copy.
    if mode == "cleanup" and state in ("live", "provisioned"):
        raise Refused(
            f"cleanup is for a halted provisioning and {rec.get('tenant_id')} "
            f"is {state}. cleanup performs NO data extraction, so running it "
            "here would remove a working tenant without a copy of anything. "
            "Use exit.")

    if state == "dormant" and mode == "dormant":
        raise Refused(
            f"{rec.get('tenant_id')} is already dormant. Nothing to tear down.")

    return True


def extract(rec, mode):
    """Exit only: get the data out, and confirm it came out.

    prompt 12.6: "the data extraction path must be real and tested --
    application data, tenant logs from the archive, and the Truveon tenant if
    retained."

    Not built, and it BLOCKS rather than being skipped. An exit that removed a
    tenant's accounts without extracting their data first is unrecoverable and
    is a contractual failure (design 10 makes exit a commitment). Returning
    "nothing to extract" would be the single most expensive no-op in this
    codebase.
    """
    if mode != "exit":
        return None
    raise Refused(
        "Exit requires a verified data extraction before anything is removed, "
        "and the extraction path is not built (B-107). It must cover "
        "application data, tenant logs from the archive, and the Truveon "
        "tenant if retained. Removing accounts first is unrecoverable and "
        "design 10 makes exit a contractual commitment -- so this blocks "
        "rather than proceeding.")


def entanglement(rec):
    """prompt 12.6: verify nothing of AltDigital's needs surgical removal.

    Returns a list of concerns. Empty means the member account holds nothing
    of the platform's that a customer taking the account over would inherit.

    Not built. Reported as unknown rather than clean -- the same rule as
    everywhere else here, and the consequence of getting it wrong is a
    customer inheriting a role that can still be assumed from AltDigital's
    accounts.
    """
    return None


# ---------------------------------------------------------------------------
# The reverse walk
# ---------------------------------------------------------------------------


def _emit(tenant_id, name, cause, routing="digest_and_jira", detail=None):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.provisioning",
        "DetailType": "deprovisioning",
        "Detail": json.dumps({
            "record_type": "deprovisioning-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": tenant_id},
            "assessment": {"probable_cause": cause},
            "deprovisioning": detail or {},
        }, default=str),
    }])


def plan(rec, mode):
    """What this teardown would remove, without removing anything.

    Separate from execution on purpose. A teardown is the one operation where
    somebody should be able to read what is about to happen before it does,
    and where "run it and see" is not available.
    """
    steps = []
    for undo in UNDO:
        present = rec.get(undo["field"])
        if not present:
            steps.append({**_summary(undo), "action": "nothing",
                          "why": "the step never completed; the registry has "
                                 "no identifier for it, so there is nothing "
                                 "this platform created to remove"})
            continue
        if mode in undo["preserve_in"]:
            steps.append({**_summary(undo), "action": "preserve",
                          "why": undo.get("note") or
                                 f"preserved in {mode} mode"})
            continue
        steps.append({**_summary(undo), "action": "remove",
                      "identifier": present,
                      "why": undo.get("note") or "created by provisioning"})
    return steps


def _summary(undo):
    return {"step": undo["step"], "what": undo["what"], "field": undo["field"]}


def run(event, context=None):
    """Tear a tenant down, in reverse order, bounded by the registry record."""
    tenant_id = (event or {}).get("tenant_id")
    mode = (event or {}).get("mode")
    authorised_by = (event or {}).get("authorised_by")
    dry_run = bool((event or {}).get("dry_run"))

    if not tenant_id:
        raise ValueError("deprovision needs tenant_id")

    rec = saga.record(tenant_id)
    if not rec:
        # No record means either nothing was ever created, or somebody removed
        # the only description of what was. Both refuse: a teardown with no
        # list of what to tear down would have to discover, which is the thing
        # this module will not do.
        raise Refused(
            f"no registry record for {tenant_id}. There is no list of what "
            "was created, and this does not discover resources -- see the "
            "module docstring. If the record was deleted, recover it before "
            "tearing anything down.")

    authorise(rec, mode, authorised_by)

    steps = plan(rec, mode)
    if dry_run:
        out = {"record_type": "deprovisioning-plan", "tenant_id": tenant_id,
               "mode": mode, "state": rec.get("state"),
               "would_remove": [s for s in steps if s["action"] == "remove"],
               "would_preserve": [s for s in steps if s["action"] == "preserve"],
               "nothing_to_do": [s for s in steps if s["action"] == "nothing"],
               "executed": False}
        print(json.dumps(out, default=str))
        return out

    # Exit extracts first, and blocks if it cannot confirm the extraction.
    extract(rec, mode)

    concerns = entanglement(rec)
    if mode == "exit" and concerns is None:
        raise Refused(
            "The entanglement check is not built (B-107), so it cannot be "
            "confirmed that nothing of AltDigital's remains in the member "
            "account. prompt 12.6 requires that verification before exit, and "
            "an unverified answer is not a clean one -- a customer taking the "
            "account over would inherit whatever was missed.")

    removed, preserved, failed = [], [], []
    for step in steps:
        if step["action"] == "preserve":
            preserved.append(step)
            continue
        if step["action"] == "nothing":
            continue
        # The removals themselves are not implemented: each undoes a
        # provisioning step that does not exist yet. Blocking is correct --
        # reporting a removal that did not happen would leave the registry
        # saying a thing is gone while it is not.
        failed.append({**step, "problem": (
            f"removal of {step['what']} is not implemented; the provisioning "
            "step that creates it is not built either (B-106)")})

    out = {
        "record_type": "deprovisioning-run",
        "tenant_id": tenant_id,
        "mode": mode,
        "ran_at": _iso(_now()),
        "authorised_by": authorised_by,
        "removed": removed,
        "preserved": preserved,
        "failed": failed,
        "executed": True,
        "complete": not failed,
    }

    if failed:
        _emit(tenant_id, "platform-deprovisioning-incomplete",
              f"{len(failed)} teardown step(s) could not run. The tenant is "
              "partially torn down and the registry record still describes "
              "what remains.", routing="digest_and_jira",
              detail={"failed": failed})

    print(json.dumps(out, default=str))
    return out


HANDLERS = {"run": run, "plan": lambda e, c=None: run({**e, "dry_run": True}, c)}


def handler(event, context):
    action = (event or {}).get("action") or "run"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(f"unknown action '{action}'")
    return fn(event, context)
