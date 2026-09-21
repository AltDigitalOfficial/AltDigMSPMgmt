"""The provisioning saga. Prompt 12.1, design/13-client-provisioning.md.

    This is NOT a pipeline -- none of it can be transactionally rolled back.
    You cannot un-create an AWS account.

Everything here follows from that. A pipeline can fail and leave nothing
behind; a saga fails and leaves a partial tenant, so the engine's job is not to
avoid failure but to make the partial state legible and recoverable.

Four properties, and the third shapes the other three
------------------------------------------------------
    Idempotent    every step safely re-runnable
    Resumable     step 9 can run after step 7 failed at 2am
    Fails closed  a half-provisioned tenant is worse than none because it
                  LOOKS SUPPORTED
    Self-cleaning failed provisioning is unwound by the deprovisioning path

"Looks supported" is the whole risk. A tenant with accounts, alarms and a Jira
project but no Truveon stream has evidence going nowhere, and everything about
it reads as working. So the engine's default on any uncertainty is `blocked`,
which is visible, rather than `provisioned`, which is not.

Doing a thing and confirming a thing are separate
---------------------------------------------------
Every step is a pair: an action, and a check that the action's effect EXISTS.
The check runs before the action -- that is the idempotency -- and again after
it, and only the second check's answer is believed.

"I called the API and got no error" is not confirmation. An API that accepted a
request is not an API that completed one, and the gap between those two is
where a half-provisioned tenant comes from.

Resumability reads the registry, it does not remember
------------------------------------------------------
design 13: "The registry record is the spine. Create it FIRST, in provisioning
state. Nothing else starts until it exists. Every system writes its identifiers
back to it."

So resuming is not replaying an execution history -- it is asking the registry
what exists and continuing from there. A saga resumed a week later, from a
different machine, by a different person, reaches the same conclusion as one
resumed immediately, because the state was never in the runtime.

Steps 2-4 before step 5, and that ordering is load-bearing
------------------------------------------------------------
design 13: external systems are "most likely to fail on a credential or
permission problem, and failing before AWS accounts exist is far cheaper than
failing after". Truveon, PagerDuty and Jira come before the accounts.

Reversing it is tempting -- AWS is the part that works -- and produces the
expensive failure: four accounts created, baselines deployed, and then a Jira
token that expired last week.
"""
import datetime
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")


# design 13's state machine. Written out rather than implied, because the
# transition that matters is invisible in code that just sets strings.
#
#   provisioning -> provisioned -> verifying -> live -> dormant
#                         |                       |
#                         +-> blocked             +-> (re-vest) -> live
#
# provisioned and live are deliberately different. design 13: "three things key
# off the transition: SLA measurement starts, billing starts, and the account
# enters the on-call rotation." An account may sit in provisioned indefinitely
# and that is the correct state for an application still being migrated.
STATES = {
    "provisioning": {"provisioned", "blocked"},
    "provisioned": {"verifying", "blocked", "dormant"},
    "verifying": {"live", "blocked"},
    # live -> live is not a typo: re-vesting a dormant environment returns it
    # to live through the same path.
    "live": {"dormant", "blocked"},
    "dormant": {"provisioning", "blocked"},
    # Terminal. Recoverable only by a human deciding what to do, which is the
    # point -- a blocked tenant should not resolve itself.
    "blocked": set(),
}

TERMINAL = {"blocked"}


class StepNotImplemented(Exception):
    """This step's system does not exist yet.

    A distinct failure, and it BLOCKS. The alternative -- treating an
    unimplemented step as a no-op -- produces a tenant that completed
    provisioning without a Truveon stream, which is exactly the
    "looks supported" failure the saga exists to prevent.
    """


class StepUnconfirmed(Exception):
    """The action ran and its effect could not be confirmed.

    Separate from an action that errored, because the two mean different
    things to whoever picks it up: an error means it did not happen, and this
    means nobody knows. The second is worse and must not be reported as the
    first.
    """


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The registry, which is the spine
# ---------------------------------------------------------------------------


