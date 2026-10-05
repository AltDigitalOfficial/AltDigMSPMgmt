"""File a HIPAA eligibility ticket in the TENANT's own Jira. E8.

docs/hipaa-eligibility.md §9, E8 (Jamie, 2026-10-05). Subscribed to the tenant
alert topic. The router decided this finding goes to the tenant now; this only
carries it there.

Where it files comes from /platform/eligibility/tenant-alerts (written by
scripts/sync-hipaa-alerts.sh from a gitignored file):

    {"oeight-oeight": {"jira_site": "https://<site>.atlassian.net",
                       "jira_project": "ARC8", "issue_type": "Task"}}

Credential: the tenant's own Jira secret, platform/jira/tenant/<tenant>
({"email": ..., "api_token": ...}), the same one set-jira-token.sh --tenant
writes. That key was first provisioned for reading change records. Filing needs
"Create issues" in the project; if it lacks it, Jira answers 403 and this says
so loudly rather than retrying a permission it will never get.

The else: any failure to file — no target configured, no secret, Jira refusing
— is reported to AltDigital through the router (AltDigital's own layer, Audit
account, declared digest_and_jira), so a ticket that was never raised is
itself visible. The tenant contact's email has already gone by then, by its
own subscription, so the tenant is never left unaware because Jira failed.
"""

import base64
import datetime
import json
import os
import urllib.error
import urllib.request

import boto3

TARGETS_PARAM = os.environ.get("TARGETS_PARAM", "/platform/eligibility/tenant-alerts")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "platform/jira/tenant")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
SELF_ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "")

ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
events = boto3.client("events")


def adf(paragraphs):
    """Atlassian Document Format: plain paragraphs only."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]}
        for p in paragraphs if p]}


def build_issue(target, payload):
    """Pure: the Jira create-issue body for one finding."""
    norm, d = payload.get("norm") or {}, payload.get("detail") or {}
    env = d.get("environment") or norm.get("environment_tier") or "?"
    subject = d.get("subject") or norm.get("resource") or "unknown"
    summary = f"HIPAA eligibility: regulated data designed onto a non-eligible service ({env}) — {subject}"
    body = [
        d.get("summary") or norm.get("reason"),
        f"Account {norm.get('account_id')} ({env}). Resource: {subject}.",
        "What this means: a resource tagged DataClassification=regulated is on an AWS "
        "service that is not HIPAA-eligible. In dev or test this is not an incident. It "
        "means the design would put patient data where AltDigital's agreement with AWS "
        "does not cover it, and it cannot be deployed to uat or prod as built.",
        "To resolve: move it to an eligible service (developer guide 4.16 lists "
        "alternatives), or, if this resource will never hold patient data, correct its "
        "DataClassification tag. The daily sweep closes the loop: once the resource is "
        "gone or reclassified, it stops being reported.",
        f"Raised automatically by the AltDigital platform. Routing: {payload.get('routing_why')}",
    ]
    return {"fields": {
        "project": {"key": target["jira_project"]},
        "issuetype": {"name": target.get("issue_type") or "Task"},
        "summary": summary[:250],
        "description": adf(body),
        "labels": ["hipaa-eligibility", f"account-{norm.get('account_id')}"],
    }}


def report_failure(tenant, why):
    events.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME, "Source": "platform.eligibility",
        "DetailType": "hipaa-eligibility",
        "Detail": json.dumps({
            "framework": "hipaa", "check": "tenant-jira", "account_id": SELF_ACCOUNT or None,
            "subject": f"ticket not filed: {tenant}", "severity": "HIGH",
            "routing": "digest_and_jira",
            "summary": f"A HIPAA eligibility ticket for {tenant} could not be filed in the "
                       f"tenant's Jira: {why}. The tenant contact's email was still sent.",
            "detected_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})}])


def file_one(payload):
    tenant = payload.get("tenant")
    try:
        targets = json.loads(ssm.get_parameter(Name=TARGETS_PARAM)["Parameter"]["Value"])
    except Exception as exc:  # noqa: BLE001
        return report_failure(tenant, f"{TARGETS_PARAM} unreadable ({type(exc).__name__})")
    target = targets.get(tenant)
    if not target or not target.get("jira_site") or not target.get("jira_project"):
        return report_failure(tenant, "no Jira site and project configured for this tenant")
    try:
        cred = json.loads(secrets.get_secret_value(
            SecretId=f"{SECRET_PREFIX}/{tenant}")["SecretString"])
    except Exception as exc:  # noqa: BLE001
        return report_failure(tenant, f"no Jira credential ({type(exc).__name__})")

    auth = base64.b64encode(f"{cred['email']}:{cred['api_token']}".encode()).decode()
    req = urllib.request.Request(
        target["jira_site"].rstrip("/") + "/rest/api/3/issue",
        data=json.dumps(build_issue(target, payload)).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json",
                 "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            key = json.load(resp).get("key")
            print(json.dumps({"filed": key, "tenant": tenant}))
            return {"filed": key}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        hint = (" The credential can read but not create issues in this project."
                if exc.code == 403 else "")
        return report_failure(tenant, f"Jira answered {exc.code}: {detail}.{hint}")


def handler(event, context):
    out = []
    for rec in event.get("Records", []):
        payload = json.loads(rec["Sns"]["Message"])
        out.append(file_one(payload))
    return out
