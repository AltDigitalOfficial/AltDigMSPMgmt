"""Four-stage response engine.

Prompt 5.1: "the framework handles staging, revalidation, safety rails and
dossier assembly generically."

So this file contains no knowledge of any particular alarm. It knows how to run
a stage, how to decide whether to run the next one, and how to write down what
happened. What to actually do lives in runbooks/catalog.yaml; what is permitted
lives in rails.py; how to do it lives in actions.py.

------------------------------------------------------------------------------
Revalidation is the whole control flow
------------------------------------------------------------------------------
Doc 07: "Each stage re-validates against the original alarm condition before
escalating. If stage 2 resolved it, stop and report — do not continue."

Read carefully that is two rules, not one:

  do not continue if it worked    obvious, and the cheap half
  re-validate before escalating   the expensive half, and the one that stops
                                  the engine escalating to a disruptive action
                                  because a metric had not refreshed yet

The second is why settle periods exist. Revalidating the instant an action
returns reports "did not fix" on every slow remediation, which would escalate
scale-out to task-cycling on a service that was recovering perfectly well.

------------------------------------------------------------------------------
A failed fix escalates the STAGE; it does not retry the action
------------------------------------------------------------------------------
Jamie, 2026-09-18. Clearing logs that did not drop disk usage will not drop it
on a second attempt, and retrying is how automation spends ten minutes failing
at something a human would have diagnosed in one.

So within an incident each action runs at most once. Repetition across
incidents is the circuit breaker's business, not the engine's.

------------------------------------------------------------------------------
The dossier is the product
------------------------------------------------------------------------------
Doc 07: "On success -> this is the RCA and the evidence record for Truveon. On
escalation -> this is the PagerDuty payload, so the human opens their laptop to
a dossier rather than a blank console."

Same structure either way, which is why it is assembled unconditionally rather
than only on the escalation path. An engine that builds the readout only when
it fails has no record of the times it worked — and those are the ones that
prove the automation is earning its place.
"""

import datetime
import json
import os
import time

import boto3

import actions
import rails

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")
CHANGE_LOG_GROUP = os.environ.get("CHANGE_LOG_GROUP", "/aws/platform/change-records")
DOSSIER_LOG_GROUP = os.environ.get("DOSSIER_LOG_GROUP", "/aws/platform/incident-dossiers")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
ENVIRONMENT_TIER = os.environ.get("ENVIRONMENT_TIER", "dev")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")

# Settle periods are honoured, but capped. A Lambda that sleeps for the full
# 600s of a scale-out settle costs 600s of Lambda time and risks the timeout.
# Above the cap the engine stops and reports "awaiting settle" rather than
# blocking — the scheduled sweep picks the incident back up. Sleeping through
# it would be simpler and would turn one slow remediation into a timeout with
# no dossier.
MAX_INLINE_SETTLE = int(os.environ.get("MAX_INLINE_SETTLE", "120"))

cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")
sns = boto3.client("sns")

with open(CATALOG_PATH, encoding="utf-8") as fh:
    CATALOG = json.load(fh)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _write(group, record):
    stream = _now().strftime("%Y/%m/%d")
    try:
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_log_events(
        logGroupName=group,
        logStreamName=stream,
        logEvents=[{"timestamp": int(_now().timestamp() * 1000),
                    "message": json.dumps(record)}],
    )


def change_record(alarm, resource_id, action, stage, outcome, detail):
    """06a: every_action_emits_change_record: true.

    Doc 07 says why, and it is not about auditing for its own sake:
    "automation must not become a route around change management." An engineer
    who restarts a service files a change. If automation does the same thing
    silently, the platform has built a way to change production without a
    record — and it would be the platform's own tooling that did it.

    Written BEFORE the reconciliation engine of phase 8 exists, so for now it
    lands in a log group. The shape is what matters: it carries who, what,
    when, why and the automated flag, which is what a reconciler will need.
    """
    record = {
        "record_type": "change-record",
        "recorded_at": _now().isoformat(),
        "automated": True,
        "account_id": ACCOUNT_ID,
        "environment_tier": ENVIRONMENT_TIER,
        "actor": "platform-response-automation",
        "trigger": {"alarm": alarm},
        "stage": stage,
        "action": action,
        "resource_id": resource_id,
        "outcome": outcome,
        "detail": detail,
    }
    _write(CHANGE_LOG_GROUP, record)
    return record


def alarm_is_breaching(alarm_name):
    """Re-read the alarm. This is the revalidation.

    Deliberately re-reads the ALARM rather than the metric. The alarm carries
    the threshold, the evaluation periods and the datapoints-to-alarm that 06a
    decided, and re-implementing that comparison here would be a second
    definition of "is this still bad" that could disagree with the first.
    """
    resp = cloudwatch.describe_alarms(AlarmNames=[alarm_name])
    found = resp.get("MetricAlarms") or []
    if not found:
        # The alarm vanished mid-incident. Treat as not breaching but say so —
        # it usually means drift detection has not caught up with someone
        # deleting it, which is its own problem.
        return None
    return found[0]["StateValue"] == "ALARM"