def record(tenant_id):
    """The registry record, or None. Every decision reads this, not memory."""
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"TENANT#{tenant_id}"}, "SK": {"S": "META"}})
    item = got.get("Item")
    if not item:
        return None
    return {k: (v.get("S") or v.get("N") or v.get("BOOL"))
            for k, v in item.items()}


def write_back(tenant_id, **identifiers):
    """Record what a step created, on the record, immediately.

    design 13: "Every system writes its identifiers back to it -- AWS account
    IDs, Jira project key, Truveon tenant ID, PagerDuty service and routing key
    IDs, Cost Category mapping."

    Immediately, not at the end. A saga that batched its writes and then failed
    would leave things created in four systems and no record of any of them,
    which is the unwind problem made unsolvable: the deprovisioning path reads
    this record to know what to remove.
    """
    if not identifiers:
        return
    sets, names, values = [], {}, {}
    for i, (key, value) in enumerate(sorted(identifiers.items())):
        if value is None:
            continue
        sets.append(f"#k{i} = :v{i}")
        names[f"#k{i}"] = key
        values[f":v{i}"] = {"S": str(value)}
    if not sets:
        return
    sets.append("#ua = :ua")
    names["#ua"] = "updated_at"
    values[":ua"] = {"S": _iso(_now())}
    dynamodb.update_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"TENANT#{tenant_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        # The record must already exist. Creating it here would let a later
        # step resurrect a tenant whose record was deliberately removed.
        ConditionExpression="attribute_exists(PK)")


def transition(tenant_id, to_state, reason=None):
    """Move the record's state, refusing transitions the design does not have.

    A conditional write on the CURRENT state, so two concurrent sagas cannot
    both move the same tenant. The one that loses gets a condition failure
    rather than a silently overwritten state -- which matters because the
    states carry commercial meaning: `live` starts billing.
    """
    current = (record(tenant_id) or {}).get("state")
    if current is None:
        raise ValueError(f"no registry record for {tenant_id}")
    allowed = STATES.get(current, set())
    if to_state not in allowed:
        raise ValueError(
            f"{tenant_id}: {current} -> {to_state} is not a transition design "
            f"13 defines. From {current} the only moves are "
            f"{sorted(allowed) or 'none, it is terminal'}.")
    dynamodb.update_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"TENANT#{tenant_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET #s = :new, state_reason = :r, updated_at = :t",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":new": {"S": to_state},
            ":old": {"S": current},
            ":r": {"S": str(reason or "")},
            ":t": {"S": _iso(_now())}},
        ConditionExpression="#s = :old")
    return to_state


# ---------------------------------------------------------------------------
# The steps, in design 13's order
# ---------------------------------------------------------------------------
#
# Each is a pair: `check` asks whether the effect already exists, `do` creates
# it. `check` runs before `do` (idempotency) and again after (confirmation),
# and only the second answer is believed.
#
# A step whose system does not exist yet raises StepNotImplemented, which
# BLOCKS. Returning a no-op would produce a tenant that finished provisioning
# without the thing that step was for.


def _not_built(what, tracked_as):
    def _do(ctx):
        raise StepNotImplemented(
            f"{what} is not built yet ({tracked_as}). The saga blocks here "
            "rather than continuing: a tenant that completes provisioning "
            "without this looks supported and is not.")
    return _do


def _check_partner(ctx):
    """design 13 step 0. Blocks, does not warn."""
    partner = ctx.get("partner")
    got = dynamodb.get_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"PARTNER#{partner}"}, "SK": {"S": "META"}})
    item = got.get("Item")
    if not item:
        return None
    state = (item.get("state") or {}).get("S")
    window = (item.get("notify_hours") or {}).get("S")
    # All three conditions, not just existence. design 13: "Verify the partner
    # exists, is active, and has a non-null notification window."
    if state != "active" or not window:
        return None
    return {"partner": partner, "partner_state": state,
            "notification_window": window}


def _check_registry(ctx):
    r = record(ctx["tenant_id"])
    return {"tenant_id": ctx["tenant_id"], "state": r["state"]} if r else None


