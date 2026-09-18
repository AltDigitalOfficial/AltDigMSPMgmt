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
import traceback

import boto3

import actions
import rails

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")
CHANGE_LOG_GROUP = os.environ.get("CHANGE_LOG_GROUP", "/aws/platform/change-records")
DOSSIER_LOG_GROUP = os.environ.get("DOSSIER_LOG_GROUP", "/aws/platform/incident-dossiers")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
# Platform gaps go here instead of waking somebody a second time about an
# incident their pager is already holding. Falls back to the high-urgency topic
# if unset, because losing the message entirely is the worse failure.
LOW_URGENCY_TOPIC_ARN = os.environ.get("LOW_URGENCY_TOPIC_ARN", "")

# When forwarding is configured the dossier goes to EventBridge and the router
# pages with it; otherwise it goes to SNS as before.
#
# Deliberately either/or rather than both. The two paths reach PagerDuty through
# different integrations, so sending both raises two incidents about one outage
# with no relationship between them — the exact duplication the router's dedup
# key exists to prevent, reintroduced one layer up.
ESCALATION_VIA_BUS = os.environ.get("ESCALATION_VIA_BUS", "") == "true"
ENVIRONMENT_TIER = os.environ.get("ENVIRONMENT_TIER", "dev")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")

# Settle periods are honoured, but capped. A Lambda that sleeps for the full
# 600s of a scale-out settle costs 600s of Lambda time and risks the timeout.
# Above the cap the engine stops and reports "awaiting settle" rather than
# blocking — the scheduled sweep picks the incident back up. Sleeping through
# it would be simpler and would turn one slow remediation into a timeout with
# no dossier.
MAX_INLINE_SETTLE = int(os.environ.get("MAX_INLINE_SETTLE", "120"))
# "tried this 3x, still having problems, page out" (Jamie, 2026-09-18). Applies
# only to a revalidation the engine could not READ — an alarm that is genuinely
# still breaching after its settle period is a verdict on the first attempt and
# pages immediately. Retrying a known failure would just delay the page.
REVALIDATION_ATTEMPTS = int(os.environ.get("REVALIDATION_ATTEMPTS", "3"))
RETRY_SETTLE_SECONDS = int(os.environ.get("RETRY_SETTLE_SECONDS", "120"))

cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")
sns = boto3.client("sns")
events = boto3.client("events")

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
                        "would risk a Lambda timeout and lose the dossier. Parked "
                        "for the sweep, which revalidates once it has settled.",
                "at": _now().isoformat(),
            })
            # Park it. Until this existed the branch above only *said* a sweep
            # would revalidate; nothing did, and because almost every action
            # settles above the cap, that made "act and never look again" the
            # platform's ordinary behaviour.
            due = rails.record_pending(ctx, action, stage_no, settle, dossier)
            dossier["actions_taken"][-1]["revalidate_after"] = due.isoformat()
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
    dossier["assessment"] = assess(dossier, state)
    dossier["next_steps"] = next_steps(ctx, dossier, state)
    _write(DOSSIER_LOG_GROUP, dossier)
    return dossier


# Assessments for states where the finding IS the state. Without these, assess()
# fell through to "no recent changes found and no remediation resolved it" — a
# sentence about a resource, printed at the top of a dossier whose subject is
# the platform. Confirmed by reading a live bad-event record.
STATE_ASSESSMENTS = {
    "bad-event": {
        "probable_cause": "the response engine was invoked with an unusable event",
        "confidence": "certain",
        "evidence": "The event lacked one or more of alarm_name, alarm_id, "
                    "resource_type, resource_id. Nothing was gathered and nothing "
                    "was attempted, so there is no finding about the resource — "
                    "the finding is that the invocation was malformed.",
    },
    "no-runbook": {
        "probable_cause": "no runbook covers this alarm on this resource type",
        "confidence": "certain",
        "evidence": "The alarm paged through its own action, so the incident is "
                    "attended. What is absent is the dossier: no gather step ran, "
                    "so whoever answered started from a blank console.",
    },
    "engine-error": {
        "probable_cause": "the response engine raised",
        "confidence": "certain",
        "evidence": "A fault in the automation, not a diagnosis of the alarm. The "
                    "incident it was invoked for should be treated as unattended.",
    },
}


