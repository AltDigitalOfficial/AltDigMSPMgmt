"""Instrumentation digest — batched, per account, to the application owner.

Prompt 4.4 and design doc 06, "The dev team notification loop":

    Here is what appeared. Here is what we instrumented generically. Reply if
    it needs something more specific.

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

So the send record matters as much as the send. Prompt 4.4 wants it in Truveon
with timestamp and recipient; Truveon does not exist, so the record is written
to the Object Lock archive instead and the Truveon hand-off is recorded as
outstanding. Losing the record would lose the evidence, which is the half that
survives the conversation.

------------------------------------------------------------------------------
Central, not per-account
------------------------------------------------------------------------------
Runs in Platform Tooling and reads each member account through a read-only
role. The alternative — a sender in every member account — needs an SES
identity in every account, which means a verified domain and a sender
reputation per tenant. One identity is not an optimisation here either: SES
reputation is per-identity, and a tenant account that starts bouncing would
otherwise damage only itself, which sounds good until you realise nobody is
watching that account's reputation.

Tooling also already holds the registry, which is where the contact addresses
live.
"""

import datetime
import json
import os

import boto3

REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "platform-registry")
SENDER = os.environ.get("DIGEST_SENDER", "")
CC_ADDRESS = os.environ.get("DIGEST_CC", "")
READER_ROLE = os.environ.get("READER_ROLE_NAME", "PlatformDigestReader")
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")
CONFIGURATION_SET = os.environ.get("SES_CONFIGURATION_SET", "")
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

dynamodb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
sts = boto3.client("sts")
s3 = boto3.client("s3")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def member_accounts():
    """Accounts from the registry, with their contacts.

    Only accounts the registry knows about. An account not in the registry gets
    no digest — which is correct rather than a gap: the registry entry is
    created FIRST during vesting, so an unregistered account is one nothing
    should have created.
    """
    out = []
    paginator = dynamodb.get_paginator("query")
    for page in paginator.paginate(
        TableName=REGISTRY_TABLE,
        IndexName="by-type",
        KeyConditionExpression="record_type = :t",
        ExpressionAttributeValues={":t": {"S": "ACCOUNT"}},
    ):
        for item in page.get("Items", []):
            out.append({
                "account_id": item["account_id"]["S"],
                "partner": item.get("partner", {}).get("S", ""),
                "client": item.get("client", {}).get("S", ""),
                "app": item.get("app", {}).get("S", ""),
                "environment": item.get("environment", {}).get("S", ""),
                "state": item.get("state", {}).get("S", ""),
            })
    return out


def client_contacts():
    """Contacts by (partner, client), from the CLIENT records.

    Contacts live on the client, not on the account — questionnaire 1.5 asks
    once per application, and a client's three or four environments share one
    technical contact. Reading them per account would be three lookups for one
    answer, and worse, would let the environments disagree.
    """
    out = {}
    paginator = dynamodb.get_paginator("query")
    for page in paginator.paginate(
        TableName=REGISTRY_TABLE,
        IndexName="by-type",
        KeyConditionExpression="record_type = :t",
        ExpressionAttributeValues={":t": {"S": "CLIENT"}},
    ):
        for item in page.get("Items", []):
            key = (item.get("partner", {}).get("S", ""),
                   item.get("slug", {}).get("S", ""))
            out[key] = {
                "tech_contact": item.get("tech_contact", {}).get("S", ""),
                "dev_manager": item.get("dev_manager", {}).get("S", ""),
            }
    return out


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