def _do_registry(ctx):
    """design 13 step 1. The anchor. Nothing else starts until it exists."""
    now = _iso(_now())
    dynamodb.put_item(
        TableName=REGISTRY_TABLE,
        Item={
            "PK": {"S": f"TENANT#{ctx['tenant_id']}"},
            "SK": {"S": "META"},
            "record_type": {"S": "TENANT"},
            "tenant_id": {"S": ctx["tenant_id"]},
            "partner": {"S": ctx["partner"]},
            "client": {"S": ctx.get("client") or ctx["tenant_id"]},
            # Created in provisioning, never in provisioned. The state is a
            # claim about reality and at this point nothing exists.
            "state": {"S": "provisioning"},
            "created_at": {"S": now},
            "updated_at": {"S": now},
        },
        # Idempotent by construction: a second run collides and the check
        # above will have returned the existing record anyway.
        ConditionExpression="attribute_not_exists(PK)")
    return {"tenant_id": ctx["tenant_id"], "state": "provisioning"}


def _do_jira(ctx):
    """design 13 step 4. The onboarding task set and the recurring obligations.

    Imported lazily and deliberately. `jira_tenant` pulls in the Jira client,
    which builds a Secrets Manager client at import time; a module-level import
    would make the whole saga unimportable anywhere that credential is not
    reachable, including in the tests for steps that never touch Jira.

    What it does NOT create is a project. See jira_tenant's docstring: the
    platform credential cannot create one (B-108), so tenant separation is a
    label inside the shared ops project. That is a weaker boundary than design
    13 assumed and it is recorded as B-109 rather than papered over.
    """
    import jira_tenant

    result = jira_tenant.provision(ctx["tenant_id"])
    now = _iso(_now())
    dynamodb.update_item(
        TableName=REGISTRY_TABLE,
        Key={"PK": {"S": f"TENANT#{ctx['tenant_id']}"}, "SK": {"S": "META"}},
        UpdateExpression=("SET jira_tenant_label = :l, "
                          "jira_onboarding_key = :k, "
                          "jira_organization_id = :o, updated_at = :u"),
        ExpressionAttributeValues={
            ":l": {"S": result["tenant_label"]},
            ":k": {"S": result["onboarding_key"]},
            # The JSM organization is the only artefact in step 4 that
            # restricts anything (B-109). Recorded so the deprovisioning path
            # knows it exists -- it cannot delete it, and a teardown that does
            # not know about it would not say so.
            ":o": {"S": result["organization_id"]},
            ":u": {"S": now}},
        # The registry record must already exist -- step 1 created it. If it
        # does not, something removed it mid-saga and stamping a Jira key onto
        # a record that is not there would create a half-record describing a
        # tenant nobody declared.
        ConditionExpression="attribute_exists(PK)")
    return {"jira_tenant_label": result["tenant_label"],
            "jira_onboarding_key": result["onboarding_key"],
            "jira_organization_id": result["organization_id"]}