def assess(dossier, state=None):
    if state in STATE_ASSESSMENTS:
        return dict(STATE_ASSESSMENTS[state])
    if state is not None and state not in NEEDS_NO_HUMAN and state not in KNOWN_STATES:
        return _unknown_state_assessment(state)
    return _assess_incident(dossier)


def _assess_incident(dossier):
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
    repaired = [a for a in (dossier.get("actions_taken") or [])
                if a.get("result") == "succeeded"]
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
    blocked = [a for a in (dossier.get("actions_taken") or [])
               if a.get("result") == "blocked"]
    if blocked:
        b = blocked[-1]
        return [f"Automation was refused by the {b['rail']} rail: {b['reason']}",
                "That refusal is the finding. Resolve the underlying condition "
                "rather than clearing the rail."]
    if state == "awaiting-settle":
        return ["A remediation ran and needs time before its effect is measurable.",
                "It is parked in the remediation history table and the sweep "
                "revalidates it once settled. If it has not cleared by then you "
                "will be paged with this dossier plus what the recheck found.",
                "No action needed yet. If the sweep rule is disabled, nothing "
                "will revalidate this — check PlatformResponseSweepRule."]
    if state == "unresolved-after-settle":
        return ["A remediation ran, the settle period elapsed, and the alarm is "
                "still breaching. The action did not fix it.",
                "The dossier below carries both the original observations and a "
                "fresh gather taken at revalidation, so the two can be compared."]
    if state == "bad-event":
        return ["The response engine was invoked with an event it could not use. "
                "This is a platform fault, not a fault on the resource.",
                "The alarm that triggered it — if any — is unattended by "
                "automation, so treat the original alarm as uncovered.",
                "Check the EventBridge rule's input transformer."]
    if state == "no-runbook":
        return ["The alarm paged on its own, so the incident is covered. What is "
                "missing is the dossier: nothing was gathered.",
                "Add an entry to runbooks/catalog.yaml, even one that only "
                "gathers. Silence here is a platform gap, not an incident."]
    untried = []
    for stage in ("stage_2", "stage_3"):
        for spec in ((ctx.get("runbook") or {}).get(stage) or []):
            if not any(a.get("action") == spec["action"]
                       for a in (dossier.get("actions_taken") or [])):
                untried.append(f"{stage}: {spec['action']}")
    steps = ["Stages 1 to 3 did not resolve this. Human diagnosis required."]
    if untried:
        steps.append("Not attempted: " + ", ".join(untried))
    else:
        steps.append("Every permitted remediation for this alarm was attempted. "
                     "The catalog has nothing further, which is itself worth "
                     "reviewing.")
    return steps


# Every outcome the engine knows how to reach, and whether it needs a human.
#
# Jamie, 2026-09-18: "any if/elsif sort of model for decisioning must have an
# 'else' where the conditions fall all the way through to 'i don't know what to
# do with this - find a human'."
#
# Only two states mean the thing is actually fine. Everything else pages, and
# an UNRECOGNISED state pages too — that last case is the else of the else, and
# it is the one that matters, because a state nobody anticipated is exactly the
# circumstance where falling off the end is most likely and least survivable.
NEEDS_NO_HUMAN = {"resolved", "resolved-before-action", "resolved-after-settle"}

# States that page, and every one of them is deliberate. A state NOT in either
# set still pages — see terminal().
KNOWN_STATES = {
    "unresolved", "unresolved-after-settle", "blocked", "awaiting-settle",
    "no-runbook", "bad-event", "engine-error",
}

