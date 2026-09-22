"""The notification clock. Prompt 7.3.

    Starts at discovery_time. Counts against the tenant's contractual
    notification window [...] Surfaces a live countdown on the incident and
    escalates at defined fractions of the window.

    Nobody should be doing date arithmetic during a live incident. Make the
    deadline impossible to miss and impossible to miscalculate.

Two clocks, in series, from one discovery_time
----------------------------------------------
design 07's chain:

    discovery_time
      |-- AltDigital -> Partner   (tightest -- must leave the partner time)
      |        \\-- Partner -> Client
      |                 \\-- Client -> individuals / HHS  (<=60 days statutory)

The design is explicit that the incident view must show the CHAIN, not just the
next deadline, and that AltDigital notifies the PARTNER -- the partner notifies
the client. Getting that wrong in either direction is a contractual failure
that looks like helpfulness: notifying a partner's client directly is not
AltDigital's call to make.

Both legs run simultaneously from the same discovery_time. The AltDigital leg
must be tighter, because the partner's own window starts when they are told and
they have to have time left. That relationship is VALIDATED here rather than
assumed -- if the windows are configured the wrong way round, the chain cannot
complete and somebody needs to know before an incident rather than during one.

Why a sweep rather than a timer per incident
--------------------------------------------
A one-shot schedule per escalation is the obvious build and has a failure mode
that only shows up on the night it matters: if the schedule is not created, or
is created and deleted, nothing fires and nothing reports that nothing fired.
A sweep that recomputes from discovery_time every few minutes is idempotent,
self-healing, and cannot silently lose a deadline -- the arithmetic is redone
from first principles each time, and the only state it keeps is which
escalations have already been announced.

Unset windows
-------------
Prompt 7.3: "Handle the case where the window is unset: that is itself a
blocking finding at vesting, not a runtime surprise."

So an unset window here does NOT quietly skip the clock. It produces a finding
that says the incident has no measurable obligation, every time the sweep runs,
because an incident with an unknown notification deadline is worse than one
with a tight deadline. The place it is supposed to have been caught is vesting
-- see scripts/derive-vesting.py, which already blocks on a null
baa_window_partner_to_client_hours where PHI is true.
"""
import datetime
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
events = boto3.client("events")
ssm = boto3.client("ssm")

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
WINDOW_PARAM = os.environ.get("NOTIFICATION_WINDOW_PARAM",
                              "/platform/incident/notification-windows")

# Fractions of a window at which the clock speaks up. design 07 says "escalating
# at defined fractions of the window" and does not define them; these are the
# build's choice and are parameters rather than constants.
#
# 0.5 / 0.75 / 0.9 / 1.0 rather than something denser, because every escalation
# that is not actionable is training somebody to stop reading them. Half the
# window gone is the last comfortable moment to start drafting; 90% is the
# "stop what you are doing" point; 1.0 is the breach itself, which is recorded
# because a missed contractual deadline is a fact somebody will later need the
# exact time of.
ESCALATION_FRACTIONS = [
    float(f) for f in
    os.environ.get("ESCALATION_FRACTIONS", "0.5,0.75,0.9,1.0").split(",") if f
]

_WINDOWS = {}


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


def windows_for(account_id):
    """Notification windows for the account, in hours.

    Read from SSM in this account, written by scripts/sync-notification-windows
    .sh from the registry's PARTNER records. Same arrangement as
    /platform/routing/account-tiers: a per-account map published INTO Audit, so
    that the thing running during an incident makes no cross-account call to
    find out what its deadline is.

    Returns (altdig_to_partner_hours, partner_to_client_hours, problem).
    """
    if not _WINDOWS:
        try:
            raw = ssm.get_parameter(Name=WINDOW_PARAM)["Parameter"]["Value"]
            _WINDOWS.update(json.loads(raw))
        except Exception as exc:                   # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            _WINDOWS["__error__"] = code or type(exc).__name__

    err = _WINDOWS.get("__error__")
    if err:
        return None, None, (
            f"could not read {WINDOW_PARAM}: {err}. No notification deadline "
            "can be computed for ANY incident until this is fixed — this is a "
            "platform fault, not a missing tenant field.")

    entry = _WINDOWS.get(account_id)
    if not entry:
        return None, None, (
            f"account {account_id} has no entry in {WINDOW_PARAM}. Its "
            "notification obligations are unknown. This should have been "
            "caught at vesting.")

    return (entry.get("altdig_to_partner_hours"),
            entry.get("partner_to_client_hours"), None)


