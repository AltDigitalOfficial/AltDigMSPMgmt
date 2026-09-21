"""Is each scheduled thing running, at roughly the rate it declared?

Both directions, and the quiet one matters more
-------------------------------------------------
B-116 was a component running at a gross multiple of normal. The opposite —
something that should be running and is not — is the failure this codebase
keeps rediscovering, and it is worse, because a runaway at least leaves a
trace. A sweep that stops produces nothing: no error, no log line, no metric.
It looks exactly like an estate with nothing to report.

That is B-086 (a reverse sweep that swept zero accounts and reported a clean
estate), B-089 (a healthy pipeline that collected zero), B-092 (a validator
that validated nothing and reported all valid), B-100 (a detector reading a
log group that would never exist). Every one of them was a thing not running,
reporting success.

design 06a already states the principle, for telemetry streams:

    "CloudWatch Logs does not alarm on absence natively. Absence is only
    detectable against a DECLARED EXPECTATION."

This generalises that from telemetry to the platform's own scheduled work.

The expectation is the rule, not a list beside it
---------------------------------------------------
The obvious implementation keeps a table of "component X should run hourly".
It is wrong for the reason this codebase has hit repeatedly: two copies of one
fact drift, and the drift is invisible until the moment the check matters.
06a makes the same argument about `resource_types_no_alarms` — "a second list
in code would be the same two-copies problem".

So nothing is declared here. Every EventBridge rule carrying a
`ScheduleExpression` already states its own cadence, authoritatively, in the
deployed infrastructure. This reads that and compares it against
`TriggeredRules` for the same rule. A rule added next year is covered without
anybody editing this file; a rule whose schedule changes is re-derived on the
next sweep.

The window is per-rule, not global
------------------------------------
A single one-hour window cannot evaluate a `rate(1 day)` rule — the expected
count is 0.04, and both 0 and 1 are consistent with it. So each rule is
evaluated over at least three of its own intervals, which is the shortest
window in which "it did not run" is distinguishable from "it is not due yet".

That is why a daily rule is reported against three days of history rather than
being quietly skipped, and why a two-minute rule is not evaluated over three
days, which would hide a six-hour outage inside an acceptable average.

What it will not do
---------------------
`cron()` schedules are NOT evaluated. Computing the expected firing count of an
arbitrary cron expression needs a real cron library, which is not available in
this runtime without adding a dependency, and an approximation would be wrong
in the direction that matters: a weekly cron would read as six days of silence.

They are reported as `not_evaluated` with the reason, and counted in the
summary. A gap that says so is recoverable; a gap that returns "fine" is the
thing this module exists to prevent. B-118.
"""
import datetime
import json
import os
import re

import boto3

events_client = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")

# Jamie's framing: "a gross multiple of normal", and "some abnormal fraction of
# normal for things that should be running but aren't".
#
# Generous on both sides on purpose. This is not a performance monitor; it is
# looking for a component that has stopped or is looping, and both of those are
# order-of-magnitude events. A tight band would produce a finding every time a
# scheduled run landed either side of a window boundary, and a check that cries
# wolf is a check somebody mutes.
OVER_MULTIPLE = float(os.environ.get("CADENCE_OVER_MULTIPLE", "3.0"))
UNDER_FRACTION = float(os.environ.get("CADENCE_UNDER_FRACTION", "0.5"))

# At least this many of a rule's own intervals, so "did not run" is
# distinguishable from "not due yet".
#
# This is also what makes the ratio tests below safe without a minimum-volume
# guard. Because the window is scaled to the rule, `expected` is never less
# than MIN_INTERVALS -- three, for any rate() schedule -- so a ratio is always
# computed over enough firings to mean something. A fixed window would have
# needed that guard; a scaled one removes the need for it.
#
# An earlier version carried a MIN_EXPECTED constant and an `ok_low_volume`
# verdict for exactly that case. Writing the test showed the branch could not
# be reached, and a branch that claims to handle a case it never sees implies
# a protection that does not exist.
MIN_INTERVALS = int(os.environ.get("CADENCE_MIN_INTERVALS", "3"))

_RATE = re.compile(r"^rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)$")

_UNIT_SECONDS = {
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def interval_seconds(schedule_expression):
    """Seconds between firings, or None if not derivable.

    Returns None for cron() deliberately rather than guessing. See the module
    docstring: an approximation here is wrong in the direction that raises a
    false alarm about a healthy weekly job, and a false alarm on this control
    is how the control gets ignored.
    """
    if not schedule_expression:
        return None
    match = _RATE.match(schedule_expression.strip())
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2)
    return count * _UNIT_SECONDS[unit]