# Two of these page at low urgency because the incident itself is already
# covered and what is being reported is a gap in the PLATFORM:
#
#   no-runbook   the alarm paged on its own; the dossier is what is missing
#   awaiting-settle   an action ran and the sweep will revalidate it shortly
#
# Sending either at high urgency would wake somebody for a second time about an
# incident they are already holding, and the reliable result of that is a
# filtered notification rule — which would then swallow the real pages too.
LOW_URGENCY_STATES = {"no-runbook", "awaiting-settle"}


def _unknown_state_assessment(state):
    return {
        "probable_cause": "unknown engine state",
        "confidence": "none",
        "evidence": f"The response engine finished in state '{state}', which it "
                    "does not recognise. That is a defect in the engine rather "
                    "than a diagnosis of the alarm. Paging because an "
                    "unrecognised outcome must never be treated as a resolved one.",
    }


def terminal(ctx, dossier, state, extra=None):
    """The single exit. Assembles the dossier and pages unless the state is fine.

    Written as one gate rather than an escalate() call at each return, because
    the failure this prevents is a path that FORGETS to escalate — and the
    forgetting is invisible: the code returns a sensible-looking dict and the
    incident is simply never mentioned to anyone.

    Three paths were doing exactly that before this existed: a malformed event,
    a missing runbook, and awaiting-settle.
    """
    known = state in NEEDS_NO_HUMAN or state in KNOWN_STATES

    # The else of the else. A state this function does not recognise is not
    # assumed benign — it is escalated, and the dossier says why, because the
    # alternative is an engine that silently drops outcomes its author did not
    # think of. assess() handles the wording; deciding it here as well meant
    # setting it either side of assemble(), which persists the dossier and
    # overwrites the field.
    dossier = assemble(ctx, dossier, state)

    result = {"status": state, "alarm": ctx.get("alarm_name"),
              "actions": len(dossier.get("actions_taken") or [])}
    if extra:
        result.update(extra)

    if state not in NEEDS_NO_HUMAN:
        dossier["escalation"] = escalate(ctx, dossier, state)
        result["paged"] = dossier["escalation"].get("paged", False)
        result["urgency_requested"] = dossier["escalation"].get("urgency_requested")

    print(json.dumps(result))
    return result


SUBJECTS = {
    "unresolved": "automation did not resolve",
    "unresolved-after-settle": "remediation ran, alarm still breaching",
    "blocked": "automation refused by a safety rail",
    "awaiting-settle": "remediation ran, awaiting revalidation",
    "no-runbook": "no runbook — alarm handled without a dossier",
    "bad-event": "response engine received an unusable event",
    "engine-error": "response engine failed",
}


