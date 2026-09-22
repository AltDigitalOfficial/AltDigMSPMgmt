"""Is this environment scheduled down right now? Prompt 13.4.

One fact, published once, read by everything
----------------------------------------------
design 14 lists eight systems that scheduling touches. Every one of them needs
the same answer: alarms must be suppressed while down, canaries paused,
telemetry silence excused, restore tests kept out of the window, a failed
deployment against a stopped environment treated as a legitimate failure
rather than a change finding.

The obvious implementation has each of them work it out. Each would read the
HOOP, compute the window in the customer's timezone, and decide -- and they
would disagree, because they run on different schedules, evaluate at different
instants, and would each carry their own copy of the timezone handling. An
alarm suppressed by one system's reading of the window and fired by another's
is worse than no suppression at all: it is intermittent, and intermittent
looks like a flapping resource.

So the SCHEDULER publishes what it did, and everything else reads it. Not the
HOOP -- the state. The scheduler is the only component that knows whether a
turndown actually succeeded, and "the window says down" and "the environment
is down" are different claims. A turndown that failed leaves the window closed
and the instances running, and an alarm suppressed on the window's say-so
would then hide a real failure.

Read by things that must not fail when it is absent
-----------------------------------------------------
Absence means "never scheduled", which is the normal state for most of the
estate and must not read as "down". Every reader here gets `up` for an absent
parameter, and that default is safe in the direction that matters: an
environment wrongly believed UP raises findings somebody investigates, while
one wrongly believed DOWN suppresses alarms on a live system.

That asymmetry is why this has no "unknown" verdict. Everywhere else in this
codebase an unreadable signal refuses to conclude; here the safe conclusion
exists and is cheap, so the caller gets it rather than a state it would have
to handle.
"""
import datetime
import json
import os

import boto3

ssm = boto3.client("ssm")

STATE_PARAM = os.environ.get("HOOP_STATE_PARAM", "/platform/hoop/state")

UP = "up"
DOWN = "down"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def publish(state, reason, detail=None):
    """Record what the scheduler just did.

    Called after the work, never before. A state saying `down` while the
    instances are still stopping would have alarm suppression start before the
    alarms it is meant to suppress.
    """
    if state not in (UP, DOWN):
        raise ValueError(f"state must be '{UP}' or '{DOWN}', not '{state}'")
    payload = {"state": state,
               "since": _now().isoformat(),
               "reason": reason,
               "detail": detail or {}}
    ssm.put_parameter(
        Name=STATE_PARAM,
        Value=json.dumps(payload, default=str, sort_keys=True),
        Type="String", Overwrite=True,
        Description="Whether this environment is scheduled down. Published by "
                    "the HOOP scheduler; read by alarm suppression, canary "
                    "pause, telemetry gap detection and cost reporting.")
    return payload


def read(client=None):
    """The current state. Absent reads as UP -- see the module docstring."""
    try:
        got = (client or ssm).get_parameter(Name=STATE_PARAM)
    except Exception:                                 # noqa: BLE001
        return {"state": UP, "since": None,
                "reason": "no HOOP state published; this environment is not "
                          "scheduled",
                "detail": {}}
    raw = (got.get("Parameter") or {}).get("Value")
    if not raw:
        return {"state": UP, "since": None,
                "reason": "HOOP state parameter is empty", "detail": {}}
    return json.loads(raw)


def is_down(client=None):
    return read(client).get("state") == DOWN


def scheduled_down_for(client=None):
    """How long it has been down, in hours, or None.

    Used by the seven-day reporting and by cost: an environment down for
    nine days has had its databases restarted by AWS whether or not the
    scheduler noticed.
    """
    current = read(client)
    if current.get("state") != DOWN or not current.get("since"):
        return None
    try:
        since = datetime.datetime.fromisoformat(
            str(current["since"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((_now() - since).total_seconds() / 3600.0, 1)
