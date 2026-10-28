"""Certificate and domain expiry warnings, through the router. ADR-016, B-176.

Design 02, *DNS and certificates*:

    Expiry monitoring is AltDigital's under all three models. An expired
    certificate is an availability event and lands against the SLA regardless
    of who owned the renewal. Same pattern as patching: detection always ours,
    remediation follows the model.

Nothing did it until 2026-10-28. This is the detection half.

------------------------------------------------------------------------------
Why a daily scan and not ACM's own events
------------------------------------------------------------------------------
ACM emits "ACM Certificate Approaching Expiration" to EventBridge, in the
certificate's own region. The platform forwards events from us-east-2 only
(baseline/70, platform-event-forwarding, deployed to the home region). A
certificate for CloudFront must live in us-east-1, so the certificates most
likely to be public are exactly the ones whose events would never arrive.
Route 53 Domains emits no expiry event at all, so a scan was needed for domains
in any case. One scan covering both, reading every allowed region, is one
mechanism rather than two that disagree about thresholds. ADR-016 records the
rejected alternatives.

------------------------------------------------------------------------------
It declares a routing class, and the router decides
------------------------------------------------------------------------------
Hard rule: an emitter may downgrade its own event and never escalate one. The
class declared here is a ceiling the router may lower further, and the router
refuses anything that would page where the tier does not (decide(), dossier
branch). The tier is read here only to choose a ceiling sensible for it. The
router reads the tier again, and its copy is the one that decides.

    dev    digest           also shown in the manager digest's "Expiring soon"
    test   digest_and_jira
    uat    digest_and_jira  not jira_and_commercial: an expiring certificate is
                            not a commercial event
    prod   pagerduty when urgent (exposure.URGENT_DAYS), else digest_and_jira

------------------------------------------------------------------------------
Who gets it
------------------------------------------------------------------------------
No layer is stated, so the router routes it as infrastructure: to AltDigital.
That follows design 02. Detection is AltDigital's, and under model A the
remediation is the client's, so AltDigital is the party that tells them.

------------------------------------------------------------------------------
The else path
------------------------------------------------------------------------------
An account that cannot be read emits nothing about that account. A missing
warning would otherwise look exactly like a healthy certificate. So
ExpiryWatchCompleted is written only when every account in the tier map was
read in full, and platform-expiry-watch-incomplete alarms on its absence
(reporting/30-expiry-watch.yaml). That reaches a person through the router like
any platform alarm.
"""

import datetime
import json
import os

import boto3

import exposure
import facts

ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Platform/ExpiryWatch")

events_client = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def ceiling(tier, urgent):
    if tier == "dev":
        return "digest"
    if tier in ("test", "uat"):
        return "digest_and_jira"
    # prod, and an unknown tier, which the tier map reports as prod (B-073)
    return "pagerduty" if urgent else "digest_and_jira"


def warning(account_id, tier, item, now):
    """The dossier the router receives. One per (account, kind, name).

    The alarm name and resource are stable from day to day, so the router's
    dedup key updates one ticket or incident rather than opening one per run.
    The number of days left is in the body, never in the name.
    """
    return {
        "record_type": "expiry-warning",
        "recorded_at": facts._iso(now),
        "current_state": "ALARM",
        "routing": ceiling(tier, item["urgent"]),
        "alarm": {"name": f"platform-expiry-{item['kind']}",
                  "resource_id": item["name"],
                  "account_id": account_id},
        "assessment": {"probable_cause": (
            f"The {item['kind']} {item['name']} expires {item['expires']}, in "
            f"{item['days']} days. {item['cause']}")},
        "expiry": item,
    }


def _emit(detail):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.expiry",
        "DetailType": "expiry-warning",
        "Detail": json.dumps(detail, default=str),
    }])


def scan(now=None):
    now = now or _now()
    tiers = facts.accounts()
    warned, unreadable = [], {}

    for account_id, tier in sorted(tiers.items()):
        try:
            make = facts.account_clients(account_id, "platform-expiry-watch")
        except Exception as exc:                      # noqa: BLE001
            unreadable[account_id] = f"{type(exc).__name__}"
            continue
        inv = exposure.read_account(make, facts.EXPOSURE_REGIONS,
                                    sections=("certificates", "domains"))
        missing = exposure.unchecked(inv)
        if missing:
            # Still warn about what WAS read. A region that failed is no
            # reason to sit on a certificate found in another one.
            unreadable[account_id] = "partial: " + ", ".join(missing)
        for item in exposure.expiring(inv, now=now):
            _emit(warning(account_id, tier, item, now))
            warned.append({"account": account_id, "kind": item["kind"],
                           "name": item["name"], "days": item["days"]})

    return {"record_type": "expiry-watch-run", "scanned_at": facts._iso(now),
            "accounts": len(tiers), "warnings": warned, "unreadable": unreadable,
            "clean": not unreadable}


def handler(event, context):
    result = scan()
    print(json.dumps(result, default=str))
    if result["clean"]:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[
            {"MetricName": "ExpiryWatchCompleted", "Value": 1, "Unit": "Count"}])
    return result