def escalate(ctx, dossier, state="unresolved"):
    """Stage 4. Page, with everything stages 1-3 learned attached."""
    low = state in LOW_URGENCY_STATES
    topic = (LOW_URGENCY_TOPIC_ARN or ALERT_TOPIC_ARN) if low else ALERT_TOPIC_ARN
    urgency = "low" if low else "high"
    if not topic and not ESCALATION_VIA_BUS:
        # Reported, not swallowed. A page that could not be sent is the one
        # failure that must never look like a page that was.
        print(json.dumps({"ESCALATION_UNDELIVERABLE": {
            "state": state, "alarm": ctx.get("alarm_name"),
            "reason": "no alert topic configured"}}))
        return {"paged": False, "urgency_requested": urgency,
                "reason": "no alert topic configured"}

    # B-027 will rework this into something readable at a glance. Until then the
    # state is in the subject, because "did not resolve" and "still breaching
    # after we changed something" call for different first moves.
    what = SUBJECTS.get(state, f"unrecognised engine state '{state}'")
    subject = f"[{ENVIRONMENT_TIER}] {ctx.get('alarm_name') or 'response-engine'} — {what}"
    if ESCALATION_VIA_BUS:
        # To the account's DEFAULT bus. baseline/70-event-forwarding.yaml
        # carries it to the central bus, and the router pages with the whole
        # dossier attached under the same dedup key as the raw alarm — so the
        # page that went out when the alarm fired is updated in place rather
        # than a second incident being raised about the same outage.
        events.put_events(Entries=[{
            "Source": "platform.response",
            "DetailType": "Platform Incident Dossier",
            "Detail": json.dumps(dossier, default=str),
            # The alarm name, so a console reader can get from the event to the
            # thing it is about without parsing the detail.
            "Resources": [a for a in [ctx.get("alarm_name")] if a],
        }])
        return {"paged": True, "via": "eventbridge", "subject": subject[:100],
                "urgency_requested": urgency}

    sns.publish(
        TopicArn=topic,
        Subject=subject[:100],
        Message=json.dumps(dossier, indent=2, default=str),
    )
    # Reported as "requested" rather than "urgency": in dev and test
    # ALERT_TOPIC_ARN is already the low-urgency topic by tier, so an engine
    # that asked for high urgency still lands on the low topic. Calling the
    # request an urgency made a live dev dossier read `urgency: high` while the
    # message went to platform-alerts-low.
    return {"paged": True, "via": "sns", "topic": topic,
            "urgency_requested": urgency}


# Keys worth seeing when an event is rejected. Everything else is reported by
# NAME ONLY: a malformed event is an unknown shape by definition, and echoing an
# unknown shape into a log group is how sensitive values end up somewhere they
# were never meant to be retained.
_EVENT_SAFE_KEYS = ("alarm_name", "alarm_id", "resource_type", "resource_id",
                    "mode", "source", "detail-type", "fired_at")


def _redact(event):
    safe = {k: event.get(k) for k in _EVENT_SAFE_KEYS if k in event}
    other = sorted(k for k in event if k not in _EVENT_SAFE_KEYS)
    if other:
        safe["_other_keys_present"] = other
    return safe


def run_sweep():
    """Revalidate remediations whose settle period has elapsed.

    The counterpart to record_pending(). Without this, awaiting-settle means
    "acted and never looked again", and because almost every action settles
    above the inline cap that was the platform's usual behaviour.
    """
    due = rails.due_revalidations()
    verdicts = []

    for item in due:
        ctx = {"alarm_name": item["alarm_name"], "alarm_id": item["alarm_id"],
               "resource_type": item["resource_type"],
               "resource_id": item["resource_id"],
               "runbook": ((CATALOG.get("runbooks") or {})
                           .get(item["resource_type"]) or {}).get(item["alarm_id"]) or {}}

        still = alarm_is_breaching(item["alarm_name"])

        if still is None:
            # The alarm could not be read. Not a verdict — retrying is right,
            # but only so far. Jamie, 2026-09-18: "tried this 3x, still having
            # problems, page out."
            if item["attempt"] < REVALIDATION_ATTEMPTS:
                rails.clear_pending(item)
                rails.record_pending(ctx, item["action"], item["stage"],
                                     RETRY_SETTLE_SECONDS,
                                     {"actions_taken": item["actions_taken"]},
                                     attempt=item["attempt"] + 1)
                verdicts.append({"alarm": item["alarm_name"], "verdict": "retry",
                                 "attempt": item["attempt"] + 1})
                continue
            state = "unresolved-after-settle"
        elif still is False:
            state = "resolved-after-settle"
        else:
            state = "unresolved-after-settle"

        dossier = {
            "record_type": "incident-dossier",
            "recorded_at": _now().isoformat(),
            "alarm": {"name": item["alarm_name"], "alarm_id": item["alarm_id"],
                      "resource_type": item["resource_type"],
                      "resource_id": item["resource_id"],
                      "account_id": ACCOUNT_ID,
                      "environment_tier": ENVIRONMENT_TIER},
            "context": {}, "observations": {},
            "actions_taken": item["actions_taken"],
            "revalidations": [{
                "after_stage": item["stage"], "after_action": item["action"],
                "settled_seconds": "elapsed", "attempt": item["attempt"],
                "still_breaching": still, "at": _now().isoformat(),
            }],
        }

        if state != "resolved-after-settle":
            # Re-gather rather than reuse. The point of comparing a fresh read
            # against the original is that the difference is the evidence — the
            # stale copy would say only what was true before the action ran.
            for g in (list(CATALOG.get("default_gather") or [])
                      + list(ctx["runbook"].get("gather") or [])):
                try:
                    dossier["observations"][g] = actions.gather(g, ctx)
                except Exception as exc:
                    dossier["observations"][g] = {"error": str(exc)}
            dossier["context"]["recent_changes"] =                 (dossier["observations"].get("recent_changes") or {}).get("changes") or []

        # Cleared BEFORE the verdict is published. Publishing first and failing
        # to clear would leave the item due forever and page on every sweep.
        rails.clear_pending(item)
        terminal(ctx, dossier, state)
        verdicts.append({"alarm": item["alarm_name"], "verdict": state})

    out = {"status": "sweep", "due": len(due), "verdicts": verdicts}
    print(json.dumps(out))
    return out


