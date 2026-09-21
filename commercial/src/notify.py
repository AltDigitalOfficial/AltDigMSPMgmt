"""The last link: a client-conversation event reaches PagerDuty. Prompt 11.2.

Why this exists as a Lambda rather than an EventBridge API Destination
------------------------------------------------------------------------
An API Destination was the obvious shape and is the wrong one here, for a
reason this repository has already been bitten by once.

PagerDuty's Events API v2 takes the routing key in the JSON BODY. There is no
header auth. An API Destination would therefore need the key baked into the
rule's input transformer, resolved at deploy time -- which is exactly the wart
`sync-pagerduty-secrets.sh` already warns about for the SNS subscription:

    "CloudFormation resolved the OLD key into the SNS subscription endpoint at
    deploy time, and it does not re-resolve on its own."

Rotating the key would then require a redeploy, and forgetting the redeploy
leaves a destination that silently 400s. Reading the secret at invocation time
makes rotation one command with nothing downstream to remember.

The boundary where the design 12 rule is actually enforced
------------------------------------------------------------
design 12: "informed is not notifier". `route.py` builds the awareness payload
and strips anything a customer could be notified from. This checks it again
before the payload leaves AWS for a third party.

That is not redundancy for its own sake. This is the only place where the data
crosses an organisational boundary, and it is the last moment the platform can
refuse. A field added upstream months from now, by someone who has never read
design 12, reaches a vendor's incident body unless something here is looking.
The allow-list is IMPORTED from route rather than copied, so there is one
definition and two enforcement points.

Severity is always info, and never derived
--------------------------------------------
These notify; they do not page. The PagerDuty service carries a constant low
urgency rule, so severity cannot cause a page on its own -- but severity is
what a human sorting an incident list sees. Deriving it from the event would
mean an SLA outage rendering as `critical` in the commercial channel, next to
an operational incident that actually woke somebody, with no way to tell which
one needs a phone call.
"""
import datetime
import json
import os
import urllib.error
import urllib.request

import boto3

# Imported, not copied. One definition of what may cross the boundary, and two
# places that enforce it. A second copy would drift, and the drift would only
# be visible in a vendor's incident body.
from route import AWARENESS_FIELDS

secrets = boto3.client("secretsmanager")

SECRET_NAME = os.environ.get("COMMERCIAL_ROUTING_KEY_SECRET",
                             "platform/pagerduty/commercial-routing-key")
EVENTS_URL = os.environ.get("PAGERDUTY_EVENTS_URL",
                            "https://events.pagerduty.com/v2/enqueue")
TIMEOUT = int(os.environ.get("PAGERDUTY_TIMEOUT_SECONDS", "15"))

# Fields route.py adds to an awareness payload on top of AWARENESS_FIELDS.
# Listed explicitly rather than allowed by prefix: the point of the check is
# that a new field has to be thought about.
_AWARENESS_EXTRA = frozenset({
    "awareness_only",
    "you_are_informed_not_notifying",
    "notification_owner",
})

_KEY = {}


class RoutingKeyUnavailable(Exception):
    """The routing key could not be read.

    Distinct, and it RAISES rather than returning quietly, so EventBridge
    retries and the failure is visible as an invocation error. A notifier that
    swallowed this would drop client conversations silently -- and silence is
    indistinguishable from there being nothing to say.
    """


