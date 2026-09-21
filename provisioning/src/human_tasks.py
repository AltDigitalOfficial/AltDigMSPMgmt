"""The human task layer. Prompt 12.4, design 13 "Human tasks".

The gate is the whole module, and it has three answers not two
---------------------------------------------------------------
design 13 splits onboarding tasks into blocking and non-blocking, and says why
the split has to exist at all:

    "If everything is blocking, nothing goes live. If nothing is blocking,
    something goes live without a BAA."

So blocking tasks gate the transition `provisioned -> verifying`, and
non-blocking ones are created, owned and dated and gate nothing.

The part that matters more is what happens when the gate cannot tell. Asking
Jira whether eight tickets are closed can fail three ways, and only one of them
means "not yet":

    clear           every blocking task is closed
    blocked         at least one is open, and it is named
    undeterminable  Jira could not be asked, or the task set does not exist

`undeterminable` is a separate verdict rather than a flavour of `blocked`
because the two want different reactions. `blocked` is the system working --
somebody has a signature to chase. `undeterminable` means the gate is not
functioning, and a gate nobody can read is not a gate. Collapsing them into
"not clear" would be safe in the moment and would hide a broken control for as
long as it stayed broken, because the tenant simply never advances and nothing
says why.

The empty set is the dangerous one, and it looks like success
---------------------------------------------------------------
Zero open blocking tasks and zero blocking tasks are the same number. The first
means the gate is clear; the second means the onboarding set was never created
and nothing is gating anything. Reporting the second as the first would send a
tenant to `verifying` with no BAA, no signed matrix and no billing -- the exact
outcome design 13's split exists to prevent.

This is B-086, B-092 and B-094 again, and it is the reason `gate` refuses to
return `clear` unless it can say how many blocking tasks it examined.

A stall is not an alarm, but it must not be silent
----------------------------------------------------
prompt 12.4: "an account sitting in provisioned for three weeks waiting on a
signature should be visible, not silent."

It is not a page. Nobody is on call for a countersignature, and paging for one
trains people to ignore pages. It goes to the digest and to a Jira ticket,
which is where work that needs a person but not an interruption belongs.

The threshold below is an ASSUMPTION -- design 13 gives no number. Fourteen
days is chosen to be shorter than the three weeks the prompt uses as its
example of "too long", so the finding arrives before somebody notices manually.
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

# See the module docstring: invented, not from the design.
STALL_DAYS = int(os.environ.get("HUMAN_TASK_STALL_DAYS", "14"))

# The states in which an outstanding human task is worth reporting. A tenant
# still in `provisioning` has automated steps outstanding too, and reporting a
# stalled signature there would bury it under work that has not been asked for
# yet.
WAITING_STATES = ("provisioned", "verifying")


class GateUndeterminable(Exception):
    """The gate could not be evaluated. Distinct from the gate being shut."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def gate(tenant_id):
    """May this tenant move from `provisioned` to `verifying`?

    Returns a verdict and, in every case, what was examined to reach it. A
    verdict without a count is not auditable: "clear" is only meaningful
    alongside the number of blocking tasks that were found and closed.
    """
    rec = saga.record(tenant_id)
    if not rec:
        return {"tenant_id": tenant_id, "verdict": "undeterminable",
                "blocking_total": None, "outstanding": None,
                "why": "no registry record. Nothing is known about this "
                       "tenant, including whether it has an onboarding set."}

    if not rec.get("jira_onboarding_key"):
        # Step 4 has not run. Distinct from "the tasks are all closed", and
        # the distinction is the whole point of this module.
        return {"tenant_id": tenant_id, "verdict": "undeterminable",
                "blocking_total": 0, "outstanding": None,
                "why": "the registry carries no jira_onboarding_key, so "
                       "provisioning step 4 has not created the onboarding "
                       "task set. There are no blocking tasks because there "
                       "are no tasks -- that is not a clear gate."}

    # Imported lazily for the same reason saga does it: the Jira client builds
    # a Secrets Manager client at import time.
    import jira_tenant
    from jira import JiraUnavailable

    try:
        found = jira_tenant.blocking_outstanding(tenant_id)
    except JiraUnavailable as exc:
        # "Jira says nothing is outstanding" and "nobody could ask Jira" must
        # not produce the same verdict. This is B-054's rule applied to the
        # gate.
        return {"tenant_id": tenant_id, "verdict": "undeterminable",
                "blocking_total": None, "outstanding": None,
                "why": f"Jira could not be asked: {exc}. No conclusion can be "
                       "drawn about the blocking set, and an unreadable gate "
                       "is not an open one."}

    if not found.get("onboarding_present"):
        return {"tenant_id": tenant_id, "verdict": "undeterminable",
                "blocking_total": 0, "outstanding": None,
                "why": found.get("why") or
                       "no blocking tasks exist for this tenant."}

    outstanding = found.get("outstanding") or []
    total = found.get("blocking_total")
    if outstanding:
        return {"tenant_id": tenant_id, "verdict": "blocked",
                "blocking_total": total,
                "outstanding": outstanding,
                "why": f"{len(outstanding)} of {total} blocking onboarding "
                       "tasks are still open. design 13 gates the transition "
                       "to verifying on all of them."}

    return {"tenant_id": tenant_id, "verdict": "clear",
            "blocking_total": total, "outstanding": [],
            "why": f"all {total} blocking onboarding tasks are closed."}