def chain(discovery_time, altdig_hours, partner_hours):
    """The whole chain, computed once, so nobody does date arithmetic.

    Both legs run from discovery_time, which is what makes the ordering
    constraint real: if AltDigital's window is not tighter than the partner's,
    the partner is told at the moment their own deadline expires.
    """
    legs = []
    for name, hours, who in (
            ("altdigital_to_partner", altdig_hours,
             "AltDigital notifies the PARTNER"),
            ("partner_to_client", partner_hours,
             "the partner notifies their client — NOT AltDigital's call to make"),
    ):
        if hours is None:
            legs.append({"leg": name, "who": who, "window_hours": None,
                         "deadline": None, "unset": True})
            continue
        deadline = discovery_time + datetime.timedelta(hours=float(hours))
        remaining = (deadline - _now()).total_seconds()
        elapsed_fraction = 1.0 - (remaining / (float(hours) * 3600.0))
        legs.append({
            "leg": name,
            "who": who,
            "window_hours": hours,
            "deadline": _iso(deadline),
            "seconds_remaining": round(remaining),
            # Spelled out, because "4h 12m left" is the thing a human can act on
            # and a timestamp at 3am is not.
            "remaining_human": _human(remaining),
            "elapsed_fraction": round(max(elapsed_fraction, 0.0), 4),
            "breached": remaining <= 0,
            "unset": False,
        })
    return legs


def _human(seconds):
    if seconds <= 0:
        return f"OVERDUE by {_human(-seconds)}" if seconds < 0 else "DUE NOW"
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def ordering_problem(altdig_hours, partner_hours):
    """design 07 and design 15: AltDigital's window must be MATERIALLY tighter.

    Checked rather than assumed. Two windows configured the wrong way round
    produce a chain that cannot complete, and the symptom during an incident is
    a partner being notified at the moment their own obligation expires — which
    nobody discovers until it has already happened.
    """
    if altdig_hours is None or partner_hours is None:
        return None
    if float(altdig_hours) >= float(partner_hours):
        return (f"AltDigital's notification window ({altdig_hours}h) is not "
                f"tighter than the partner's ({partner_hours}h). The chain "
                "cannot complete: the partner would be told at or after the "
                "moment their own deadline expires. design 07 and design 15 "
                "both require AltDigital's window to be materially tighter.")
    return None


# ---------------------------------------------------------------------------
# Which incidents still have a clock running
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Recording that a notification HAPPENED
# ---------------------------------------------------------------------------
# Until this existed the clock could only count down. An incident where
# AltDigital notified the partner on time still escalated, still read as
# approaching breach, and still paged -- because nothing could say the
# obligation had been discharged.
#
# prompt 14.4: "it must not be possible for the system to record AltDigital as
# having notified a client."
#
# That is enforced here, and the enforcement is the SHAPE rather than a check
# on a free-text field. Each leg has exactly one legitimate notifier, and the
# pair is an allow-list:
#
#   altdigital_to_partner   notifier: altdigital    recipient: partner
#   partner_to_client       notifier: PARTNER       recipient: client
#
# There is no (altdigital, client) pair, so it cannot be expressed. A validated
# string field would have let somebody pass "altdigital" with a comment
# explaining why it was fine this once; an allow-list of pairs has nowhere to
# put that.
#
# The reason this matters is contractual rather than tidy. AltDigital is a
# subcontractor business associate (design 15): its obligation runs to the
# business associate above it, not to the covered entity. Notifying a partner's
# client directly -- or recording that it did -- asserts a relationship that
# does not exist, and it is the kind of mistake that looks like helpfulness at
# the time.