def settle_for(action):
    table = CATALOG.get("settle_seconds") or {}
    return table.get(action, table.get("default", 300))


def run_stage(stage_no, specs, ctx, dossier):
    """Run one stage. Returns 'resolved' | 'escalate' | 'blocked' | 'awaiting-settle'."""
    if not specs:
        return "escalate"

    for spec in specs:
        action = spec["action"]

        try:
            evidence = rails.preflight(
                stage_no, ctx["resource_type"], ctx["resource_id"], action, spec)
        except rails.Blocked as blocked:
            dossier["actions_taken"].append({
                "stage": stage_no, "action": action, "result": "blocked",
                "rail": blocked.rail, "reason": blocked.message,
                "at": _now().isoformat(),
            })
            rails.record_attempt(ctx["resource_id"], action, "blocked",
                                 {"rail": blocked.rail})
            change_record(ctx["alarm_name"], ctx["resource_id"], action,
                          stage_no, "blocked", {"rail": blocked.rail,
                                                "reason": blocked.message})
            # A blocked action escalates rather than trying the next one in the
            # stage. The rails that block are account-wide or resource-wide —
            # the kill switch, the rate limit, the breaker — so the next action
            # would be blocked for the same reason, and trying it just adds
            # noise to the dossier.
            return "blocked"

        started = _now()
        try:
            result = actions.run(action, ctx, spec.get("parameters") or {})
            outcome, detail = "succeeded", result
        except Exception as exc:
            outcome, detail = "failed", {"error": str(exc)}

        rails.record_attempt(ctx["resource_id"], action, outcome, detail)
        change_record(ctx["alarm_name"], ctx["resource_id"], action, stage_no,
                      outcome, {"evidence": evidence, "result": detail})
        dossier["actions_taken"].append({
            "stage": stage_no, "action": action, "result": outcome,
            "detail": detail, "preconditions": evidence,
            "at": started.isoformat(),
        })

        if outcome == "failed":
            # The action itself errored. No point waiting to revalidate
            # something that never ran.
            return "escalate"

        settle = settle_for(action)
        if settle > MAX_INLINE_SETTLE:
            dossier["actions_taken"].append({
                "stage": stage_no, "action": action, "result": "awaiting-settle",
                "settle_seconds": settle,
                "note": f"{action} needs {settle}s before its effect is visible in "
                        "the metric. The engine does not sleep through it — that "
                        "would risk a Lambda timeout and lose the dossier. The "
                        "scheduled sweep revalidates.",
                "at": _now().isoformat(),
            })
            return "awaiting-settle"

        time.sleep(settle)
        still = alarm_is_breaching(ctx["alarm_name"])
        dossier["revalidations"].append({
            "after_stage": stage_no, "after_action": action,
            "settled_seconds": settle,
            "still_breaching": still, "at": _now().isoformat(),
        })

        if still is False:
            return "resolved"

    # Every action in the stage ran and the alarm is still breaching.
    return "escalate"


def assemble(ctx, dossier, state):
    """Doc 07's incident readout. Same structure whether repaired or escalated."""
    dossier["current_state"] = state
    dossier["assessment"] = assess(dossier)
    dossier["next_steps"] = next_steps(ctx, dossier, state)
    _write(DOSSIER_LOG_GROUP, dossier)
    return dossier


def assess(dossier):
    """Probable cause, confidence, supporting evidence.

    Deliberately conservative. This is not a diagnosis engine — it reports what
    the gather stage found and how strongly it points somewhere. Overstating
    confidence here is worse than saying nothing, because the dossier is what a
    woken engineer reads first and a confident wrong answer sends them down the
    wrong path at 3am.
    """
    changes = (dossier.get("context") or {}).get("recent_changes") or []
    if changes:
        return {
            "probable_cause": "a recent change",
            "confidence": "moderate",
            "evidence": f"{len(changes)} write API call(s) against this resource in "
                        "the 30 minutes before the alarm. Doc 07: that question "
                        "resolves a large share of real incidents on its own.",
            "changes": changes[:10],
        }
    repaired = [a for a in dossier["actions_taken"] if a["result"] == "succeeded"]
    if repaired and dossier.get("current_state") == "resolved":
        return {
            "probable_cause": f"addressed by {repaired[-1]['action']}",
            "confidence": "low",
            "evidence": "the remediation was followed by the alarm clearing. That is "
                        "correlation — the underlying cause is unaddressed, and if "
                        "this recurs the circuit breaker will stop masking it.",
        }
    return {
        "probable_cause": "undetermined",
        "confidence": "none",
        "evidence": "no recent changes found and no remediation resolved it. The "
                    "gather output below is the whole of what is known.",
    }


