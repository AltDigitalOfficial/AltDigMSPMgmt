"""The client-conversation event class. Prompt 11.2, design 12.

design/12-commercial-access.md:

    A FOURTH destination in the routing design of 06, alongside PagerDuty, Jira
    and the review queue.

    Defined by WHO NEEDS TO HAVE A CONVERSATION, not by severity.

That definition is the whole design. A cost anomaly is low severity and needs a
conversation; a failed restore test in a dev sandbox is higher severity and
needs none. Routing this class by severity would get both wrong, which is why
it is a separate destination rather than a threshold on an existing one.

Informed is not notifier
------------------------
design 12 is emphatic, and asks for something stronger than a rule:

    The commercial team must KNOW an incident is in progress, so they are not
    blindsided on a call. The commercial team must NOT be the notifying party.
    [...] Keep informed and notifier clearly separated in the event routing, or
    the distinction erodes the first time someone is being helpful under
    pressure.

A convention erodes. "Do not notify from this channel" is a sentence in a
runbook, and the moment a client is on the phone asking what happened, the
person with the incident details in front of them will answer.

So the separation here is STRUCTURAL: the commercial payload for a security
incident does not contain the facts a notifier would need. Not redacted with a
marker saying what was removed -- absent. Somebody trying to notify from this
event cannot, because the scope, the affected data and the individuals are not
in it and never were.

What it does carry is enough to not be blindsided: an incident exists, which
tenant, when it was declared, and who owns the notification. That last field is
the other half of the structure -- the event names its notifier, so the reader
is looking at somebody else's name while holding the awareness.
"""
import datetime
import json
import os

import boto3

events_client = boto3.client("events")
ssm = boto3.client("ssm")

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
DIGEST_TABLE = os.environ.get("COMMERCIAL_DIGEST_TABLE", "")
NOTIFIER_PARAM = os.environ.get("COMMERCIAL_NOTIFIER_PARAM",
                                "/platform/commercial/notifier")


# design 12's table, as data. Timing is a property of the EVENT, not of its
# severity or of who happens to be awake.
#
# `immediate` where design 12 says immediate, and the reason it gives is worth
# keeping: "those are the ones where a customer calls before the commercial
# team has heard". The test for immediacy is not importance -- it is whether
# the customer finds out first.
EVENT_CLASS = {
    "sla-outage":            {"timing": "immediate", "awareness_only": False},
    "sla-credit-triggered":  {"timing": "immediate", "awareness_only": False},
    # The one that is different, and the reason this module exists.
    "security-incident":     {"timing": "immediate", "awareness_only": True},
    "cost-anomaly":          {"timing": "immediate", "awareness_only": False},
    "budget-breach":         {"timing": "immediate", "awareness_only": False},
    "scope-change":          {"timing": "immediate", "awareness_only": False},
    "exit-initiated":        {"timing": "immediate", "awareness_only": False},
    "vesting-complete":      {"timing": "batched",   "awareness_only": False},
    "onboarding-milestone":  {"timing": "batched",   "awareness_only": False},
    "customer-owned-sla-breach": {"timing": "batched", "awareness_only": False},
    "service-model-change":  {"timing": "batched",   "awareness_only": False},
    "maintenance-coordination": {"timing": "batched", "awareness_only": False},
}

# What an awareness-only event is permitted to carry.
#
# An ALLOW-LIST, not a deny-list, and that direction is the control. A deny-list
# would let a new field added upstream flow through to the commercial channel
# by default -- and the fields that matter most here are exactly the ones
# somebody adds later when an incident turns out to need more detail.
AWARENESS_FIELDS = (
    "incident_id",
    "tenant",
    "partner",
    "account_id",
    "declared_at",
    "environment_tier",
    "state",
)