def advance(event, context=None):
    """Move a tenant to `verifying` if, and only if, the gate is clear.

    The transition is the only thing this does. It does not run the
    verification sweep and it does not approve go-live -- step 8 and step 9
    own those, and a function that did all three would make the gate a
    formality inside a larger success path.
    """
    tenant_id = (event or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("advance needs tenant_id")

    verdict = gate(tenant_id)
    rec = saga.record(tenant_id) or {}
    state = rec.get("state")

    if verdict["verdict"] != "clear":
        out = {**verdict, "record_type": "human-task-gate",
               "state": state, "advanced": False}
        print(json.dumps(out, default=str))
        return out

    if state != "provisioned":
        # The gate being clear does not make every transition legal. design
        # 13's state machine still applies, and a tenant in `provisioning`
        # has automated steps outstanding regardless of its paperwork.
        out = {**verdict, "record_type": "human-task-gate", "state": state,
               "advanced": False,
               "why": f"the blocking set is clear, but this tenant is "
                      f"'{state}' and only 'provisioned' may move to "
                      "'verifying'."}
        print(json.dumps(out, default=str))
        return out

    saga.transition(tenant_id, "verifying",
                    f"all {verdict['blocking_total']} blocking onboarding "
                    "tasks closed")
    out = {**verdict, "record_type": "human-task-gate", "state": "verifying",
           "advanced": True}
    print(json.dumps(out, default=str))
    return out


# ---------------------------------------------------------------------------
# Making a stall visible
# ---------------------------------------------------------------------------


def _registry_tenants():
    """Every tenant record. Raises rather than returning an empty list.

    A sweep over zero tenants emits zero findings and reports success, which is
    indistinguishable from an estate with nothing stalled in it -- B-086. The
    same rule `evidence/completeness.tenants()` follows, for the same reason.
    """
    items, kwargs = [], {
        "TableName": REGISTRY_TABLE,
        "FilterExpression": "SK = :sk AND begins_with(PK, :pk)",
        "ExpressionAttributeValues": {":sk": {"S": "META"},
                                      ":pk": {"S": "TENANT#"}}}
    while True:
        try:
            page = dynamodb.scan(**kwargs)
        except Exception as exc:                      # noqa: BLE001
            raise GateUndeterminable(
                f"could not read {REGISTRY_TABLE}: {type(exc).__name__}. No "
                "conclusion can be drawn about any tenant's human tasks."
            ) from exc
        items.extend(page.get("Items") or [])
        token = page.get("LastEvaluatedKey")
        if not token:
            break
        kwargs["ExclusiveStartKey"] = token
    return items


def _emit(tenant_id, name, cause, detail):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.provisioning",
        "DetailType": "human-task",
        "Detail": json.dumps({
            "record_type": "human-task-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            # Not a page, deliberately. Nobody is on call for a
            # countersignature, and paging for one teaches people that pages
            # can be ignored. See the module docstring.
            "routing": "digest_and_jira",
            "alarm": {"name": name, "resource_id": tenant_id},
            "assessment": {"probable_cause": cause},
            "human_tasks": detail,
        }, default=str),
    }])


