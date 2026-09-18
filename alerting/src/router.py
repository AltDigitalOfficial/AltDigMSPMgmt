"""The single routing layer. Everything that wants attention arrives here.

Prompt 5.3, design/06 "Alerting and routing":

    CloudWatch alarm -+                        +-> PagerDuty (actionable now)
    Config finding    +-> EventBridge -> routing +-> Jira (owned, dated)
    Security Hub      |                        +-> Review queue (no page)
    GuardDuty        -+                        +-> Commercial (conversation)

Routing is by severity AND environment. Production canary failure pages; a dev
environment CPU alarm does not.

Where the decision comes from
-----------------------------
design/06a, which is authoritative and already carries the whole model: each
tier has a `default_routing`, each alarm may override it with
`severity_by_tier`, and `routing_destinations` says what each routing class
actually does:

    pagerduty            pages, ticket, digest
    jira_and_commercial  ticket, digest, commercial event   -- no page
    digest_and_jira      ticket, digest                     -- no page
    digest               digest                             -- no page
    review_queue         nothing but the record             -- no page

Nothing here re-derives that table. The instrumentation Lambda already computed
the routing class when it created the alarm and wrote it to the alarm's
`platform-routing` tag, so this reads the decision rather than making it a
second time and risking a different answer.

The fourth destination
----------------------
The prompt names three. design/06's own diagram has four, the fourth being
"defined by who needs to have a conversation, not by severity", and 06a's
`jira_and_commercial` depends on it. Built, because dropping it would silently
downgrade every alarm 06a routes that way.

Why paging is a direct POST rather than SNS
-------------------------------------------
The existing SNS subscription posts to PagerDuty's CloudWatch integration,
which parses an SNS-wrapped CloudWatch alarm and nothing else. The prompt
requires the page to carry the full dossier, which means arbitrary JSON in
custom_details, which means Events API v2.

It also means a dedup_key, and that turns out to be the load-bearing part. An
alarm fires and pages; minutes later the response engine finishes gathering and
sends the dossier. Without a dedup key those are two incidents about one
outage. With one, the second updates the first, so the page arrives immediately
and the evidence catches up.
"""
import datetime
import hashlib
import json
import os
import urllib.error
import urllib.request

import boto3

# No cloudwatch client. The router deliberately makes no cross-account AWS call
# on the alert path — everything it needs arrives in the event. See
# alarm_routing() for what that replaced and why.
secrets = boto3.client("secretsmanager")
sqs = boto3.client("sqs")

PD_SECRET_NAME = os.environ.get("PAGERDUTY_SECRET_NAME", "")
PD_SECRET_NAME_LOW = os.environ.get("PAGERDUTY_SECRET_NAME_LOW", "")
JIRA_QUEUE_URL = os.environ.get("JIRA_QUEUE_URL", "")
REVIEW_QUEUE_URL = os.environ.get("REVIEW_QUEUE_URL", "")
COMMERCIAL_QUEUE_URL = os.environ.get("COMMERCIAL_QUEUE_URL", "")
ROUTING_LOG_GROUP = os.environ.get("ROUTING_LOG_GROUP", "/aws/platform/routing")

PD_ENQUEUE = "https://events.pagerduty.com/v2/enqueue"

# design/06a routing_destinations, duplicated here for one specific reason: this
# Lambda runs in the Audit account and 06a ships with the instrumentation
# package into member accounts. Shipping the whole spec here to read five lines
# of it would be worse. The instrumentation Lambda is authoritative and computes
# the routing class; this only needs to know what each class DOES.
#
# If these ever disagree, the tag wins, because the tag is what the alarm was
# created with.
DESTINATIONS = {
    "pagerduty":           {"pages": True,  "ticket": True,  "digest": True},
    "jira_and_commercial": {"pages": False, "ticket": True,  "digest": True,
                            "commercial_event": True},
    "digest_and_jira":     {"pages": False, "ticket": True,  "digest": True},
    "digest":              {"pages": False, "ticket": False, "digest": True},
    "review_queue":        {"pages": False, "ticket": False, "digest": False},
}