class NotifierUnknown(Exception):
    """Nobody is recorded as owning notification.

    Fatal for an awareness-only event. design 12 puts notification under the
    BAA clock and names it as owned by Art and Wayne; an awareness event that
    cannot say who the notifier is invites the reader to become one.
    """


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def notifier():
    """Who owns customer notification. From configuration, never defaulted.

    Returns a role reference rather than a person: design 12 assigns this to
    contract owners, and a name in a payload goes stale and is personal data
    besides. The role resolves to people in the system that owns identity.
    """
    try:
        raw = ssm.get_parameter(Name=NOTIFIER_PARAM)["Parameter"]["Value"]
    except Exception as exc:                       # noqa: BLE001
        raise NotifierUnknown(
            f"could not read {NOTIFIER_PARAM}: {type(exc).__name__}. An "
            "awareness-only event must name its notifier, or the person "
            "reading it is the only candidate in view.") from exc
    value = (json.loads(raw) if raw.strip().startswith("{") else {"role": raw})
    role = (value or {}).get("role")
    if not role:
        raise NotifierUnknown(
            f"{NOTIFIER_PARAM} names no notifier role.")
    return role


def to_awareness(detail):
    """Strip a security incident to what the commercial team may see.

    The structural half of design 12's "informed is not notifier". What comes
    back cannot be used to notify a customer, because the scope, the affected
    data and the individuals are not in it.

    Note what is NOT done: nothing is replaced with a redaction marker. A
    payload saying `"affected_records": "[REDACTED]"` tells the reader there is
    a number and invites them to go and find it. Absent is absent.
    """
    out = {k: detail.get(k) for k in AWARENESS_FIELDS if detail.get(k) is not None}
    out["awareness_only"] = True
    out["you_are_informed_not_notifying"] = (
        "This is awareness so that a call does not blindside you. Customer "
        "notification is governed by the BAA clock and is owned by the role "
        "named in `notification_owner`. The record that matters is the one "
        "Truveon holds against that clock, not this event.")
    return out


def classify(event):
    """Which class of client conversation this is, and when it travels."""
    detail = event.get("detail") or {}
    kind = detail.get("commercial_event") or detail.get("record_type")

    # An explicit mapping, not a guess. An unrecognised event is NOT routed
    # here on the assumption that it might be commercial -- the whole value of
    # a channel defined by "who needs a conversation" is that it stays small
    # enough to be read.
    spec = EVENT_CLASS.get(kind)
    if spec is None:
        return None, None
    return kind, spec


def route(event, context=None):
    """Send one event to the commercial destination, or decline to."""
    kind, spec = classify(event)
    if spec is None:
        out = {"record_type": "commercial-routing", "routed": False,
               "reason": "not a client-conversation event",
               "seen": (event.get("detail") or {}).get("record_type")}
        print(json.dumps(out, default=str))
        return out

    detail = event.get("detail") or {}

    if spec["awareness_only"]:
        try:
            owner = notifier()
        except NotifierUnknown as exc:
            # Refuses to send rather than sending an awareness event with no
            # notifier named. An event that says "an incident is in progress"
            # and nothing about who tells the customer is the exact shape that
            # produces a helpful person on a phone call.
            out = {"record_type": "commercial-routing", "routed": False,
                   "reason": str(exc), "blocked_by": "NotifierUnknown"}
            print(json.dumps(out, default=str))
            return out
        payload = to_awareness(detail)
        payload["notification_owner"] = owner
    else:
        payload = dict(detail)

    body = {
        "record_type": "client-conversation",
        "commercial_event": kind,
        "timing": spec["timing"],
        "raised_at": _iso(_now()),
        "detail": payload,
        # design 12: "a PagerDuty business service with NOTIFICATION rules
        # rather than paging rules". Carried explicitly so the destination
        # cannot be mistaken for the operational one by anything downstream.
        "destination": "commercial-business-service",
        "pages": False,
    }

    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.commercial",
        "DetailType": "client-conversation",
        "Detail": json.dumps(body, default=str),
    }])

    out = {"record_type": "commercial-routing", "routed": True,
           "commercial_event": kind, "timing": spec["timing"],
           "awareness_only": spec["awareness_only"]}
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"route": route}


def handler(event, context):
    return route(event, context)