def handler(event, context):
    """Dispatch, and make sure nothing leaves this function silently.

    The outer except is the last else. A Lambda exception does raise the
    function's Errors alarm, but that alarm says only "the engine broke" — it
    carries no alarm name, no resource and no dossier, so whoever answers it
    cannot tell which incident went unattended. This escalates with that
    context attached and then re-raises, so both signals fire.
    """
    sweep = (event or {}).get("mode") == "sweep"
    try:
        if sweep:
            return run_sweep()
        return _handle_alarm(event or {})
    except Exception as exc:
        if sweep:
            # The sweep runs every two minutes. Escalating its own failure the
            # way an alarm invocation escalates would publish thirty messages an
            # hour for one broken thing, and the reliable outcome of that is a
            # muted topic — which would then swallow the incident pages too.
            #
            # Caught during the first deploy of this rule: the GSI was still
            # CREATING, every query failed, and the else-rule escalation fired
            # on each invocation.
            #
            # So a sweep failure re-raises only. That raises the function's
            # Errors alarm, which deduplicates by design, and
            # platform-response-sweep-stalled catches a sweep that has stopped
            # producing verdicts. Both say "the sweep is broken" once rather
            # than continuously. No incident is lost: parked items stay parked
            # and are revalidated when the sweep recovers.
            print(json.dumps({"SWEEP_FAILED": {
                "error": f"{type(exc).__name__}: {exc}",
                "note": "not escalated — see the Errors and sweep-stalled alarms",
                "traceback": traceback.format_exc()[-2000:]}}))
            raise
        try:
            terminal({"alarm_name": (event or {}).get("alarm_name")},
                     {"record_type": "incident-dossier",
                      "recorded_at": _now().isoformat(),
                      "observations": {"received_event": _redact(event or {}),
                                       "error": f"{type(exc).__name__}: {exc}",
                                       "traceback": traceback.format_exc()[-4000:]},
                      "actions_taken": [], "revalidations": []},
                     "engine-error")
        except Exception:
            # Escalation itself failed. Nothing further can be done from here;
            # the re-raise below is the only remaining signal.
            print(json.dumps({"FATAL_escalation_failed":
                              traceback.format_exc()[-2000:]}))
        raise