def gather(account_id, region):
    """What appeared, what was instrumented, what is pending — for one account."""
    c = assume(account_id)
    cw = boto3.client("cloudwatch", region_name=region, **c)
    logs = boto3.client("logs", region_name=region, **c)

    since = _now() - datetime.timedelta(hours=WINDOW_HOURS)
    since_ms = int(since.timestamp() * 1000)

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

    new_alarms = [a for a in alarms
                  if a["updated"] and a["updated"].timestamp() >= since.timestamp()]

    exceptions = []
    try:
        for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName="/aws/platform/instrumentation-exceptions",
                startTime=since_ms):
            for e in page.get("events", []):
                try:
                    rec = json.loads(e["message"])
                except ValueError:
                    continue
                exceptions.append({
                    "reason": rec.get("reason"),
                    "resource_type": (rec.get("detail") or {}).get("resource_type"),
                    "resource_id": (rec.get("detail") or {}).get("resource_id"),
                    "alarm_id": (rec.get("detail") or {}).get("alarm_id"),
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


def render(account, data):
    """Plain text. Deliberately.

    The recipient is a named technical contact and the content is a short list
    of resource names. HTML buys nothing here and costs the thing that matters:
    a plain-text digest renders identically in every client, quotes cleanly
    when they reply to ask for different monitoring, and cannot silently fail
    to display.
    """
    label = "/".join(x for x in (account["partner"], account["client"],
                                 account["app"], account["environment"]) if x)
    lines = [
        f"Instrumentation digest — {label}",
        f"AWS account {account['account_id']}",
        f"Covering the {WINDOW_HOURS} hours to {_now().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

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
        lines.append(f"Resources we could NOT instrument ({len(data['exceptions'])}):")
        for e in data["exceptions"][:30]:
            who = e.get("resource_id") or e.get("resource_type") or "?"
            lines.append(f"  - {who} ({e.get('resource_type')}) — {e.get('reason')}")
        lines += [
            "",
            "These are running with no platform monitoring. Usually it means a",
            "resource type we do not yet have a standard alarm set for.",
            "",
        ]
    else:
        lines += ["Every resource we saw was instrumented. No exceptions pending.", ""]

    lines += [
        "-" * 68,
        "This is generic instrumentation: it tells us a thing is running, not",
        "that it works. If any of these need specific thresholds, or if there is",
        "a user journey whose failure we would not catch, reply to this message",
        "and we will add it.",
        "",
        "AltDigital is copied on this digest.",
    ]
    return "\n".join(lines)


def send(to_address, subject, body):
    if DRY_RUN or not SENDER:
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


def record_evidence(account, to_address, message_id, body):
    """The send record. Immutable, because it is the evidence.

    Prompt 4.4 wants this in Truveon with timestamp and recipient. Truveon does
    not exist, so it goes to the Object Lock archive — which is a better home
    for it anyway, and the Truveon hand-off is recorded as outstanding rather
    than silently skipped.
    """
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
        "body": body,
    }
    s3.put_object(Bucket=EVIDENCE_BUCKET, Key=key,
                  Body=json.dumps(payload, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return key


def handler(event, context):
    region = os.environ.get("AWS_REGION", "us-east-2")
    sent, skipped, failed = [], [], []
    contacts = client_contacts()

    for account in member_accounts():
        who = contacts.get((account["partner"], account["client"]), {})
        to_address = who.get("tech_contact")
        if not to_address:
            # Recorded, not silent. A client with no contact means the digest —
            # which design doc 06 calls the documented evidence that monitoring
            # requirements were communicated — is not being sent at all. That
            # is a compliance gap, and it must be visible in this result rather
            # than inferred from a shorter `sent` list than expected.
            skipped.append({"account": account["account_id"],
                            "client": f"{account['partner']}/{account['client']}",
                            "reason": "no tech_contact on the client registry record "
                                      "(questionnaire 1.5)"})
            continue

        try:
            data = gather(account["account_id"], region)
            body = render(account, data)
            label = "/".join(x for x in (account["client"], account["app"],
                                         account["environment"]) if x)
            resp = send(to_address, f"Instrumentation digest — {label}", body)
            key = record_evidence(account, to_address, resp.get("MessageId"), body)
            sent.append({"account": account["account_id"], "evidence": key})
        except Exception as exc:
            failed.append({"account": account["account_id"], "error": str(exc)})

    result = {"sent": sent, "skipped": skipped, "failed": failed}
    print(json.dumps(result, default=str))
    return result