LEG_NOTIFIERS = {
    "altdigital_to_partner": {"notifier": "altdigital", "recipient": "partner"},
    "partner_to_client": {"notifier": "partner", "recipient": "client"},
}


class NotifierRefused(Exception):
    """The named notifier does not own that leg of the chain."""


def record_notification(incident_id, leg, notifier, notified_at=None,
                        recorded_by=None):
    """Mark one leg of the chain discharged.

    Refuses any (leg, notifier) pair the chain does not contain. In particular
    AltDigital cannot discharge the partner->client leg: that obligation is the
    partner's, and recording otherwise would put a false statement in the
    evidence an auditor reads.
    """
    expected = LEG_NOTIFIERS.get(leg)
    if expected is None:
        raise NotifierRefused(
            f"'{leg}' is not a leg of the notification chain. Known: "
            f"{', '.join(LEG_NOTIFIERS)}.")

    if notifier != expected["notifier"]:
        raise NotifierRefused(
            f"'{notifier}' cannot discharge the '{leg}' leg -- that obligation "
            f"belongs to '{expected['notifier']}', who notifies the "
            f"{expected['recipient']}. design 15 makes AltDigital a "
            "subcontractor business associate whose obligation runs to the "
            "party above it, so recording AltDigital as having notified a "
            "client would assert a relationship that does not exist.")

    # B12 / prompt 14.6: the two legs must be recorded by different people.
    # One person discharging both makes the chain two notifications that were
    # a single act -- and the chain is the whole reason this platform models
    # partner and client as separate parties at all.
    #
    # Checked against what is ALREADY recorded rather than against a claim in
    # this call, because the other leg may have been recorded days earlier by
    # somebody else.
    import separation
    other_leg = next(l for l in LEG_NOTIFIERS if l != leg)
    already = discharged(incident_id).get(other_leg)
    if already and recorded_by:
        separation.assert_separated(
            "notification-chain", already.get("recorded_by"), recorded_by,
            names=(f"{other_leg} (recorded earlier)", leg))

    when = notified_at or _iso(_now())
    dynamodb.put_item(
        TableName=INCIDENT_TABLE,
        Item={
            "PK": {"S": f"incident#{incident_id}"},
            "SK": {"S": f"notified#{leg}"},
            "leg": {"S": leg},
            "notifier": {"S": expected["notifier"]},
            "recipient": {"S": expected["recipient"]},
            "notified_at": {"S": when},
            # Who told the platform, as distinct from who did the notifying.
            # A partner's notification is recorded BY AltDigital and performed
            # BY the partner, and collapsing those two would lose exactly the
            # distinction this function exists to keep.
            "recorded_by": {"S": str(recorded_by or "unrecorded")},
        },
        # One record per leg. A second attempt is not an error -- somebody
        # confirming twice is harmless -- but it must not overwrite the first
        # timestamp, which is the one that proves the deadline was met.
        ConditionExpression="attribute_not_exists(SK)")
    return {"incident_id": incident_id, "leg": leg,
            "notifier": expected["notifier"],
            "recipient": expected["recipient"], "notified_at": when}


def discharged(incident_id):
    """Which legs have been notified, keyed by leg."""
    got = dynamodb.query(
        TableName=INCIDENT_TABLE,
        KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
        ExpressionAttributeValues={
            ":p": {"S": f"incident#{incident_id}"},
            ":s": {"S": "notified#"}})
    out = {}
    for item in got.get("Items") or []:
        out[(item.get("leg") or {}).get("S")] = {
            "notified_at": (item.get("notified_at") or {}).get("S"),
            "notifier": (item.get("notifier") or {}).get("S"),
            "recorded_by": (item.get("recorded_by") or {}).get("S"),
        }
    return out


