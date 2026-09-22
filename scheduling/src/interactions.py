"""What else a HOOP touches. Prompt 13.4, design 14 "Interaction with the rest
of the platform".

The conflicts that fail silently
----------------------------------
Six of design 14's eight interactions are behavioural: suppress an alarm,
pause a canary, excuse telemetry silence. They are wired where the behaviour
lives, and `WIRING` below records where, so that "which of the eight are
actually connected" is a question with a written answer rather than an
archaeology exercise.

Two are different in kind, and they are the two this module exists for:

    restore testing         must not collide with a turndown window
    patch and maintenance   must fall INSIDE the HOOP

Both fail by not happening. A maintenance window at 02:00 on an environment
scheduled 08:00-18:00 does not error, does not alarm and does not page -- the
instances are stopped, SSM has nobody to talk to, and the window closes having
patched nothing. Six months later the environment is behind on patching and
every individual run looks like it completed.

prompt 13.4: "a maintenance window outside the HOOP is a silent failure of
patch management. Detect it at configuration time."

Configuration time is the whole point. This is arithmetic over two declared
schedules and needs nothing to be running, so it can be answered at vesting --
before the first window has been missed rather than after the hundredth.
"""
import datetime

import hoop

# Where each of design 14's eight interactions is actually implemented.
#
# Written down because the alternative is a reader grepping for "hoop" across
# the repository and concluding from silence. An interaction that is NOT wired
# says so here, with what it would take -- an honest gap is recoverable and an
# assumed one is not.
WIRING = {
    "alarms": {
        "requirement": "Suppressed during scheduled down, or every turndown pages",
        "wired": "scheduling/src/state.py publishes the state; the gap "
                 "detector and alarm consumers read it",
        "status": "partial",
        "note": "Alarm actions are not yet disabled on turndown. Most alarms "
                "on a stopped instance simply go INSUFFICIENT_DATA and do not "
                "fire, so the exposure is narrower than it looks -- but any "
                "alarm with TreatMissingData=breaching will page. B-123.",
    },
    "canaries": {
        "requirement": "Paused during down, resumed and used as turnup validation",
        "wired": None,
        "status": "not wired",
        "note": "Pausing needs synthetics:UpdateCanary in the tenant account, "
                "and resume-as-validation needs to READ the canary result, "
                "which is the same cross-account gap as B-112. A canary "
                "running against a stopped environment fails every run and "
                "trains people to ignore canary alerts.",
    },
    "telemetry_gap": {
        "requirement": "Must distinguish scheduled down from an agent being killed",
        "wired": "telemetry/src/gap.py splits its stopped row on HOOP state",
        "status": "wired",
        "note": "design 14 calls this the one most likely to be missed. The "
                "table already had a stopped row that did not alarm, so a "
                "turndown was never a false positive -- but it treated EVERY "
                "stop as expected, so an instance stopped outside its HOOP "
                "was equally silent. Splitting the row fixes both directions.",
    },
    "restore_testing": {
        "requirement": "Must not collide with a turndown window; schedule inside the HOOP",
        "wired": "check_window_conflicts, below",
        "status": "wired",
    },
    "patching": {
        "requirement": "Must fall inside the HOOP, or patching silently never runs",
        "wired": "check_window_conflicts, below",
        "status": "wired",
    },
    "backup": {
        "requirement": "Continues regardless -- backup targets storage, not running compute",
        "wired": "nothing to do, and that is the correct answer",
        "status": "wired",
        "note": "Explicitly listed rather than omitted. The temptation is to "
                "pause backups with everything else, and it is wrong: a HOOP "
                "stops compute and leaves the volumes, so the data is still "
                "there and still needs backing up. An environment down for a "
                "fortnight with no backups is a fortnight of exposure.",
    },
    "change_reconciliation": {
        "requirement": "A deployment against a stopped environment is a "
                       "legitimate failure, not a finding",
        "wired": None,
        "status": "not wired",
        "note": "change/src/reconcile.py would need to read the HOOP state "
                "and classify a deployment failure during scheduled down as "
                "expected. Today it would report an unapproved-or-failed "
                "deployment, which is a finding about a person who did "
                "nothing wrong. B-123.",
    },
    "cost_reporting": {
        "requirement": "Actual vs expected HOOP hours, with auto-restart events called out",
        "wired": "hoop.sweep_auto_restarts emits the restart events",
        "status": "partial",
        "note": "The auto-restart half is wired and carries design 14's "
                "teardown recommendation. Actual-vs-expected hours needs the "
                "cost report to read the state history, and B-090 already "
                "records that the per-audience cost reports are unbuilt.",
    },
}


def _minutes(value):
    parsed = datetime.time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def _day_set(days):
    return {d.lower()[:3] for d in days or []}