STEPS = [
    {
        "number": 0,
        "name": "partner-precondition",
        "check": _check_partner,
        # No `do`. design 13: "Blocks, does not warn." A partner that is not
        # active is not something provisioning creates -- it is a reason to
        # stop, and D-004 records an earlier script that warned instead.
        "do": None,
        "blocks_with": (
            "The partner must exist, be active, and have a non-null "
            "notification window before a tenant is provisioned under it. "
            "design 13 requires this to block rather than warn: a tenant "
            "under an inactive partner has nobody to notify."),
    },
    {
        "number": 1,
        "name": "registry-entry",
        "check": _check_registry,
        "do": _do_registry,
    },
    {
        "number": 2,
        "name": "truveon-tenant",
        # Before any account emits evidence, "or early evidence is lost rather
        # than buffered" -- design 13.
        "check": lambda ctx: ({"truveon_tenant_id": (record(ctx["tenant_id"]) or {}).get("truveon_tenant_id")}
                              if (record(ctx["tenant_id"]) or {}).get("truveon_tenant_id") else None),
        "do": _not_built("The Truveon tenant API", "B-088"),
    },
    {
        "number": 3,
        "name": "pagerduty-services",
        # Before alarms exist, "or they fire into the void".
        "check": lambda ctx: ({"pagerduty_service_id": (record(ctx["tenant_id"]) or {}).get("pagerduty_service_id")}
                              if (record(ctx["tenant_id"]) or {}).get("pagerduty_service_id") else None),
        "do": _not_built("Per-tenant PagerDuty provisioning", "prompt 12.2"),
    },
    {
        "number": 4,
        "name": "jira-tenant-artefacts",
        # Before reconciliation has a target. Confirmed by the onboarding
        # parent key rather than by the label: the label is derived from the
        # tenant id and would read as present before anything was created.
        "check": lambda ctx: ({"jira_onboarding_key": (record(ctx["tenant_id"]) or {}).get("jira_onboarding_key")}
                              if (record(ctx["tenant_id"]) or {}).get("jira_onboarding_key") else None),
        "do": _do_jira,
    },
    {
        "number": 5,
        "name": "aws-accounts-and-baseline",
        "check": lambda ctx: ({"account_ids": (record(ctx["tenant_id"]) or {}).get("account_ids")}
                              if (record(ctx["tenant_id"]) or {}).get("account_ids") else None),
        "do": _not_built("The vesting pipeline invocation", "phase 3"),
    },
    {
        "number": 6,
        "name": "instrumentation-and-schedules",
        "check": lambda ctx: ({"instrumented_at": (record(ctx["tenant_id"]) or {}).get("instrumented_at")}
                              if (record(ctx["tenant_id"]) or {}).get("instrumented_at") else None),
        "do": _not_built("Per-tenant instrumentation rollout", "phase 4 / 13"),
    },
    {
        "number": 7,
        "name": "cost-categories-and-budgets",
        "check": lambda ctx: ({"cost_category_synced_at": (record(ctx["tenant_id"]) or {}).get("cost_category_synced_at")}
                              if (record(ctx["tenant_id"]) or {}).get("cost_category_synced_at") else None),
        "do": _not_built("Per-tenant budget creation", "prompt 9.2 covers the categories; budgets do not exist"),
    },
    {
        "number": 8,
        "name": "verification-sweep",
        "check": lambda ctx: ({"verified_at": (record(ctx["tenant_id"]) or {}).get("verified_at")}
                              if (record(ctx["tenant_id"]) or {}).get("verified_at") else None),
        "do": _not_built("The per-tenant verification sweep", "design 13 'The verification sweep'"),
    },
    {
        "number": 9,
        "name": "go-live-gate",
        # Deliberately has no `do`, and never will.
        #
        # design 13 lists go-live approval as a BLOCKING human task owned by
        # Jamie. An automated path from step 8 to `live` would mean SLA
        # measurement, billing and on-call rotation all starting because a
        # sweep passed -- which is the whole gate lost to a green tick.
        "check": lambda ctx: ({"go_live_approved_by": (record(ctx["tenant_id"]) or {}).get("go_live_approved_by")}
                              if (record(ctx["tenant_id"]) or {}).get("go_live_approved_by") else None),
        "do": None,
        "blocks_with": (
            "Go-live is a human decision. design 13 makes it a blocking task "
            "owned by Jamie, and three things start on the transition to live: "
            "SLA measurement, billing, and the on-call rotation. Nothing here "
            "approves it automatically."),
    },
]


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _emit(tenant_id, name, cause, routing="review_queue", detail=None):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.provisioning",
        "DetailType": "provisioning-saga",
        "Detail": json.dumps({
            "record_type": "provisioning-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": tenant_id},
            "assessment": {"probable_cause": cause},
            "provisioning": detail or {},
        }, default=str),
    }])