def open_incidents(max_age_hours):
    """Incident headers recent enough that a deadline could still be live.

    Bounded by age rather than by a status field, because nothing closes an
    incident today — standing one down is a human action and there is no
    mechanism for it. An age bound means the sweep cannot accumulate work
    forever, and it degrades honestly: an incident older than the longest
    window has no deadline left to announce.
    """
    if not INCIDENT_TABLE:
        return []
    since = _iso(_now() - datetime.timedelta(hours=max_age_hours))
    got = dynamodb.query(
        TableName=INCIDENT_TABLE,
        IndexName="by-record-type",
        KeyConditionExpression="record_type = :t AND discovery_time >= :s",
        ExpressionAttributeValues={
            ":t": {"S": "security-incident"},
            ":s": {"S": since},
        },
    ).get("Items", [])
    return [{k: list(v.values())[0] for k, v in item.items()} for item in got]


def already_announced(incident_id):
    """Which escalations this incident has already produced.

    The only state the sweep keeps. Everything else is recomputed from
    discovery_time every run, so a missed sweep catches up rather than losing a
    deadline.
    """
    got = dynamodb.query(
        TableName=INCIDENT_TABLE,
        KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
        ExpressionAttributeValues={
            ":p": {"S": f"incident#{incident_id}"},
            ":s": {"S": "clock#"},
        },
    ).get("Items", [])
    return {item["SK"]["S"] for item in got}


def announce(incident, leg, fraction, state):
    """Record and emit one escalation. Append-only, like everything else."""
    incident_id = incident["incident_id"]
    key = f"clock#{leg['leg']}#{fraction}"
    dynamodb.put_item(
        TableName=INCIDENT_TABLE,
        Item={
            "PK": {"S": f"incident#{incident_id}"},
            "SK": {"S": key},
            "record_type": {"S": "notification-clock"},
            "incident_id": {"S": incident_id},
            "recorded_at": {"S": _iso(_now())},
            "leg": {"S": leg["leg"]},
            "fraction": {"S": str(fraction)},
            "detail": {"S": json.dumps({"leg": leg, "state": state},
                                       default=str)[:38000]},
        },
        # Two sweeps overlapping must not announce the same escalation twice.
        ConditionExpression="attribute_not_exists(SK)",
    )

    breached = fraction >= 1.0
    dossier = {
        "record_type": "notification-clock",
        "recorded_at": _iso(_now()),
        "current_state": "ALARM",
        "alarm": {
            # A DIFFERENT name from the containment dossier, deliberately. The
            # router derives its dedup key from the title, so this raises its
            # own PagerDuty incident rather than updating the containment one.
            # A notification deadline is a separate obligation with a separate
            # owner and a separate clock; burying it as an update to an
            # incident somebody has already acknowledged is how it gets missed.
            "name": f"platform-notification-deadline-{leg['leg']}",
            "resource_id": incident.get("account_id") or incident_id,
            "environment_tier": incident.get("environment_tier"),
            "account_id": incident.get("account_id"),
        },
        "assessment": {
            "probable_cause": (
                f"NOTIFICATION DEADLINE BREACHED — {leg['who']}. The window "
                f"({leg['window_hours']}h from discovery) expired at "
                f"{leg['deadline']}."
                if breached else
                f"Notification deadline at {int(fraction * 100)}% — {leg['who']}. "
                f"{leg['remaining_human']} remaining. Due {leg['deadline']}.")
        },
        "clock": state,
    }
    events.put_events(Entries=[{
        "Source": "platform.security",
        "DetailType": "Platform Incident Dossier",
        "Detail": json.dumps(dossier, default=str),
        "EventBusName": ALERT_BUS_NAME,
    }])
    return key


