"""Manager digest — batched, per account, to the client's development manager.

Prompt 4.4 and design doc 06, "The dev team notification loop":

    Here is what appeared. Here is what we instrumented generically. Reply if
    it needs something more specific.

Extended 2026-10-14 into design 11's *Manager digest* (docs/digest-design.md,
ADR-003, B-171): what needs the manager's attention, what the team did, the
monitoring, and cost. Stage A carries the sections that need no CloudTrail
query; the rest say "not available yet" until stage B.

------------------------------------------------------------------------------
Batched is the requirement, not an optimisation
------------------------------------------------------------------------------
Doc 06: "A Terraform run creating forty resources must not generate forty
emails; that gets filtered to a folder within a week."

That sentence describes the actual failure mode. An unbatched digest is not
merely annoying — it trains the recipient to filter the address, after which
the compliance value below is gone and nobody notices, because the emails are
still being sent successfully.

------------------------------------------------------------------------------
It is evidence, not just a courtesy
------------------------------------------------------------------------------
Doc 06: "documented evidence that monitoring requirements were communicated to
the application owner. If something later fails unmonitored, the trail shows we
asked."

So the send record matters as much as the send. It goes to the Object Lock
archive (ADR-001); the Truveon hand-off is recorded as outstanding. Losing the
record would lose the evidence, which is the half that survives the
conversation.

------------------------------------------------------------------------------
Central, not per-account
------------------------------------------------------------------------------
Runs in Platform Tooling. Reads each member account's alarms and
instrumentation exceptions through a read-only role, and everything else from
the facts documents reporting/src/facts.py writes in Audit (ADR-004). One SES
identity: reputation is per-identity, and a tenant account that started
bouncing would otherwise damage a reputation nobody is watching.

------------------------------------------------------------------------------
One window per email
------------------------------------------------------------------------------
Every section covers the same 24 hours: the window facts.window() defines,
ending at 12:00 UTC. The alarms and exceptions read here used to cover the 24
hours to the run, an hour later than the facts. An email whose sections
describe different days invites the reader to reconcile them, and the one
that does not reconcile is the one they stop trusting.
"""

import datetime
import json
import os
import re

import boto3
from botocore.exceptions import ClientError

import facts as facts_mod

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")
SENDER = os.environ.get("DIGEST_SENDER", "")
CC_ADDRESS = os.environ.get("DIGEST_CC", "")
READER_ROLE = os.environ.get("READER_ROLE_NAME", "PlatformDigestReader")
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")
FACTS_BUCKET = os.environ.get("FACTS_BUCKET", "")
CONFIGURATION_SET = os.environ.get("SES_CONFIGURATION_SET", "")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
# The reader's clock when the client record names none. Mountain because the
# CC is AltDigital (Jamie, 2026-10-14); a client's manager elsewhere gets their
# own zone from the registry's `timezone` attribute.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/Denver")

dynamodb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
sts = boto3.client("sts")
s3 = boto3.client("s3")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


# The registry attributes this function reads, and the only ones its role may
# read: 10-digest-sender.yaml grants dynamodb:Query on exactly this list
# (dynamodb:Attributes). A CLIENT record also carries the breach, security,
# change-approver and IP-owner contacts; the digest needs none of them, so it
# cannot see them. Add a name here and in the template together, or the query
# is denied.
#
# Every name goes through ExpressionAttributeNames because several (state,
# environment, client, timezone) are or may become DynamoDB reserved words, and
# a reserved word in a bare projection fails the whole query.
ACCOUNT_ATTRIBUTES = ("account_id", "kind", "partner", "client", "app", "environment", "state")
CLIENT_ATTRIBUTES = ("partner", "slug", "tech_contact", "dev_manager", "timezone")


def _projection(names):
    return {
        "ProjectionExpression": ", ".join(f"#a{i}" for i in range(len(names))),
        "ExpressionAttributeNames": {f"#a{i}": n for i, n in enumerate(names)},
    }


def _query_by_type(record_type, names):
    proj = _projection(names)
    # The key condition is also an attribute access, so record_type is named
    # through the same map and is on the template's allowed list.
    proj["ExpressionAttributeNames"]["#rt"] = "record_type"
    paginator = dynamodb.get_paginator("query")
    return paginator.paginate(
        TableName=REGISTRY_TABLE,
        IndexName="by-type",
        KeyConditionExpression="#rt = :t",
        ExpressionAttributeValues={":t": {"S": record_type}},
        **proj,
    )