# Tier defaults from 06a, used only when an event carries no routing decision of
# its own — a Security Hub or GuardDuty finding, which no alarm tag covers.
TIER_DEFAULT_ROUTING = {
    "dev": "digest", "test": "digest_and_jira",
    "uat": "jira_and_commercial", "prod": "pagerduty",
}

# Finding severities that page in a production account. Below this they are
# tickets, because design/06 is explicit that nothing pages which a human
# cannot act on right now, and a MEDIUM GuardDuty finding at 3am is not that.
FINDING_PAGES_AT = {"CRITICAL", "HIGH"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Normalising the sources
# ---------------------------------------------------------------------------
# Four event shapes arrive and every one puts the account, the resource and the
# severity somewhere different. Everything downstream works on the normalised
# form, so adding a fifth source is a function here and nothing else.


def from_alarm(event):
    d = event.get("detail") or {}
    name = d.get("alarmName") or ""
    state = (d.get("state") or {}).get("value")
    return {
        "kind": "alarm",
        "source": "cloudwatch",
        "account_id": event.get("account"),
        "region": event.get("region"),
        "title": name,
        "state": state,
        # The alarm's own description, which carries the routing trailer. The
        # RESOURCE comes out of that trailer rather than out of the alarm name:
        # the name is platform-auto-RESOURCEID-ALARMID and both halves may
        # contain dashes, so parsing it is guesswork. B-031.
        "description": (d.get("configuration") or {}).get("description"),
        "resource": name,
        # No alarm_arn. It was here for the tag lookup that turned out to be
        # impossible, and it crashed on the way out: event.get("resources",
        # [None])[0] raises IndexError when `resources` is PRESENT and empty,
        # because a default only applies to a missing key. EventBridge supplies
        # an empty list, so the router died on an event it was built to handle
        # and the whole invocation dead-lettered.
        "reason": (d.get("state") or {}).get("reason"),
        "occurred_at": event.get("time"),
        # Resolved from the alarm's tags below. An alarm the platform did not
        # create has none, which is itself a finding — see route().
        "routing": None,
    }


def from_security_hub(event):
    d = event.get("detail") or {}
    findings = d.get("findings") or [{}]
    f = findings[0]
    res = (f.get("Resources") or [{}])[0]
    return {
        "kind": "finding",
        "source": "securityhub",
        "account_id": f.get("AwsAccountId") or event.get("account"),
        "region": event.get("region"),
        "title": f.get("Title") or "Security Hub finding",
        "severity": ((f.get("Severity") or {}).get("Label") or "").upper(),
        "resource": res.get("Id") or "unknown",
        "reason": f.get("Description"),
        "occurred_at": f.get("UpdatedAt") or event.get("time"),
        "generator": f.get("GeneratorId"),
        "routing": None,
    }


def from_guardduty(event):
    d = event.get("detail") or {}
    # GuardDuty severity is numeric 1-8.9. The published bands are
    # low 1-3.9, medium 4-6.9, high 7-8.9; 9+ is documented as reserved and has
    # never been observed, so it is folded into CRITICAL rather than dropped.
    sev = d.get("severity") or 0
    label = ("CRITICAL" if sev >= 9 else "HIGH" if sev >= 7
             else "MEDIUM" if sev >= 4 else "LOW")
    res = d.get("resource") or {}
    return {
        "kind": "finding",
        "source": "guardduty",
        "account_id": d.get("accountId") or event.get("account"),
        "region": event.get("region"),
        "title": d.get("title") or "GuardDuty finding",
        "severity": label,
        "resource": (res.get("instanceDetails") or {}).get("instanceId")
                    or (res.get("accessKeyDetails") or {}).get("userName")
                    or res.get("resourceType") or "unknown",
        "reason": d.get("description"),
        "occurred_at": d.get("updatedAt") or event.get("time"),
        "routing": None,
    }


def from_config(event):
    d = event.get("detail") or {}
    ni = (d.get("newEvaluationResult") or {})
    return {
        "kind": "finding",
        "source": "config",
        "account_id": event.get("account"),
        "region": event.get("region"),
        "title": f"Config rule {d.get('configRuleName')} NON_COMPLIANT",
        # Config has no severity. Treated as MEDIUM so it tickets rather than
        # pages: a drifted control is real and dated work, and waking somebody
        # for it is how the rotation learns to ignore the pager.
        "severity": "MEDIUM",
        "resource": (d.get("resourceId") or "unknown"),
        "reason": ni.get("annotation") or d.get("configRuleName"),
        "occurred_at": event.get("time"),
        "routing": None,
    }


def from_dossier(event):
    """The response engine's incident readout, after it has tried and failed."""
    d = event.get("detail") or {}
    alarm = d.get("alarm") or {}
    return {
        "kind": "dossier",
        "source": "response-engine",
        "account_id": alarm.get("account_id") or event.get("account"),
        "region": event.get("region"),
        "title": alarm.get("name") or "response engine",
        "resource": alarm.get("resource_id") or "unknown",
        "environment_tier": alarm.get("environment_tier"),
        "reason": (d.get("assessment") or {}).get("probable_cause"),
        "occurred_at": d.get("recorded_at") or event.get("time"),
        "state": d.get("current_state"),
        "dossier": d,
        "routing": None,
    }


NORMALISERS = {
    "CloudWatch Alarm State Change": from_alarm,
    "Security Hub Findings - Imported": from_security_hub,
    "GuardDuty Finding": from_guardduty,
    "Config Rules Compliance Change": from_config,
    "Platform Incident Dossier": from_dossier,
}


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


# Written into every AlarmDescription by the instrumentation Lambda. Must match
# ROUTING_MARKER there.
ROUTING_MARKER = "[platform-routing]"


def alarm_routing(description):
    """Read the routing class the instrumentation Lambda already decided.

    Out of the alarm's DESCRIPTION, which arrives inside the event as
    detail.configuration.description — not out of its tags.

    The tags carry the same facts and were the obvious source, and they are
    unreachable. CloudWatch alarms have no resource-based policy, so there is no
    IAM grant that lets the Audit account read another account's alarm tags; the
    call fails and every alarm falls through to the review queue. Found on the
    first live event, where the log read
    "could not read alarm tags: ClientError".

    Reading the description is better than fixing that would have been. The
    alternative was assuming a role per member account per alarm, which puts a
    cross-account round trip on the path that pages people, at the moment an
    incident is starting and the account may itself be unhealthy.

    Returns (routing, tier, note).
    """
    if not description or ROUTING_MARKER not in description:
        return None, None, ("no routing marker in the alarm description — this "
                            "alarm was not created by the instrumentation "
                            "Lambda, or predates the marker")
    trailer = description.split(ROUTING_MARKER, 1)[1]
    fields = {}
    for token in trailer.split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
    return fields.get("routing"), fields.get("tier"), None


def decide(norm):
    """Turn a normalised event into a routing class.

    Every path ends in a named class, and an unrecognised one is treated as
    pagerduty rather than dropped — the same else rule the response engine
    follows. An event nobody anticipated is the one most likely to matter.
    """
    if norm["kind"] == "alarm":
        routing, tier, note = alarm_routing(norm.get("description"))
        norm["environment_tier"] = tier
        if norm.get("description") and ROUTING_MARKER in norm["description"]:
            trailer = norm["description"].split(ROUTING_MARKER, 1)[1]
            for token in trailer.split():
                if token.startswith("resource-id="):
                    # The real resource, so the page names the database rather
                    # than the alarm about the database.
                    norm["resource"] = token.split("=", 1)[1]
        if routing:
            return routing, f"alarm tag platform-routing={routing}"
        # No tag. Either an alarm the platform did not create, or one created
        # before tagging existed. Not silently dropped and not silently paged:
        # it goes to the review queue WITH the reason, because the finding is
        # that an untagged alarm exists rather than whatever the alarm says.
        return "review_queue", (
            f"no platform-routing tag ({note or 'alarm not created by the platform'}). "
            "Routed to review rather than guessed at. An untagged alarm is a gap "
            "in instrumentation, not an incident.")

    if norm["kind"] == "dossier":
        # The response engine only emits a dossier when it has decided a human
        # is needed, so the routing class is not in question — what varies is
        # urgency, and that is the tier's business below.
        tier = norm.get("environment_tier") or "prod"
        return ("pagerduty" if tier in ("uat", "prod")
                else "digest_and_jira"), f"response engine escalation, tier {tier}"

    # Findings. No alarm tag exists, so the tier default applies, floored by
    # the finding's own severity: a LOW GuardDuty finding in production is not
    # a page just because production pages by default.
    tier = norm.get("environment_tier") or "prod"
    default = TIER_DEFAULT_ROUTING.get(tier, "digest")
    sev = (norm.get("severity") or "").upper()
    if default == "pagerduty" and sev not in FINDING_PAGES_AT:
        return "digest_and_jira", (
            f"{tier} defaults to pagerduty, but a {sev or 'UNRATED'} finding is "
            "not actionable right now — ticketed instead")
    return default, f"tier default for {tier}, severity {sev or 'UNRATED'}"


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def dedup_key(norm):
    """One incident per (account, resource, alarm), not one per event.

    This is what merges the raw alarm with the dossier that arrives minutes
    later. Both derive the same key from the same three facts, so PagerDuty
    treats the second as an update to the first — the page goes out
    immediately and the evidence catches up, instead of two pages about one
    outage.
    """
    raw = "|".join([
        norm.get("account_id") or "", norm.get("resource") or "",
        norm.get("title") or "",
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def routing_key(tier):
    name = PD_SECRET_NAME_LOW if tier in ("dev", "test") and PD_SECRET_NAME_LOW \
        else PD_SECRET_NAME
    if not name:
        return None
    raw = secrets.get_secret_value(SecretId=name)["SecretString"]
    return json.loads(raw).get("events_v2_routing_key")


def page(norm, routing, why):
    tier = norm.get("environment_tier") or "prod"
    key = routing_key(tier)
    if not key:
        raise RuntimeError(
            "No Events API v2 routing key. Refusing to silently not page: the "
            "caller records this and the DLQ retains the event.")

    body = {
        "routing_key": key,
        "event_action": "resolve" if norm.get("state") == "OK" else "trigger",
        "dedup_key": dedup_key(norm),
        "payload": {
            # B-027 will make this readable at a glance. Until then the account
            # and resource are in the summary, because the alarm name alone
            # answers roughly one of the four questions design/07 asks.
            "summary": (f"[{tier}] {norm.get('title')} — "
                        f"{norm.get('resource')} in {norm.get('account_id')}")[:1024],
            "source": f"{norm.get('source')}/{norm.get('account_id')}",
            "severity": "critical" if tier == "prod" else "warning",
            "component": norm.get("resource"),
            "group": norm.get("account_id"),
            "class": norm.get("kind"),
            "timestamp": norm.get("occurred_at"),
            # The whole dossier, which is the point of prompt 5.3. When the
            # response engine has not run there is none, and the payload says
            # so rather than arriving looking complete.
            "custom_details": norm.get("dossier") or {
                "note": "No dossier. Either no runbook covers this alarm, or the "
                        "response engine has not finished gathering — if it has, "
                        "a second event will update this incident in place.",
                "reason": norm.get("reason"),
                "routing_decision": why,
            },
        },
    }

    # ensure_ascii and an explicit charset, both.
    #
    # The first live page rendered an em dash as mojibake in PagerDuty. Dossier
    # text is full of them, so every real page would have carried garbled
    # characters through to whoever was reading it at 3am. ensure_ascii=True
    # escapes non-ASCII to \uXXXX so nothing depends on the receiver guessing an
    # encoding, and the charset header says so regardless.
    req = urllib.request.Request(
        PD_ENQUEUE, data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return {"status": resp.status, "dedup_key": body["dedup_key"],
                "event_action": body["event_action"]}


def enqueue(url, norm, routing, why, destination):
    if not url:
        return {"sent": False, "reason": f"{destination} queue not configured"}
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({
        "destination": destination,
        "routing": routing,
        "why": why,
        "recorded_at": _now().isoformat(),
        "event": norm,
    }, default=str))
    return {"sent": True}


def handler(event, context):
    detail_type = event.get("detail-type") or ""
    normalise = NORMALISERS.get(detail_type)

    if normalise is None:
        # The else. An event shape nobody anticipated is not dropped — it goes
        # to review with the raw event attached, because a router that silently
        # discards what it does not recognise is indistinguishable from one that
        # is working.
        enqueue(REVIEW_QUEUE_URL,
                {"kind": "unknown", "source": event.get("source"),
                 "title": detail_type or "(no detail-type)",
                 "account_id": event.get("account"), "raw": event},
                "review_queue",
                f"no normaliser for detail-type '{detail_type}'", "review")
        out = {"routed": "review_queue", "reason": "unrecognised event shape",
               "detail_type": detail_type}
        print(json.dumps(out))
        return out

    norm = normalise(event)

    # An alarm returning to OK is only interesting as a PagerDuty resolve. It is
    # not a ticket and not a review item; filing one per recovery would bury the
    # queues in work nobody asked for.
    if norm["kind"] == "alarm" and norm.get("state") not in ("ALARM", "OK"):
        out = {"routed": "ignored", "reason": f"alarm state {norm.get('state')}"}
        print(json.dumps(out))
        return out

    routing, why = decide(norm)
    spec = DESTINATIONS.get(routing)
    if spec is None:
        # Unrecognised routing class. Paged, deliberately: this means 06a named
        # a class this router does not implement, and the alarm it belongs to is
        # currently going nowhere. Under-routing is the failure that hides.
        spec = DESTINATIONS["pagerduty"]
        why = (f"routing class '{routing}' is not implemented by the router, so "
               "the event was escalated rather than dropped. Fix the router or "
               "06a — until then this alarm has no correct destination.")
        routing = "pagerduty"

    recovery = norm["kind"] == "alarm" and norm.get("state") == "OK"

    delivered = {}
    if spec["pages"]:
        delivered["pagerduty"] = page(norm, routing, why)
    elif recovery:
        # A recovery on a non-paging alarm has nowhere useful to go.
        out = {"routed": "ignored", "reason": "recovery on a non-paging alarm"}
        print(json.dumps(out))
        return out

    if recovery:
        # Resolve the incident and stop. A recovery is not work: filing a
        # ticket for it would mean every alarm that clears on its own produces
        # something for a human to close, and a queue of those is how the real
        # ones get closed unread.
        record = {"record_type": "routing-decision",
                  "recorded_at": _now().isoformat(),
                  "detail_type": detail_type, "source": norm.get("source"),
                  "account_id": norm.get("account_id"),
                  "title": norm.get("title"), "routing": routing,
                  "why": "recovery — incident resolved, no ticket raised",
                  "delivered": delivered}
        print(json.dumps(record, default=str))
        return record

    if spec.get("ticket"):
        delivered["jira"] = enqueue(JIRA_QUEUE_URL, norm, routing, why, "jira")
    if spec.get("commercial_event"):
        delivered["commercial"] = enqueue(
            COMMERCIAL_QUEUE_URL, norm, routing, why, "commercial")
    if not spec["pages"] and not spec.get("ticket"):
        delivered["review"] = enqueue(
            REVIEW_QUEUE_URL, norm, routing, why, "review")

    # Every decision is recorded, including the ones that deliberately did
    # nothing. "Why was I not paged about this" is the question this log exists
    # to answer, and it is asked after the fact.
    record = {
        "record_type": "routing-decision",
        "recorded_at": _now().isoformat(),
        "detail_type": detail_type,
        "source": norm.get("source"),
        "account_id": norm.get("account_id"),
        "environment_tier": norm.get("environment_tier"),
        "title": norm.get("title"),
        "resource": norm.get("resource"),
        "routing": routing,
        "why": why,
        "delivered": delivered,
        # digest is a FLAG on the destination, not a delivery: 4.4 builds the
        # digest by reading records like this one, rather than by being pushed
        # to. Recorded so that reading is possible.
        "digest": spec.get("digest", False),
    }
    print(json.dumps(record, default=str))
    return record