def sweep(event=None, context=None):
    """Report every tenant waiting on a person, and for how long.

    Reports what it examined, not only what it concluded. A sweep that says
    "nothing stalled" without saying how many tenants it looked at is the
    failure this codebase keeps rediscovering.
    """
    now = _now()
    threshold = int((event or {}).get("stall_days") or STALL_DAYS)

    records = _registry_tenants()

    examined, stalled, unreadable, not_waiting = [], [], [], []
    for item in records:
        tenant_id = (item.get("tenant_id") or {}).get("S")
        state = (item.get("state") or {}).get("S")
        if not tenant_id:
            continue
        if state not in WAITING_STATES:
            not_waiting.append({"tenant_id": tenant_id, "state": state})
            continue

        examined.append(tenant_id)
        verdict = gate(tenant_id)

        # Age from when the record last MOVED, not from a due date on a ticket
        # and deliberately not from updated_at. A due date is a target
        # somebody set; updated_at moves on any identifier write-back and
        # would reset the clock for unrelated reasons. created_at is the
        # honest fallback for records written before state_changed_at existed.
        since = _parse((item.get("state_changed_at") or {}).get("S")
                       or (item.get("created_at") or {}).get("S"))
        days = round((now - since).total_seconds() / 86400.0, 1) if since else None

        if verdict["verdict"] == "undeterminable":
            # Falls through to a human rather than ending here. A tenant whose
            # gate cannot be read will never advance and would otherwise sit
            # silently forever.
            unreadable.append({**verdict, "state": state, "days": days})
            _emit(tenant_id, "platform-human-task-gate-unreadable",
                  f"The human task gate for {tenant_id} cannot be evaluated, "
                  "so this tenant cannot advance and nothing else will say "
                  "so. " + verdict["why"],
                  {"state": state, "days_in_state": days,
                   "verdict": verdict["verdict"], "why": verdict["why"]})
            continue

        if verdict["verdict"] == "blocked" and days is not None \
                and days >= threshold:
            stalled.append({**verdict, "state": state, "days": days})
            names = ", ".join(t.get("summary", t.get("key", "?"))
                              for t in (verdict["outstanding"] or [])[:3])
            _emit(tenant_id, "platform-human-task-stalled",
                  f"{tenant_id} has been '{state}' for {days} days waiting on "
                  f"{len(verdict['outstanding'])} blocking onboarding "
                  f"task(s): {names}. Provisioning is complete; this is "
                  "waiting on a person.",
                  {"state": state, "days_in_state": days,
                   "threshold_days": threshold,
                   "outstanding": verdict["outstanding"],
                   "blocking_total": verdict["blocking_total"]})

    out = {
        "record_type": "human-task-sweep",
        "swept_at": _iso(now),
        "threshold_days": threshold,
        # Counts first, and all of them. "0 stalled" means nothing without
        # "of N examined".
        "records_in_registry": len(records),
        "examined": len(examined),
        "not_waiting": len(not_waiting),
        "stalled": stalled,
        "unreadable": unreadable,
        "stalled_count": len(stalled),
        "unreadable_count": len(unreadable),
        "tenants_examined": examined,
    }
    print(json.dumps(out, default=str))
    return out


HANDLERS = {
    "gate": lambda e: gate((e or {}).get("tenant_id")),
    "advance": advance,
    "sweep": sweep,
}


def handler(event, context=None):
    action = (event or {}).get("action") or "sweep"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(HANDLERS)}.")
    return fn(event or {})