class LeakRefused(Exception):
    """The payload carried something an awareness event may not carry."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _routing_key():
    """Read at invocation, cached for the execution environment's life.

    Cached rather than read every time because a burst of conversations should
    not be a burst of Secrets Manager calls; re-read on cold start, which is
    the trade that makes rotation take effect without a redeploy.
    """
    if "value" not in _KEY:
        try:
            raw = secrets.get_secret_value(SecretId=SECRET_NAME)["SecretString"]
            _KEY["value"] = json.loads(raw)["events_v2_routing_key"]
        except Exception as exc:                       # noqa: BLE001
            raise RoutingKeyUnavailable(
                f"could not read {SECRET_NAME}: {type(exc).__name__}. The "
                "client-conversation destination cannot be reached. Run "
                "scripts/sync-pagerduty-secrets.sh after applying the "
                "PagerDuty configuration.") from exc
    return _KEY["value"]


def check_awareness(payload):
    """Refuse to send an awareness payload carrying notifiable detail.

    Only applies to awareness-only events. A cost anomaly carries its number
    because the number is the point, and design 12 says so -- the rule is not
    "send less", it is "an event that exists to warn somebody must not also be
    the thing they notify a customer from".
    """
    if not payload.get("awareness_only"):
        return
    permitted = set(AWARENESS_FIELDS) | _AWARENESS_EXTRA
    unexpected = sorted(set(payload) - permitted)
    if unexpected:
        raise LeakRefused(
            f"awareness payload carries field(s) outside the allow-list: "
            f"{', '.join(unexpected)}. design 12 keeps informed separate from "
            "notifier, and this is the last point before the payload leaves "
            "AWS for a third party. Refusing to send. If the field genuinely "
            "belongs in the commercial channel, add it to AWARENESS_FIELDS in "
            "route.py -- deliberately, in one place.")


def to_pagerduty(body):
    """Shape a client-conversation event into an Events API v2 trigger."""
    payload = body.get("detail") or {}
    kind = body.get("commercial_event") or "client-conversation"
    tenant = payload.get("tenant") or payload.get("account_id") or "platform"

    # Deduplication on the underlying subject, not on this event. Two
    # awareness events about one incident are one conversation, and a
    # commercial channel that shows the same outage three times gets skimmed.
    dedup = payload.get("incident_id") or f"{kind}:{tenant}:{body.get('raised_at')}"

    return {
        "routing_key": _routing_key(),
        "event_action": "trigger",
        "dedup_key": str(dedup),
        "client": "AltDigital Managed Platform",
        "payload": {
            "summary": f"{kind}: {tenant}",
            # Never derived. See the module docstring.
            "severity": "info",
            "source": str(tenant),
            "component": "client-conversation",
            "group": str(payload.get("partner") or "unassigned"),
            "class": str(kind),
            "custom_details": payload,
        },
    }


def send(message):
    req = urllib.request.Request(
        EVENTS_URL, data=json.dumps(message, default=str).encode(),
        method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        # 400 from Events API v2 almost always means a bad routing key, and
        # saying so beats making somebody read PagerDuty's error vocabulary.
        hint = (" The routing key is probably wrong or belongs to a deleted "
                "integration; re-run scripts/sync-pagerduty-secrets.sh."
                if exc.code == 400 else "")
        raise RuntimeError(
            f"PagerDuty rejected the event: HTTP {exc.code} {detail}.{hint}")


def handler(event, context=None):
    """One client-conversation event, delivered or refused."""
    body = (event or {}).get("detail") or {}

    if body.get("record_type") != "client-conversation":
        # Not this notifier's event. Declining loudly rather than silently:
        # something is pointed at this function that should not be, and a
        # quiet return would make that invisible.
        out = {"record_type": "commercial-notify", "sent": False,
               "reason": "not a client-conversation event",
               "seen": body.get("record_type")}
        print(json.dumps(out, default=str))
        return out

    payload = body.get("detail") or {}
    check_awareness(payload)

    message = to_pagerduty(body)
    result = send(message)

    out = {
        "record_type": "commercial-notify",
        "sent": True,
        "sent_at": _now().isoformat(),
        "commercial_event": body.get("commercial_event"),
        "dedup_key": message["dedup_key"],
        "awareness_only": bool(payload.get("awareness_only")),
        "pagerduty_status": result.get("status"),
        "pagerduty_dedup_key": result.get("dedup_key"),
        # What was actually transmitted, by field NAME only. Enough to audit
        # that nothing unexpected crossed the boundary, without repeating the
        # values into a log that is kept for seven years.
        "fields_transmitted": sorted(payload),
    }
    print(json.dumps(out, default=str))
    return out
