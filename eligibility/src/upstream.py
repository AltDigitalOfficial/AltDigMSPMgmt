"""Daily: has the AWS HIPAA Eligible Services Reference changed under us?

docs/hipaa-eligibility.md §1. B-159.

Fetches the page, parses it with the same parser the repository validator
uses, and compares it with the catalog this Lambda was packaged with. Every
outcome is reported through the router; nothing here edits the catalog or the
SCP. A scraped list deployed automatically would hand the preventive control
to whatever HTML aws.amazon.com serves that day.

Outcomes and severities:

    no change                   none. A success metric, so silence is visible
    service added               LOW    a review item: something new may be allowlisted
    service removed             HIGH   existing use may now be non-compliant
    qualification changed       HIGH   ditto; a narrowed qualification reads the same
    page unreadable             HIGH   the check is blind

All are declared digest_and_jira. This runs in Audit, a prod-tier account,
where the tier default would page; an AWS page edit is a review item for
business hours, and only this emitter knows that. The router honours a
declared class only as a downgrade.
"""

import datetime
import json
import os
import urllib.request

import boto3

import catalog
import state

PAGE_URL = os.environ.get(
    "PAGE_URL", "https://aws.amazon.com/compliance/hipaa-eligible-services-reference/")
CATALOG_PATH = os.environ.get("CATALOG_PATH", "hipaa-eligible-services.tsv")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Platform/HipaaEligibility")
# The router reads the tier of the account an event is ABOUT from the detail.
# The upstream check is about no member account; it names its own.
SELF_ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "")
STATE_TABLE = os.environ.get("STATE_TABLE", "")
# Daily, so that an alarm can notice it stopping (an alarm cannot look back
# further than seven days). A diff found today is found again tomorrow; it is
# re-reported at most once per REMIND_DAYS until the catalog is fixed.
REMIND_DAYS = int(os.environ.get("REMIND_DAYS", "7"))

events = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def fetch(url=PAGE_URL):
    req = urllib.request.Request(url, headers={
        # The site serves a reduced page to clients it does not recognise.
        "User-Agent": "Mozilla/5.0 (platform-hipaa-eligibility upstream check)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def emit(subject, severity, summary, facts):
    detail = {
        "framework": "hipaa",
        "check": "upstream",
        "account_id": SELF_ACCOUNT or None,
        "subject": subject,
        "severity": severity,
        "summary": summary,
        "routing": "digest_and_jira",
        "detected_at": _now().isoformat(),
        "facts": facts,
    }
    events.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.eligibility",
        "DetailType": "hipaa-eligibility",
        "Detail": json.dumps(detail, default=str),
    }])
    return detail


def succeeded():
    """The heartbeat. An alarm on its absence is the only way to notice the
    check itself has stopped running; an outcome-only design treats silence as
    'no change'."""
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[{
        "MetricName": "UpstreamCheckCompleted", "Value": 1, "Unit": "Count"}])


def check(page_text, rows, version):
    """Pure decision: page text and catalog in, list of events-to-emit out."""
    out = []
    try:
        page_date, names = catalog.parse_page(page_text)
    except catalog.PageError as exc:
        return [("page-unreadable", "HIGH",
                 f"The AWS HIPAA reference page could not be parsed: {exc}. "
                 "The weekly upstream check is blind until this is fixed.",
                 {"error": str(exc)})]

    in_file = len(catalog.page_services(rows))
    if abs(len(names) - in_file) > max(10, in_file // 10):
        # A swing this large is far likelier to be the parser than AWS. Reported
        # as blindness, not as forty removals, so nobody starts editing the
        # catalog on the strength of it.
        return [("page-unreadable", "HIGH",
                 f"Parsed {len(names)} services against {in_file} in the catalog. A swing "
                 "that large is more likely a page layout change than a list change. "
                 "Check the parser before trusting any diff.",
                 {"parsed": len(names), "catalog": in_file})]

    d = catalog.diff_page(rows, names)
    facts = {"page_last_updated": page_date, "list_version": version}
    for name in d["removed"]:
        out.append((f"removed: {name}", "HIGH",
                    f"'{name}' is no longer on the AWS HIPAA eligible services list "
                    f"(page updated {page_date}). Existing use in HIPAA accounts may now be "
                    "non-compliant. Review the catalog row and every account using its prefixes.",
                    facts))
    for old, new in d["requalified"]:
        out.append((f"requalified: {old}", "HIGH",
                    f"The qualification on '{old}' changed to '{new}' (page updated "
                    f"{page_date}). If it narrowed, existing use may now be non-compliant.",
                    dict(facts, old=old, new=new)))
    for name in d["added"]:
        out.append((f"added: {name}", "LOW",
                    f"'{name}' is newly on the AWS HIPAA eligible services list (page "
                    f"updated {page_date}). Add a catalog row; until then its prefixes stay denied.",
                    facts))
    return out


def handler(event, context):
    version, rows = catalog.load(CATALOG_PATH)
    try:
        text = fetch()
    except Exception as exc:  # noqa: BLE001 — any failure to fetch is blindness
        findings = [("page-unreadable", "HIGH",
                     f"The AWS HIPAA reference page could not be fetched: {type(exc).__name__}: {exc}",
                     {"error": str(exc)})]
    else:
        findings = check(text, rows, version)

    now = _now()
    emitted = [emit(*f) for f in findings
               if state.should_emit(STATE_TABLE, "upstream", f[0], now, REMIND_DAYS)]
    blind = any(f[0] == "page-unreadable" for f in findings)
    if not blind:
        succeeded()
    result = {"list_version": version, "findings": len(emitted), "blind": blind,
              "suppressed_repeats": len(findings) - len(emitted),
              "subjects": [e["subject"] for e in emitted]}
    print(json.dumps(result))
    return result