def evaluate(incident):
    """The live countdown for one incident, plus any escalations now due."""
    incident_id = incident["incident_id"]
    discovery = _parse(incident.get("discovery_time"))
    account_id = incident.get("account_id")

    if discovery is None:
        return {"incident_id": incident_id, "error": "no discovery_time"}

    altdig, partner, problem = windows_for(account_id)
    legs = chain(discovery, altdig, partner)
    ordering = ordering_problem(altdig, partner)

    unset = [l["leg"] for l in legs if l.get("unset")]
    findings = []
    if problem:
        findings.append(problem)
    if ordering:
        findings.append(ordering)
    if unset:
        findings.append(
            f"NO MEASURABLE NOTIFICATION OBLIGATION for {', '.join(unset)}. "
            "An incident whose deadline is unknown cannot be managed to it. "
            "This is a blocking finding at vesting (prompt 7.3), surfaced here "
            "because it was not caught there.")

    # A leg somebody has discharged stops counting. Before this existed the
    # clock escalated an obligation that had been met -- which teaches people
    # that the countdown is noise, on the one signal that must never be.
    done = discharged(incident_id) if INCIDENT_TABLE else {}
    for leg in legs:
        satisfied = done.get(leg["leg"])
        leg["discharged"] = bool(satisfied)
        if satisfied:
            leg["notified_at"] = satisfied["notified_at"]
            leg["notified_by"] = satisfied["notifier"]
            # Whether the obligation was MET is a different question from
            # whether it is still running, and both are recorded: a leg
            # notified after its deadline is discharged and late.
            leg["met_deadline"] = bool(
                leg.get("deadline") and satisfied["notified_at"] <= leg["deadline"])

    live = [l for l in legs
            if not l.get("unset") and not l.get("discharged")]
    next_due = min((l for l in live if not l["breached"]),
                   key=lambda l: l["seconds_remaining"], default=None)

    state = {
        "incident_id": incident_id,
        "discovery_time": incident.get("discovery_time"),
        "account_id": account_id,
        "chain": legs,
        "next_deadline": next_due,
        "breached_legs": [l["leg"] for l in live if l["breached"]],
        # Reported separately from breached. An obligation discharged late is
        # a fact an auditor needs and is not the same as one still outstanding.
        "discharged_legs": [l["leg"] for l in legs if l.get("discharged")],
        "discharged_late": [l["leg"] for l in legs
                            if l.get("discharged") and l.get("met_deadline") is False],
        "findings": findings,
        "computed_at": _iso(_now()),
    }

    announced = already_announced(incident_id) if INCIDENT_TABLE else set()
    fired = []
    for leg in live:
        for fraction in sorted(ESCALATION_FRACTIONS):
            if leg["elapsed_fraction"] < fraction:
                continue
            key = f"clock#{leg['leg']}#{fraction}"
            if key in announced:
                continue
            try:
                announce(incident, leg, fraction, state)
                fired.append(key)
            except dynamodb.exceptions.ConditionalCheckFailedException:
                # Another sweep got there first. Not an error.
                pass
    state["escalations_fired"] = fired
    return state


def handler(event, context):
    # Longest window the map contains, doubled, so an incident stays in scope
    # long enough to announce its own breach and then falls out.
    max_age = int(os.environ.get("MAX_INCIDENT_AGE_HOURS", "720"))
    results = []
    for incident in open_incidents(max_age):
        try:
            results.append(evaluate(incident))
        except Exception as exc:                   # noqa: BLE001
            # One malformed incident must not stop the clock for the others.
            results.append({"incident_id": incident.get("incident_id"),
                            "error": f"{type(exc).__name__}: {exc}"})
    out = {"record_type": "notification-clock-sweep",
           "swept_at": _iso(_now()),
           "incidents": len(results),
           "escalations": sum(len(r.get("escalations_fired") or [])
                              for r in results),
           "with_findings": [r["incident_id"] for r in results
                             if r.get("findings")],
           "results": results}
    print(json.dumps(out, default=str))
    return out