def member_accounts():
    """Accounts from the registry, with their contacts.

    Only accounts the registry knows about. An account not in the registry gets
    no digest — which is correct rather than a gap: the registry entry is
    created FIRST during vesting, so an unregistered account is one nothing
    should have created.
    """
    out = []
    for page in _query_by_type("ACCOUNT", ACCOUNT_ATTRIBUTES):
        for item in page.get("Items", []):
            out.append({
                "account_id": item["account_id"]["S"],
                "kind": item.get("kind", {}).get("S", ""),
                "partner": item.get("partner", {}).get("S", ""),
                "client": item.get("client", {}).get("S", ""),
                "app": item.get("app", {}).get("S", ""),
                "environment": item.get("environment", {}).get("S", ""),
                "state": item.get("state", {}).get("S", ""),
            })
    return out


def client_contacts():
    """Contacts and time zone by (partner, client), from the CLIENT records.

    Contacts live on the client, not on the account — questionnaire 1.5 asks
    once per application, and a client's three or four environments share one
    technical contact. Reading them per account would be three lookups for one
    answer, and worse, would let the environments disagree.
    """
    out = {}
    for page in _query_by_type("CLIENT", CLIENT_ATTRIBUTES):
        for item in page.get("Items", []):
            key = (item.get("partner", {}).get("S", ""),
                   item.get("slug", {}).get("S", ""))
            out[key] = {
                "tech_contact": item.get("tech_contact", {}).get("S", ""),
                "dev_manager": item.get("dev_manager", {}).get("S", ""),
                "timezone": item.get("timezone", {}).get("S", ""),
            }
    return out


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def load_zone(name):
    """(tzinfo, zone name, note). Falls back to UTC and SAYS so.

    Deliberately unlike scheduling/src/hoop.py, which refuses to run on a zone
    it cannot load: there, a wrong clock turns an environment off. Here a
    refusal would lose the email, and the email is the evidence. UTC with a
    line in the header is wrong-but-visible; no email is wrong-and-silent.

    tzdata is bundled in the package by publish-digest.sh, so zoneinfo does
    not depend on the runtime image carrying /usr/share/zoneinfo.
    """
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name), name, None
    except Exception:                                 # noqa: BLE001
        return datetime.timezone.utc, "UTC", (
            f"Times are UTC: the time zone on record ({name}) could not be loaded.")


def fmt_time(ts, tz):
    """'2026-10-09 11:00 MDT' from a datetime or an ISO string."""
    if ts is None:
        return "unknown time"
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    local = ts.astimezone(tz)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzname()}"


# ---------------------------------------------------------------------------
# Naming resources for a reader
# ---------------------------------------------------------------------------

SERVICE_NAMES = {
    "ApiGateway": "API Gateway", "ApiGatewayV2": "API Gateway",
    "AutoScaling": "Auto Scaling", "DynamoDB": "DynamoDB", "EC2": "EC2",
    "ECS": "ECS", "EFS": "EFS", "ElastiCache": "ElastiCache",
    "ElasticLoadBalancingV2": "Load balancing", "RDS": "RDS", "S3": "S3",
    "SQS": "SQS", "SNS": "SNS", "KMS": "KMS", "IAM": "IAM",
    "ResourceExplorer2": "Resource Explorer",
}


def _words(camel):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", camel)


def type_label(resource_type):
    """'AWS::ResourceExplorer2::Index' -> 'Resource Explorer index'."""
    parts = (resource_type or "").split("::")
    if len(parts) != 3:
        return resource_type or "resource"
    service = SERVICE_NAMES.get(parts[1], _words(parts[1]))
    return f"{service} {_words(parts[2]).lower()}"


def split_resource(resource_id):
    """(short id, region or None). An ARN is noise in an email; its tail and
    region are what a reader uses to find the thing."""
    rid = resource_id or "?"
    if rid.startswith("arn:"):
        fields = rid.split(":", 5)
        region = fields[3] if len(fields) > 3 and fields[3] else None
        tail = fields[5] if len(fields) > 5 else rid
        return tail.rsplit("/", 1)[-1], region
    return rid, None


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def assume(account_id):
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{READER_ROLE}",
        RoleSessionName="platform-digest",
    )["Credentials"]
    return {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }


def gather(account_id, region, start, end):
    """Alarms and instrumentation exceptions for one account, in the window."""
    c = assume(account_id)
    cw = boto3.client("cloudwatch", region_name=region, **c)
    logs = boto3.client("logs", region_name=region, **c)

    alarms = []
    for page in cw.get_paginator("describe_alarms").paginate(
            AlarmNamePrefix="platform-auto", AlarmTypes=["MetricAlarm"]):
        for a in page.get("MetricAlarms", []):
            alarms.append({
                "name": a["AlarmName"],
                "state": a["StateValue"],
                # AlarmConfigurationUpdatedTimestamp is how "appeared in this
                # window" is determined. CloudWatch has no creation timestamp,
                # so an alarm modified in the window looks new — acceptable for
                # a digest, and drift detection is the thing that cares about
                # modification.
                "updated": a.get("AlarmConfigurationUpdatedTimestamp"),
            })

    new_alarms = [a for a in alarms if a["updated"]
                  and start.timestamp() <= a["updated"].timestamp() < end.timestamp()]

    exceptions, seen = [], set()
    try:
        for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName="/aws/platform/instrumentation-exceptions",
                startTime=int(start.timestamp() * 1000),
                endTime=int(end.timestamp() * 1000) - 1):
            for e in page.get("events", []):
                try:
                    rec = json.loads(e["message"])
                except ValueError:
                    continue
                detail = rec.get("detail") or {}
                key = (rec.get("reason"), detail.get("resource_id"), detail.get("alarm_id"))
                if key in seen:
                    # Drift re-evaluation can record the same exception twice
                    # in a day. One resource, one line.
                    continue
                seen.add(key)
                exceptions.append({
                    "reason": rec.get("reason"),
                    "resource_type": detail.get("resource_type"),
                    "resource_id": detail.get("resource_id"),
                    "alarm_id": detail.get("alarm_id"),
                    "recorded_at": rec.get("recorded_at"),
                })
    except logs.exceptions.ResourceNotFoundException:
        # The exception group is created by the instrumentation stack. Absent
        # means that stack has not reached this account — which is itself worth
        # saying in the digest rather than silently reporting zero exceptions.
        exceptions = None

    return {
        "alarms_total": len(alarms),
        "alarms_new": new_alarms,
        "alarms_in_alarm": [a["name"] for a in alarms if a["state"] == "ALARM"],
        "exceptions": exceptions,
    }