def scheduled_rules():
    """Every scheduled rule on every bus, with its declared cadence.

    Walks all buses, not just the default one. A scheduled rule on a custom bus
    is still scheduled work, and looking only at the default bus is the kind of
    partial enumeration that reports a clean result over half an estate.
    """
    rules = []
    buses = []
    # Manual paging: list_event_buses has no paginator in botocore, and
    # get_paginator raises rather than degrading to a single call.
    token = None
    while True:
        kwargs = {"Limit": 100}
        if token:
            kwargs["NextToken"] = token
        resp = events_client.list_event_buses(**kwargs)
        buses.extend(b["Name"] for b in resp.get("EventBuses") or [])
        token = resp.get("NextToken")
        if not token:
            break
    if not buses:
        # Not "no rules". Refusing to conclude anything from an empty bus list
        # -- there is always at least a default bus, so an empty answer means
        # the call did not work.
        raise RuntimeError(
            "list_event_buses returned no buses at all. There is always a "
            "default bus, so this is a failed read rather than an empty "
            "estate, and no conclusion may be drawn from it.")

    for bus in buses:
        token = None
        while True:
            kwargs = {"EventBusName": bus, "Limit": 100}
            if token:
                kwargs["NextToken"] = token
            resp = events_client.list_rules(**kwargs)
            for rule in resp.get("Rules") or []:
                if rule.get("ScheduleExpression"):
                    rules.append({
                        "name": rule["Name"],
                        "bus": bus,
                        "schedule": rule["ScheduleExpression"],
                        "state": rule.get("State"),
                    })
            token = resp.get("NextToken")
            if not token:
                break
    return rules


def triggered_series(rule_name, start, end, period):
    """Firings in the window as (total, first_seen).

    `first_seen` exists to solve a problem found by running this against a rule
    created four minutes earlier: it reported `under`, correctly by arithmetic
    and uselessly in practice. A rule younger than its own evaluation window
    cannot have fired as many times as the window expects, and reporting that
    as a fault would produce a finding for every rule on the day it is
    deployed -- which is the fastest way to teach somebody to ignore this.

    There is no creation timestamp on an EventBridge rule, so the first
    datapoint is the proxy: whatever else is true, the rule was not firing
    before CloudWatch first saw it fire.

    Returns (None, None) when there are no datapoints at all, which is NOT
    zero. A rule that has never fired publishes no metric, so the two are
    reported separately: zero is "it should have and did not", absent is
    "there is no evidence either way".
    """
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/Events",
        MetricName="TriggeredRules",
        Dimensions=[{"Name": "RuleName", "Value": rule_name}],
        StartTime=start, EndTime=end,
        Period=period,
        Statistics=["Sum"])
    points = [p for p in (resp.get("Datapoints") or []) if p.get("Sum")]
    if not points:
        return None, None
    return sum(p["Sum"] for p in points), min(p["Timestamp"] for p in points)