def run_step(step, ctx):
    """One step. Check, act if needed, then confirm by checking again.

    Returns (status, identifiers, note) where status is one of:

        already   the effect existed; nothing was done
        done      the action ran and its effect was confirmed
        blocked   the step cannot proceed, and says why

    The second check is not belt and braces. An action that returns without
    error has told you the API accepted a request, which is a different claim
    from the effect existing -- and the gap between them is where a
    half-provisioned tenant comes from.
    """
    existing = step["check"](ctx)
    if existing:
        return "already", existing, None

    if step["do"] is None:
        # A step with no action is a precondition or a human gate. Either way
        # the saga stops here rather than proceeding past something that has
        # not happened.
        return "blocked", None, step.get("blocks_with", "manual step")

    try:
        step["do"](ctx)
    except StepNotImplemented as exc:
        return "blocked", None, str(exc)
    except Exception as exc:                       # noqa: BLE001
        return "blocked", None, (
            f"{type(exc).__name__}: {exc}. The action failed, so its effect "
            "does not exist.")

    confirmed = step["check"](ctx)
    if not confirmed:
        # The worst outcome, and the one that must not be reported as success.
        # Something may have been created; nothing can confirm it.
        raise StepUnconfirmed(
            f"step {step['number']} ({step['name']}) ran without error and its "
            "effect could not be confirmed. Something may exist and nothing "
            "here knows what. This is not the same as the step failing, and "
            "must not be treated as it: a failure leaves nothing, this leaves "
            "an unknown.")
    return "done", confirmed, None


def run(event, context=None):
    """Advance one tenant as far as it can go.

    Resumable because it reads the registry rather than an execution history:
    every step's check asks what exists. A saga resumed a week later by a
    different person reaches the same conclusion as one resumed immediately.
    """
    tenant_id = (event or {}).get("tenant_id")
    partner = (event or {}).get("partner")
    if not tenant_id or not partner:
        raise ValueError("run needs tenant_id and partner")
    ctx = dict(event or {})

    progress, blocked_at = [], None
    for step in STEPS:
        try:
            status, ids, note = run_step(step, ctx)
        except StepUnconfirmed as exc:
            # Fails closed, and loudly. An unconfirmed step is the one case
            # where a human must look before anything else happens.
            blocked_at = {"number": step["number"], "name": step["name"],
                          "status": "unconfirmed", "note": str(exc)}
            progress.append(blocked_at)
            break

        progress.append({"number": step["number"], "name": step["name"],
                         "status": status, "note": note})

        if status == "blocked":
            blocked_at = progress[-1]
            break
        if ids:
            # Immediately, not batched. See write_back's docstring: the
            # deprovisioning path reads this to know what to remove.
            try:
                write_back(tenant_id, **{k: v for k, v in ids.items()
                                         if k not in ("state", "tenant_id")})
            except Exception:                      # noqa: BLE001
                # Only after step 1; before it there is no record to write to.
                pass

    current = (record(tenant_id) or {}).get("state")
    completed = [p for p in progress if p["status"] in ("already", "done")]
    all_done = len(completed) == len(STEPS)

    # State follows evidence, not optimism.
    #
    # Reaching the end of the steps does NOT make a tenant live: step 9 is a
    # human gate and blocks by design, so `all_done` here means somebody has
    # already recorded the approval.
    if blocked_at and current not in TERMINAL and current is not None:
        if blocked_at["status"] == "unconfirmed":
            transition(tenant_id, "blocked", blocked_at["note"][:400])
            current = "blocked"
            _emit(tenant_id, "platform-provisioning-unconfirmed",
                  blocked_at["note"], routing="pagerduty",
                  detail={"step": blocked_at})
        else:
            # An ordinary block is not a failure of the saga -- it is the saga
            # correctly declining to continue past something incomplete. The
            # record stays where it is, and somebody finishes the step.
            _emit(tenant_id, "platform-provisioning-halted",
                  f"Provisioning halted at step {blocked_at['number']} "
                  f"({blocked_at['name']}): {blocked_at['note']}",
                  routing="digest_and_jira", detail={"step": blocked_at})

    out = {
        "record_type": "provisioning-saga-run",
        "tenant_id": tenant_id,
        "ran_at": _iso(_now()),
        "state": current,
        "steps_complete": len(completed),
        "steps_total": len(STEPS),
        "halted_at": blocked_at,
        "progress": progress,
        # Explicit, because "steps_complete == steps_total" is the thing
        # somebody will read as "ready", and it is not: step 9 completing means
        # a human already approved, which is the only way it completes.
        "live_eligible": all_done,
    }
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"run": run}


def handler(event, context):
    return run(event, context)