def read_facts(account_id, end):
    """(facts document or None, status). Status is what the email says.

    ok           read, and for the expected window
    missing      no document: the gatherer did not write one for this account
    unreadable   the digest cannot read Audit's bucket (a grant, not a gap)
    stale        a document for a different window than this email covers
    unconfigured FACTS_BUCKET is not set on this function
    """
    if not FACTS_BUCKET:
        return None, "unconfigured"
    key = facts_mod.facts_key(end, account_id)
    try:
        body = s3.get_object(Bucket=FACTS_BUCKET, Key=key)["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        return None, "missing" if code in ("NoSuchKey", "404") else "unreadable"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, "unreadable"
    expected_end = end.isoformat().replace("+00:00", "Z")
    if (doc.get("window") or {}).get("end") != expected_end:
        return None, "stale"
    return doc, "ok"


FACTS_STATUS_TEXT = {
    "missing": "the daily findings for this account were not produced",
    "unreadable": "the daily findings could not be read",
    "stale": "the daily findings available were for a different day",
    "unconfigured": "the daily findings are not connected yet",
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _rule(title):
    return [f"== {title} " + "=" * max(4, 64 - len(title)), ""]


def render_attention(doc, facts_status, tz):
    lines = _rule("Needs your attention")
    if doc is None:
        lines += [
            f"Not available for this window: {FACTS_STATUS_TEXT.get(facts_status, facts_status)}.",
            "AltDigital has been told. The monitoring section below is unaffected.",
            "",
        ]
        return lines

    src = doc.get("sources") or {}
    shown = False

    # Missing platform-layer tag. Tenant resources only: an untagged platform
    # resource is AltDigital's to fix and is reported to AltDigital instead.
    layer = (doc.get("layer_tag") or {}).get("tenant") or {}
    if src.get("layer") != "ok":
        lines += ["Resources missing the platform-layer tag: could not be checked today.", ""]
        shown = True
    elif layer.get("total"):
        shown = True
        lines.append(f"Resources missing a valid platform-layer tag ({layer['total']}):")
        for e in layer.get("items", [])[:25]:
            name = e.get("name") or e.get("resource_id")
            problem = ("tag missing" if e.get("problem") == "missing"
                       else f"tag value '{e.get('value')}' is not infrastructure or application")
            lines.append(f"  - {type_label(e.get('resource_type'))} {name}, "
                         f"{e.get('region') or '?'} — {problem}")
        if layer["total"] > 25:
            lines.append(f"  ... and {layer['total'] - 25} more")
        lines += [
            "  Until these carry platform-layer=application (or infrastructure),",
            "  their alarms page AltDigital rather than your team, so your team",
            "  hears about their failures second.",
            "",
        ]

    # Certificates and domains about to expire. Here rather than under the
    # exposure section because an expiry is an outage with a date on it.
    # Design 02: detection is AltDigital's under every service model; who
    # renews follows the model.
    expiring = (doc.get("exposure") or {}).get("expiring") or []
    if expiring:
        shown = True
        lines.append(f"Expiring soon ({len(expiring)}):")
        for e in expiring[:20]:
            when = fmt_time(e.get("expires"), tz)
            where = "" if e.get("where") in (None, "global") else f", {e['where']}"
            lines.append(f"  - {e.get('kind')} {e.get('name')}{where}: expires {when}, "
                         f"in {e.get('days')} days")
            lines.append(f"    {e.get('cause')}")
        lines.append("")

    if src.get("router") != "ok":
        lines += ["HIPAA eligibility and telemetry findings: could not be read today.", ""]
        shown = True
    else:
        findings = doc.get("routed_findings") or []
        for source, heading in (("platform.eligibility", "HIPAA eligibility"),
                                ("platform.telemetry", "Telemetry gaps")):
            mine = [f for f in findings if f.get("source") == source]
            if not mine:
                continue
            shown = True
            lines.append(f"{heading} ({len(mine)}):")
            for f in mine[:20]:
                # The resource, unless the title already names it (eligibility
                # titles end with the service; telemetry titles name only the
                # scenario, so the instance would otherwise be missing).
                title, res = f.get("title") or "", f.get("resource") or ""
                lines.append(f"  - {title}" + (f" — {res}" if res and res not in title else ""))
                if f.get("reason"):
                    lines.append(f"    {f['reason']}")
                seen = (f"seen {f['count']} times, last {fmt_time(f.get('last_seen'), tz)}"
                        if f.get("count", 1) > 1 else fmt_time(f.get("last_seen"), tz))
                lines.append(f"    {seen}")
            if len(mine) > 20:
                lines.append(f"  ... and {len(mine) - 20} more")
            lines.append("")

    if not shown:
        lines += ["Nothing needs your attention.", ""]
    return lines


def render_exposure(doc):
    """What in the account can be reached from the internet. ADR-015.

    Shown every day, including when empty, and never shortened silently. The
    ruling it serves is that the team may build what it likes in dev and the
    development manager decides what to do about public access. That only
    works if the manager can see the access, every day, until something is
    done about it, so a quiet section has to say what was checked.

    The wording reports and never instructs. It names the gap, and the
    decision stays with the team.
    """
    lines = _rule("Reachable from the internet")
    if doc is None:
        lines += ["Not available for this window (see above).", ""]
        return lines
    ex = doc.get("exposure")
    if not ex:
        lines += ["Not checked yet for this account.", ""]
        return lines
    status = ex.get("status") or ""
    if status.startswith("error"):
        lines += ["Could not be checked today: AltDigital could not read this account.",
                  "AltDigital has been told.", ""]
        return lines

    findings = (ex.get("findings") or {}).get("items") or []
    total = (ex.get("findings") or {}).get("total", len(findings))
    risky = [f for f in findings if f.get("risks")]
    other = [f for f in findings if not f.get("risks")]

    if not findings:
        lines.append("Nothing we check is reachable from the internet.")
    else:
        if risky:
            lines.append(f"Reachable, with no protection in front of it ({len(risky)}):")
            for f in risky[:25]:
                where = "" if f.get("where") in (None, "global") else f", {f['where']}"
                lines.append(f"  - {f.get('kind')} {f.get('name')}{where}")
                for r in f["risks"]:
                    lines.append(f"    ! {r}")
                for n in f.get("notes") or []:
                    lines.append(f"      {n}")
            if len(risky) > 25:
                lines.append(f"  ... and {len(risky) - 25} more")
            lines += [
                "",
                "  Each line above is something anyone on the internet can reach.",
                "  What to do about it is your team's decision. AltDigital reports it",
                "  here every day and will not change it.",
                "",
            ]
        if other:
            lines.append(f"Also public ({len(other)}):")
            for f in other[:25]:
                where = "" if f.get("where") in (None, "global") else f", {f['where']}"
                notes = "; ".join(f.get("notes") or [])
                lines.append(f"  - {f.get('kind')} {f.get('name')}{where}"
                             + (f" — {notes}" if notes else ""))
            if len(other) > 25:
                lines.append(f"  ... and {len(other) - 25} more")
            lines.append("")
        if total > len(findings):
            lines.append(f"(The list is capped at {len(findings)} of {total}.)")
    if ex.get("unchecked"):
        lines.append("Could not be checked today: " + ", ".join(ex["unchecked"]) + ".")
    if ex.get("not_checked"):
        lines.append("Not checked at all: " + ", ".join(ex["not_checked"]) + ".")
    lines.append("")
    return lines


def render_team(doc):
    lines = _rule("What your team did")
    lines += [
        "Not available yet. Resources created and by whom, roles created,",
        "services used for the first time, guardrail denials, and resources made",
        "by hand outside your deployment code arrive here once AltDigital's",
        "CloudTrail query is in place.",
        "",
    ]
    return lines


def render_monitoring(data, tz):
    lines = _rule("Monitoring")
    if data["alarms_new"]:
        lines.append(f"New or changed monitoring ({len(data['alarms_new'])}):")
        for a in data["alarms_new"][:50]:
            lines.append(f"  - {a['name']}")
        if len(data["alarms_new"]) > 50:
            lines.append(f"  ... and {len(data['alarms_new']) - 50} more")
    else:
        lines.append("No new monitoring was applied in this window.")
    lines.append("")

    lines.append(f"Total platform-managed alarms in this account: {data['alarms_total']}")
    if data["alarms_in_alarm"]:
        lines.append(f"Currently in ALARM ({len(data['alarms_in_alarm'])}):")
        for n in data["alarms_in_alarm"][:20]:
            lines.append(f"  - {n}")
    lines.append("")

    if data["exceptions"] is None:
        lines += [
            "NOTE: the instrumentation stack has not reached this account, so no",
            "exception data is available. This is not the same as no exceptions.",
            "",
        ]
    elif data["exceptions"]:
        lines.append(f"Running with no platform monitoring ({len(data['exceptions'])}):")
        for e in data["exceptions"][:30]:
            short, region = split_resource(e.get("resource_id") or e.get("resource_type"))
            rtype = e.get("resource_type") or "unknown type"
            lines.append(f"  - {type_label(rtype)} ({rtype}){', ' + region if region else ''}")
            lines.append(f"    {short}")
            lines.append(f"    Seen {fmt_time(e.get('recorded_at'), tz)}. "
                         "Who created it: not available yet.")
            if e.get("reason") == "unrecognised-resource-type":
                lines.append("    We have no monitoring standard for this type yet. AltDigital")
                lines.append("    has been told; no action is needed from you.")
            else:
                lines.append(f"    Reason: {e.get('reason')}")
        lines.append("")
    else:
        lines += ["Every resource we saw was instrumented. No exceptions pending.", ""]
    return lines


def render(account, data, doc, facts_status, start, end, tz, tz_name, tz_note):
    """Plain text. Deliberately.

    The recipient is a named person and the content is a short list of
    resource names. HTML buys nothing here and costs the thing that matters:
    a plain-text digest renders identically in every client, quotes cleanly
    when they reply to ask for different monitoring, and cannot silently fail
    to display.
    """
    label = "/".join(x for x in (account["partner"], account["client"],
                                 account["app"], account["environment"]) if x)
    lines = [
        f"Daily digest — {label}",
        f"AWS account {account['account_id']}",
        f"Covering {fmt_time(start, tz)} to {fmt_time(end, tz)} "
        f"({end.strftime('%H:%M')} UTC)",
        tz_note or f"Times are shown in {tz_name}.",
        "",
    ]
    lines += render_attention(doc, facts_status, tz)
    lines += render_exposure(doc)
    lines += render_team(doc)
    lines += render_monitoring(data, tz)
    lines += _rule("Cost")
    lines += ["No budget is configured for this account.", ""]
    lines += [
        "-" * 68,
        "Monitoring here is generic: it tells us a thing is running, not that",
        "it works. If any of this needs specific thresholds, or if there is a",
        "user journey whose failure we would not catch, reply to this message",
        "and we will add it.",
        "",
        "AltDigital is copied on this digest.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sending and recording
# ---------------------------------------------------------------------------


def send(to_address, subject, body, dry_run=False):
    if dry_run or DRY_RUN or not SENDER:
        print(json.dumps({"dry_run": True, "to": to_address, "subject": subject}))
        return {"MessageId": "dry-run"}

    destination = {"ToAddresses": [to_address]}
    if CC_ADDRESS:
        destination["CcAddresses"] = [CC_ADDRESS]

    kwargs = {
        "FromEmailAddress": SENDER,
        "Destination": destination,
        # Charset stated explicitly. The digest contains em-dashes and the
        # occasional non-ASCII character in a resource name, and SES does not
        # reliably infer UTF-8 — an undeclared charset renders them as mojibake
        # in the recipient's client. The first impression a client's technical
        # contact gets of this platform should not be a corrupted email.
        "Content": {"Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        }},
    }
    # The configuration set is what makes bounces observable. Without it SES
    # accepts the message, the recipient's server rejects it, and nothing here
    # ever learns — which is exactly the rotted-contact case prompt 4.4 calls a
    # finding.
    if CONFIGURATION_SET:
        kwargs["ConfigurationSetName"] = CONFIGURATION_SET
    return ses.send_email(**kwargs)


def record_evidence(account, to_address, message_id, body, meta):
    """The send record. Immutable, because it is the evidence (ADR-001)."""
    if not EVIDENCE_BUCKET:
        return None
    key = (f"digest-evidence/{account['partner']}/{account['client']}/"
           f"{account['account_id']}/{_now().strftime('%Y%m%dT%H%M%SZ')}.json")
    payload = {
        "record_type": "instrumentation-digest-sent",
        "recorded_at": _now().isoformat(),
        "account_id": account["account_id"],
        "recipient": to_address,
        "cc": CC_ADDRESS or None,
        "message_id": message_id,
        "truveon_delivered": False,
        # UTC, whatever the email showed: one unambiguous clock for evidence.
        **meta,
        "body": body,
    }
    s3.put_object(Bucket=EVIDENCE_BUCKET, Key=key,
                  Body=json.dumps(payload, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return key


def handler(event, context):
    # {"dry_run": true} on a manual invoke: read the registry, assume into
    # every account, gather and render, then send nothing and record nothing.
    # It exercises every permission the real run needs without a client
    # receiving an unscheduled email, and without an evidence record claiming
    # one was sent. The rendered bodies come back in the invoke RESPONSE, not
    # the log, so reviewing one does not copy it into a 90-day log group.
    dry_run = bool((event or {}).get("dry_run"))
    region = os.environ.get("AWS_REGION", "us-east-2")
    start, end = facts_mod.window()
    sent, skipped, failed, shared, facts_missing = [], [], [], [], []
    rendered = {}
    contacts = client_contacts()

    for account in member_accounts():
        # Shared accounts (management, audit, log archive, tooling and the
        # rest) are AltDigital's own. They have no client, so no contact, and
        # the digest's audience -- the application owner -- does not exist for
        # them. Listed in the result so the exclusion is visible, but not
        # counted as skipped: skipped feeds platform-digest-not-delivered, and
        # eight permanent skips would page every day for nothing (found on the
        # first dry run, 2026-10-07).
        #
        # Only an explicit "shared" is excluded. A record with kind missing is
        # treated as a tenant: a missing value fails toward sending, and then
        # toward a visible skip, never toward silently dropping a client.
        if account["kind"] == "shared":
            shared.append(account["account_id"])
            continue

        who = contacts.get((account["partner"], account["client"]), {})
        # The development manager first, then the technical contact. The
        # questionnaire has always said the digest goes to the development
        # manager (client form 3.6): "the main way a manager sees platform
        # activity without needing an AWS login". The fallback keeps a tenant
        # with no manager recorded from silently losing its digest.
        to_address = who.get("dev_manager") or who.get("tech_contact")
        if not to_address:
            # Recorded, not silent. A client with no contact means the digest —
            # which design doc 06 calls the documented evidence that monitoring
            # requirements were communicated — is not being sent at all.
            skipped.append({"account": account["account_id"],
                            "client": f"{account['partner']}/{account['client']}",
                            "reason": "no dev_manager or tech_contact on the client "
                                      "registry record (questionnaire 3.6 / 3.1)"})
            continue

        try:
            tz, tz_name, tz_note = load_zone(who.get("timezone") or DEFAULT_TIMEZONE)
            data = gather(account["account_id"], region, start, end)
            doc, facts_status = read_facts(account["account_id"], end)
            if facts_status != "ok":
                # Degraded, not undelivered: the email still goes, and says
                # what it is missing. Counted so the gap reaches a person.
                facts_missing.append({"account": account["account_id"],
                                      "status": facts_status})
            body = render(account, data, doc, facts_status, start, end,
                          tz, tz_name, tz_note)
            label = "/".join(x for x in (account["client"], account["app"],
                                         account["environment"]) if x)
            resp = send(to_address, f"AltDigital daily digest — {label}", body,
                        dry_run=dry_run)
            if dry_run:
                rendered[account["account_id"]] = body
                key = None
            else:
                key = record_evidence(account, to_address, resp.get("MessageId"), body, {
                    "window_start": start.isoformat(), "window_end": end.isoformat(),
                    "timezone_shown": tz_name, "facts_status": facts_status,
                })
            sent.append({"account": account["account_id"], "evidence": key,
                         "facts": facts_status})
        except Exception as exc:
            failed.append({"account": account["account_id"], "error": str(exc)})

    result = {"dry_run": dry_run, "window_end": end.isoformat(), "sent": sent,
              "skipped": skipped, "failed": failed, "facts_missing": facts_missing,
              "shared_not_digested": shared}
    print(json.dumps(result, default=str))
    # A dry run emits no counts: the alarms are about digests the client did
    # or did not receive, and a dry run sends none either way.
    if not dry_run:
        emit_metrics(len(sent), len(skipped), len(failed), len(facts_missing))
    if dry_run:
        result["rendered"] = rendered
    return result


def emit_metrics(sent, skipped, failed, facts_missing):
    """Per-run counts, as an embedded metric format log line.

    The handler catches per-account failures so one bad account cannot stop
    the rest, which means a run where every account failed still returns
    normally and Lambda's own Errors metric stays at zero. These counts are
    what the alarms in 10-digest-sender.yaml watch alongside Errors, so an
    account that was not digested, or was digested without its findings,
    reaches a person instead of only this log.

    EMF rather than PutMetricData: CloudWatch extracts the metric from the log
    line, so the role needs no cloudwatch:PutMetricData grant.
    """
    print(json.dumps({
        "_aws": {
            "Timestamp": int(_now().timestamp() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "AltDigital/Digest",
                "Dimensions": [[]],
                "Metrics": [
                    {"Name": "AccountsSent", "Unit": "Count"},
                    {"Name": "AccountsSkipped", "Unit": "Count"},
                    {"Name": "AccountsFailed", "Unit": "Count"},
                    {"Name": "AccountsFactsMissing", "Unit": "Count"},
                ],
            }],
        },
        "AccountsSent": sent,
        "AccountsSkipped": skipped,
        "AccountsFailed": failed,
        "AccountsFactsMissing": facts_missing,
    }))