def assess(rule, now=None):
    """One rule: expected, observed, and a verdict with its reasoning."""
    now = now or _now()
    interval = interval_seconds(rule["schedule"])

    if rule.get("state") == "DISABLED":
        # A disabled rule is not a fault. It is also not healthy, and saying
        # "ok" would make a deliberately-disabled component indistinguishable
        # from a running one -- including one somebody disabled to stop a
        # runaway and then forgot.
        return {**rule, "verdict": "disabled", "expected": None,
                "observed": None,
                "why": "the rule is DISABLED. Nothing is wrong with it and "
                       "nothing is running it. If this was a temporary "
                       "measure, it has outlived the incident."}

    if interval is None:
        return {**rule, "verdict": "not_evaluated", "expected": None,
                "observed": None,
                "why": f"cadence not derivable from '{rule['schedule']}'. "
                       "cron() expressions need a real cron library to expand "
                       "and an approximation would report a healthy weekly job "
                       "as six days of silence (B-118)."}

    window_seconds = max(interval * MIN_INTERVALS, 3600)
    start = now - datetime.timedelta(seconds=window_seconds)
    # Bucket at the rule's own interval so the first firing is locatable, not
    # averaged into one number covering the whole window.
    observed, first_seen = triggered_series(
        rule["name"], start, now, max(60, min(interval, 86400)))

    if first_seen is not None:
        # Measure from when this rule was first seen firing, never from before
        # it existed. See triggered_series.
        effective_start = max(start, first_seen - datetime.timedelta(
            seconds=interval))
        effective_seconds = (now - effective_start).total_seconds()
    else:
        effective_seconds = window_seconds

    expected = effective_seconds / interval

    base = {**rule, "window_seconds": window_seconds,
            "measured_over_seconds": int(effective_seconds),
            "expected": round(expected, 2), "observed": observed}

    if observed is not None and expected < MIN_INTERVALS:
        # Too young to judge. Distinct from healthy: nothing has been
        # established about this rule yet, and saying "ok" would claim it had.
        return {**base, "verdict": "insufficient_history",
                "why": f"first seen firing {int(effective_seconds // 60)} "
                       "minutes ago, which is less than the "
                       f"{MIN_INTERVALS} intervals needed to judge cadence. "
                       "Not a fault and not yet a clean bill of health -- "
                       "re-evaluated on the next sweep."}

    if observed is None:
        return {**base, "verdict": "no_metric",
                "why": "CloudWatch has no TriggeredRules datapoints for this "
                       "rule in the window. That is not zero firings -- a rule "
                       "that has never fired publishes nothing at all -- so "
                       "this is an absence of evidence, not evidence of "
                       "absence. If the rule was created recently this is "
                       "expected; if it was not, it has never run."}

    if observed == 0:
        return {**base, "verdict": "silent",
                "why": f"expected about {expected:.0f} firings in "
                       f"{window_seconds // 3600}h and observed none. The "
                       "schedule exists and is enabled, so something is "
                       "preventing it from firing."}

    if observed > expected * OVER_MULTIPLE:
        return {**base, "verdict": "over",
                "why": f"fired {observed:.0f} times against about "
                       f"{expected:.0f} expected, more than {OVER_MULTIPLE}x. "
                       "Either the schedule was changed without this being "
                       "re-derived, or something is triggering the rule "
                       "outside its schedule."}

    if observed < expected * UNDER_FRACTION:
        return {**base, "verdict": "under",
                "why": f"fired {observed:.0f} times against about "
                       f"{expected:.0f} expected, under {UNDER_FRACTION:.0%}. "
                       "It is running intermittently, which is the state most "
                       "likely to be mistaken for working."}

    return {**base, "verdict": "ok",
            "why": f"fired {observed:.0f} times against about "
                   f"{expected:.0f} expected."}


def _emit(findings, summary):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.alerting",
        "DetailType": "cadence-finding",
        "Detail": json.dumps({
            "record_type": "cadence-finding",
            "recorded_at": _now().isoformat(),
            "current_state": "ALARM",
            # Not a page. A scheduled job that missed a window needs looking
            # at today, not at 3am -- and the account-wide runaway alarms
            # cover the case that genuinely cannot wait.
            "routing": "digest_and_jira",
            "alarm": {"name": "platform-cadence-anomaly",
                      "resource_id": ",".join(f["name"] for f in findings)},
            "assessment": {
                "probable_cause": f"{len(findings)} scheduled component(s) "
                                  "are not running at their declared cadence."},
            "cadence": {"findings": findings, "summary": summary},
        }, default=str),
    }])


def handler(event, context=None):
    """Sweep every scheduled rule and report both directions."""
    now = _now()
    rules = scheduled_rules()

    results = [assess(rule, now) for rule in rules]
    by_verdict = {}
    for result in results:
        by_verdict.setdefault(result["verdict"], []).append(result["name"])

    findings = [r for r in results
                if r["verdict"] in ("silent", "under", "over", "no_metric")]

    summary = {
        # What was EXAMINED, always, and first. A sweep reporting "0 findings"
        # without "of N rules" is the exact shape it exists to detect.
        "rules_scheduled": len(rules),
        "rules_evaluated": sum(
            1 for r in results
            if r["verdict"] not in ("not_evaluated", "disabled")),
        "not_evaluated": by_verdict.get("not_evaluated", []),
        "disabled": by_verdict.get("disabled", []),
        "findings": len(findings),
        "by_verdict": {k: len(v) for k, v in by_verdict.items()},
    }

    if findings:
        _emit(findings, summary)

    # The dead man's switch. This sweep is itself a scheduled component, and
    # nothing above would notice if it stopped -- it would simply stop
    # reporting, which reads identically to reporting nothing wrong.
    #
    # So it publishes a heartbeat on every run, and an alarm on THIS metric
    # with TreatMissingData=breaching fires when the metric stops arriving.
    # That is the one alarm in the platform that fires on absence rather than
    # on a value.
    cloudwatch.put_metric_data(
        Namespace="Platform/Cadence",
        MetricData=[{
            "MetricName": "SweepCompleted",
            "Value": 1,
            "Unit": "Count",
            "Timestamp": now,
        }, {
            "MetricName": "RulesEvaluated",
            "Value": summary["rules_evaluated"],
            "Unit": "Count",
            "Timestamp": now,
        }])

    out = {"record_type": "cadence-sweep",
           "swept_at": now.isoformat(),
           **summary,
           "results": results}
    print(json.dumps(out, default=str))
    return out