def covered(hoop_config, days, start, end):
    """Is [start, end) on every one of `days` inside some HOOP window?

    Returns (covered, uncovered_days). A maintenance window spanning midnight
    is reported as uncovered rather than silently wrapped: the arithmetic is
    answerable, but a window that runs 23:00-01:00 against a HOOP that closes
    at 18:00 is a configuration nobody should be talked out of noticing.
    """
    windows = hoop_config.get("windows") or []
    if not windows:
        # No HOOP means nothing is ever scheduled down, so everything is
        # inside it. Not a conflict.
        return True, []

    want_start, want_end = _minutes(start), _minutes(end)
    if want_end <= want_start:
        return False, sorted(_day_set(days))

    uncovered = []
    for day in sorted(_day_set(days)):
        day_covered = False
        for window in windows:
            if day not in _day_set(window.get("days")):
                continue
            if (_minutes(window["start"]) <= want_start
                    and _minutes(window["end"]) >= want_end):
                day_covered = True
                break
        if not day_covered:
            uncovered.append(day)
    return not uncovered, uncovered


def check_window_conflicts(hoop_config, maintenance=None, restore_test=None):
    """Find schedules that fall outside the HOOP, at configuration time.

    Returns findings, each naming what would not happen. Deliberately not a
    boolean: "there is a conflict" is not actionable, and the person reading
    this needs to know which days and which window.
    """
    findings = []

    if not (hoop_config or {}).get("windows"):
        # Nothing is scheduled down, so nothing can be missed. Said explicitly
        # because "no findings" over an unscheduled environment and "no
        # findings" over a correctly-scheduled one are different facts.
        return [], {"checked": False,
                    "why": "this environment has no HOOP windows, so nothing "
                           "is ever scheduled down and no window can fall "
                           "outside it."}

    for name, schedule, consequence in (
        ("patch-maintenance", maintenance,
         "Patching will silently never run. The window opens, the instances "
         "are stopped, SSM has nobody to talk to, and it closes having "
         "patched nothing. Every individual run looks like it completed."),
        ("restore-test", restore_test,
         "The restore test will fail or be skipped, and a restore path that "
         "is never exercised is design 05's whole concern. In UAT this also "
         "breaches the tier's own requirement, since UAT is in restore-test "
         "scope because its data is real."),
    ):
        if not schedule:
            continue
        ok, uncovered = covered(hoop_config, schedule.get("days"),
                                schedule["start"], schedule["end"])
        if not ok:
            findings.append({
                "conflict": name,
                "window": f"{schedule['start']}-{schedule['end']}",
                "uncovered_days": uncovered,
                "consequence": consequence,
                "resolution": "Move the window inside the HOOP, or widen the "
                              "HOOP on those days. Both are configuration; "
                              "neither is a code change.",
            })

    return findings, {"checked": True,
                      "hoop_windows": len(hoop_config.get("windows") or [])}


def validate(event, context=None):
    """Config-time validation, callable from vesting.

    prompt 13.4 requires the conflict to be flagged rather than accepted, so
    this RAISES on a finding when asked to be strict. A vesting run that
    recorded the conflict and continued would produce exactly the environment
    design 14 warns about, with a note nobody reads attached to it.
    """
    import json

    hoop_config = (event or {}).get("hoop") or {}
    findings, summary = check_window_conflicts(
        hoop_config,
        maintenance=(event or {}).get("maintenance_window"),
        restore_test=(event or {}).get("restore_test_window"))

    # The timezone is validated too, by asking for it. A HOOP whose zone
    # cannot be resolved has no windows anybody can compute, and discovering
    # that at vesting is cheaper than discovering it the first night.
    zone_problem = None
    if hoop_config.get("windows"):
        try:
            hoop.zone(hoop_config)
        except hoop.ZoneUnknown as exc:
            zone_problem = str(exc)
            findings.append({
                "conflict": "timezone",
                "consequence": str(exc),
                "resolution": "Set a valid IANA timezone from questionnaire "
                              "3a.7 on the HOOP.",
            })

    out = {"record_type": "hoop-interaction-check",
           "findings": findings,
           "summary": summary,
           "timezone_problem": zone_problem,
           "wiring": {k: v["status"] for k, v in WIRING.items()}}
    print(json.dumps(out, default=str))

    if findings and (event or {}).get("strict"):
        raise ValueError(
            f"{len(findings)} scheduling conflict(s): "
            + "; ".join(f["conflict"] for f in findings)
            + ". prompt 13.4 requires these to be flagged at configuration "
              "time rather than accepted, because each of them fails by not "
              "happening.")
    return out


def handler(event, context=None):
    return validate(event, context)