def _handle_alarm(event):
    alarm_name = event.get("alarm_name") or (event.get("detail") or {}).get("alarmName")
    resource_type = event.get("resource_type")
    resource_id = event.get("resource_id")
    alarm_id = event.get("alarm_id")

    if not all((alarm_name, resource_type, resource_id, alarm_id)):
        # Used to return a dict and stop. EventBridge invokes asynchronously, so
        # that return value went nowhere: an alarm whose rule was misconfigured
        # produced no dossier, no log line and no page, and looked from the
        # console exactly like an alarm the engine had handled.
        return terminal(
            {"alarm_name": alarm_name},
            {"record_type": "incident-dossier",
             "recorded_at": _now().isoformat(),
             "alarm": {"name": alarm_name, "alarm_id": alarm_id,
                       "resource_type": resource_type, "resource_id": resource_id,
                       "account_id": ACCOUNT_ID,
                       "environment_tier": ENVIRONMENT_TIER},
             "observations": {"received_event": _redact(event)},
             "actions_taken": [], "revalidations": []},
            "bad-event",
            {"reason": "event must carry alarm_name, alarm_id, resource_type and "
                       "resource_id. The engine does not guess: acting on the "
                       "wrong resource is worse than not acting."})

    runbook = ((CATALOG.get("runbooks") or {}).get(resource_type) or {}).get(alarm_id)
    if runbook is None:
        # RECORDED, not just returned.
        #
        # This path used to return a dict and do nothing else — and EventBridge
        # invokes asynchronously, so the return value went nowhere. An alarm
        # with no runbook produced total silence: no log line, no metric, no
        # page. The incident was still handled, because the alarm's own action
        # pages independently of this engine, but nobody ever learned that the
        # platform had nothing to offer for it.
        #
        # That is the same silent-gap failure the instrumentation layer's
        # exception log exists to prevent, so it uses the same mechanism: a
        # record a metric filter can count and an alarm can fire on.
        record = {
            "record_type": "response-gap",
            "recorded_at": _now().isoformat(),
            "reason": "no-runbook",
            "alarm_name": alarm_name,
            "alarm_id": alarm_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "account_id": ACCOUNT_ID,
            "environment_tier": ENVIRONMENT_TIER,
            "message": "An alarm fired with no runbook for this (alarm x "
                       "resource) pair. The alarm paged on its own, so the "
                       "incident was seen — but no gathering happened, so "
                       "whoever answered it started from a blank console. Add "
                       "an entry to runbooks/catalog.yaml, even one that only "
                       "gathers.",
        }
        try:
            _write(DOSSIER_LOG_GROUP, record)
        except Exception as exc:
            # Re-raised rather than swallowed. If the gap cannot even be
            # recorded, a Lambda error is the only remaining signal, and the
            # Errors alarm on this function catches it.
            print(json.dumps({"FATAL_could_not_record_gap": record}))
            raise
        print(json.dumps(record))
        # Recorded AND escalated, at low urgency. Recording alone left the gap
        # discoverable only by someone who went looking, which in practice meant
        # during the next incident on the same alarm.
        return terminal(
            {"alarm_name": alarm_name, "alarm_id": alarm_id,
             "resource_type": resource_type, "resource_id": resource_id},
            {"record_type": "incident-dossier",
             "recorded_at": record["recorded_at"],
             "alarm": {"name": alarm_name, "alarm_id": alarm_id,
                       "resource_type": resource_type, "resource_id": resource_id,
                       "account_id": ACCOUNT_ID,
                       "environment_tier": ENVIRONMENT_TIER},
             "observations": {}, "actions_taken": [], "revalidations": []},
            "no-runbook", {"recorded": True})

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
        return terminal(ctx, dossier, "resolved-before-action", {
            "note": "The alarm cleared between firing and this running. Common "
                    "with flapping metrics. Acting on a resolved alarm is the "
                    "easiest way for automation to cause the incident it was "
                    "called for."})

    state = "unresolved"
    for stage_no, key in ((2, "stage_2"), (3, "stage_3")):
        result = run_stage(stage_no, runbook.get(key) or [], ctx, dossier)
        if result == "resolved":
            state = "resolved"
            break
        if result in ("blocked", "awaiting-settle"):
            state = result
            break

    return terminal(ctx, dossier, state)