def next_steps(ctx, dossier, state):
    if state == "resolved":
        return ["No action required. The change record is in "
                "/aws/platform/change-records."]
    blocked = [a for a in dossier["actions_taken"] if a["result"] == "blocked"]
    if blocked:
        b = blocked[-1]
        return [f"Automation was refused by the {b['rail']} rail: {b['reason']}",
                "That refusal is the finding. Resolve the underlying condition "
                "rather than clearing the rail."]
    if state == "awaiting-settle":
        return ["A remediation ran and needs time before its effect is measurable. "
                "The scheduled sweep will revalidate; no action needed yet."]
    untried = []
    for stage in ("stage_2", "stage_3"):
        for spec in (ctx["runbook"].get(stage) or []):
            if not any(a["action"] == spec["action"] for a in dossier["actions_taken"]):
                untried.append(f"{stage}: {spec['action']}")
    steps = ["Stages 1 to 3 did not resolve this. Human diagnosis required."]
    if untried:
        steps.append("Not attempted: " + ", ".join(untried))
    else:
        steps.append("Every permitted remediation for this alarm was attempted. "
                     "The catalog has nothing further, which is itself worth "
                     "reviewing.")
    return steps


def escalate(ctx, dossier):
    """Stage 4. Page, with everything stages 1-3 learned attached."""
    if not ALERT_TOPIC_ARN:
        return {"paged": False, "reason": "no alert topic configured"}
    subject = f"[{ENVIRONMENT_TIER}] {ctx['alarm_name']} — automation did not resolve"
    sns.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject=subject[:100],
        Message=json.dumps(dossier, indent=2, default=str),
    )
    return {"paged": True, "topic": ALERT_TOPIC_ARN}


def handler(event, context):
    alarm_name = event.get("alarm_name") or (event.get("detail") or {}).get("alarmName")
    resource_type = event.get("resource_type")
    resource_id = event.get("resource_id")
    alarm_id = event.get("alarm_id")

    if not all((alarm_name, resource_type, resource_id, alarm_id)):
        return {"status": "exception",
                "reason": "event must carry alarm_name, alarm_id, resource_type and "
                          "resource_id. The engine does not guess: acting on the "
                          "wrong resource is worse than not acting."}

    runbook = ((CATALOG.get("runbooks") or {}).get(resource_type) or {}).get(alarm_id)
    if runbook is None:
        return {"status": "no-runbook", "alarm_id": alarm_id,
                "resource_type": resource_type,
                "note": "No runbook for this (alarm x resource). Stage 1 gather is "
                        "still worth running; that is a catalog gap rather than an "
                        "error."}

    ctx = {"alarm_name": alarm_name, "alarm_id": alarm_id,
           "resource_type": resource_type, "resource_id": resource_id,
           "runbook": runbook}

    dossier = {
        "record_type": "incident-dossier",
        "recorded_at": _now().isoformat(),
        # Doc 07: "what fired, when, on what, in which account". All four, in
        # the first block, because this is the top of the PagerDuty payload and
        # B-027 exists because the alarm name alone answers roughly one of them.
        "alarm": {
            "name": alarm_name, "alarm_id": alarm_id,
            "resource_type": resource_type, "resource_id": resource_id,
            "account_id": ACCOUNT_ID, "environment_tier": ENVIRONMENT_TIER,
            "fired_at": event.get("fired_at") or _now().isoformat(),
        },
        "context": {},
        "observations": {},
        "actions_taken": [],
        "revalidations": [],
    }

    # Stage 1 always runs, always first, always read-only.
    gather = list(CATALOG.get("default_gather") or []) + list(runbook.get("gather") or [])
    for item in gather:
        try:
            dossier["observations"][item] = actions.gather(item, ctx)
        except Exception as exc:
            dossier["observations"][item] = {"error": str(exc)}
    dossier["context"]["recent_changes"] = \
        (dossier["observations"].get("recent_changes") or {}).get("changes") or []

    # If the alarm cleared on its own between firing and this running, stop.
    # Common with flapping metrics, and acting on a resolved alarm is the
    # easiest way for automation to cause the incident it was called for.
    if alarm_is_breaching(alarm_name) is False:
        # Same return SHAPE as every other path. An earlier version returned
        # the dossier here and the status envelope everywhere else, so a caller
        # got a different structure depending on which branch it took — and the
        # branch it took depended on a race between the alarm clearing and this
        # running, which is the worst possible thing for a return type to
        # depend on.
        assemble(ctx, dossier, "resolved-before-action")
        return {"status": "resolved-before-action", "alarm": alarm_name,
                "actions": 0,
                "note": "The alarm cleared between firing and this running. Common "
                        "with flapping metrics. Acting on a resolved alarm is the "
                        "easiest way for automation to cause the incident it was "
                        "called for."}

    state = "unresolved"
    for stage_no, key in ((2, "stage_2"), (3, "stage_3")):
        result = run_stage(stage_no, runbook.get(key) or [], ctx, dossier)
        if result == "resolved":
            state = "resolved"
            break
        if result in ("blocked", "awaiting-settle"):
            state = result
            break

    dossier = assemble(ctx, dossier, state)
    if state not in ("resolved", "awaiting-settle"):
        dossier["escalation"] = escalate(ctx, dossier)
    return {"status": state, "alarm": alarm_name,
            "actions": len(dossier["actions_taken"])}
