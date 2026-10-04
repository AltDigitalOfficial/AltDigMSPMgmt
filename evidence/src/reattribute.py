"""Moves evidence filed under the unattributed tenant to the tenant its account
now maps to. B-156 item 8.

Why this exists
---------------
collect.py fixes a record's tenant at the moment it numbers it. Evidence from an
account absent from the tenant map is filed under the unattributed tenant so it
is not lost -- and then stays there, because nothing ever revisits it.

Onboarding makes that the normal case rather than the exception. Vesting
creates the accounts, and the map that attributes them is published three steps
later, so a newly vested account's first evidence always lands unattributed.
Arc8 left 181 records there on 2026-10-01. The unattributed tenant has no
Truveon key, so the hourly replay re-queued every one of them, each failed ten
times, and that was most of the platform's background Lambda traffic for two
days.

What a move is
--------------
A record cannot simply be relabelled. Its sequence number belongs to the
unattributed stream, and design 04's completeness argument rests on numbers
never being reused or moved: a number that changes stream is a gap in one
stream and an unexplained extra in another. So a move is:

  1. CLAIM the original row: pending -> reattributing, conditionally. A row
     that is not pending (delivered, or claimed by another run) is left alone.
  2. BUILD a new record in the target tenant's stream, with a fresh number and
     `reattributed_from` naming the original (stream, sequence, content hash,
     emitted_at). occurred_at, source, payload and control_ref are carried
     unchanged; emitted_at is now, because count reconciliation windows on
     emitted_at and both sides must count by the platform's clock.
  3. WRITE the new ledger row, before anything is queued -- collect.py's
     ordering, for collect.py's reason.
  4. FINALISE the original: reattributing -> reattributed, with
     `reattributed_to` naming the new record. Never `delivered`: it was not.
  5. QUEUE the new record for the sender.

The pointers run both ways, so either record leads an auditor to the other.

A crash between 1 and 4 leaves the original `reattributing`. That status is
reported on every later run and is NOT retried: retrying would allocate a
second copy if step 3 had in fact completed. The gap detector also counts it as
missing, so it cannot be forgotten. Resolving one is a human reading the target
stream for a record whose `reattributed_from` names it.

What is NOT moved
-----------------
A record whose account still maps to nothing. It stays pending and is reported
by account, because the fix for it is publishing the map, not moving evidence.
"""
import datetime
import json
import os

import boto3

import record

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")
sqs = boto3.client("sqs")
ssm = boto3.client("ssm")

LEDGER_TABLE = os.environ.get("EVIDENCE_LEDGER_TABLE", "")
QUEUE_URL = os.environ.get("EVIDENCE_QUEUE_URL", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
TENANT_MAP_PARAM = os.environ.get("EVIDENCE_TENANT_MAP_PARAM",
                                  "/platform/evidence/tenants")
UNATTRIBUTED_TENANT = os.environ.get("EVIDENCE_UNATTRIBUTED_TENANT",
                                     "altdigital-unattributed")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.isoformat()


def account_map():
    """account_id -> {tenant, partner}, read fresh.

    Not collect.py's cached copy: this runs precisely because the map has
    changed since those records were filed.
    """
    raw = ssm.get_parameter(Name=TENANT_MAP_PARAM)["Parameter"]["Value"]
    accounts = {}
    for tenant, facts in (json.loads(raw).get("tenants") or {}).items():
        for account_id in (facts or {}).get("accounts") or []:
            accounts[str(account_id)] = {"tenant": tenant,
                                         "partner": (facts or {}).get("partner")}
    return accounts


def stranded():
    """Every unattributed ledger row that is pending or stuck mid-move."""
    items, start = [], None
    while True:
        kwargs = {
            "TableName": LEDGER_TABLE,
            "FilterExpression": "begins_with(PK, :p) AND #s IN (:pending, :claimed)",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":p": {"S": f"{UNATTRIBUTED_TENANT}#"},
                ":pending": {"S": "pending"},
                ":claimed": {"S": "reattributing"}},
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = dynamodb.scan(**kwargs)
        items.extend(page.get("Items") or [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def _key(item):
    return {"PK": item["PK"], "SK": item["SK"]}


def _ledger_item(rec):
    """The ledger row for a new record. Same shape collect.py writes."""
    return {
        "PK": {"S": rec["stream_id"]},
        "SK": {"S": f"{rec['sequence']:012d}"},
        "sequence": {"N": str(rec["sequence"])},
        "tenant": {"S": rec["tenant"]},
        "partner": {"S": str(rec.get("partner") or "")},
        "category": {"S": rec["category"]},
        "emitted_at": {"S": rec["emitted_at"]},
        "status": {"S": "pending"},
        "content_hash": {"S": rec["content_hash"]},
        "body": {"S": json.dumps(rec, default=str)},
    }


def _emit(name, cause, detail, routing):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.evidence",
        "DetailType": "evidence-completeness",
        "Detail": json.dumps({
            "record_type": "evidence-reattribution-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": UNATTRIBUTED_TENANT},
            "assessment": {"probable_cause": cause},
            "evidence_reattribution": detail,
        }, default=str),
    }])


def move(item, target):
    """Steps 1-5 for one row. Returns the move, or None if the claim lost."""
    origin_body = json.loads(item["body"]["S"])
    claimed_at = _iso(_now())
    try:
        dynamodb.update_item(
            TableName=LEDGER_TABLE, Key=_key(item),
            UpdateExpression="SET #s = :claimed, reattribution_claimed_at = :t",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":claimed": {"S": "reattributing"},
                                       ":pending": {"S": "pending"},
                                       ":t": {"S": claimed_at}})
    except dynamodb.exceptions.ConditionalCheckFailedException:
        # Delivered or claimed since the scan. Not ours to move.
        return None

    origin = {"stream_id": item["PK"]["S"],
              "sequence": int(item["sequence"]["N"]),
              "content_hash": origin_body.get("content_hash"),
              "emitted_at": origin_body.get("emitted_at")}
    new = record.build(
        tenant=target["tenant"], partner=target.get("partner"),
        category=origin_body["category"], source=origin_body.get("source"),
        payload=origin_body.get("payload"),
        control_ref=origin_body.get("control_ref"),
        occurred_at=origin_body.get("occurred_at"),
        account_id=origin_body.get("account_id"),
        reattributed_from=origin)

    dynamodb.put_item(TableName=LEDGER_TABLE, Item=_ledger_item(new),
                      ConditionExpression="attribute_not_exists(PK)")

    dynamodb.update_item(
        TableName=LEDGER_TABLE, Key=_key(item),
        UpdateExpression=("SET #s = :done, reattributed_to = :to, "
                          "reattributed_at = :t"),
        ConditionExpression="#s = :claimed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":done": {"S": "reattributed"},
            ":claimed": {"S": "reattributing"},
            ":to": {"S": f"{new['stream_id']}#{new['sequence']:012d}"},
            ":t": {"S": _iso(_now())}})

    if QUEUE_URL:
        sqs.send_message(QueueUrl=QUEUE_URL,
                         MessageBody=json.dumps(new, default=str))

    return {**origin, "to_stream": new["stream_id"],
            "to_sequence": new["sequence"]}


def reattribute(event=None, context=None):
    """Move every stranded record whose account now maps to a tenant.

    event: {"dry_run": true} reports what would move and writes nothing.
    """
    dry_run = bool((event or {}).get("dry_run"))
    if not LEDGER_TABLE:
        raise RuntimeError("EVIDENCE_LEDGER_TABLE is unset. Nothing was read.")
    accounts = account_map()

    moved, would_move, unmapped, stuck, lost_claim = [], [], {}, [], 0
    for item in stranded():
        body = json.loads(item["body"]["S"])
        where = {"stream_id": item["PK"]["S"],
                 "sequence": int(item["sequence"]["N"])}
        if item["status"]["S"] == "reattributing":
            stuck.append(where)
            continue
        account = str(body.get("account_id") or "")
        target = accounts.get(account)
        if target is None or target["tenant"] == UNATTRIBUTED_TENANT:
            unmapped[account or "(none)"] = unmapped.get(account or "(none)", 0) + 1
            continue
        if dry_run:
            would_move.append({**where, "account_id": account,
                               "to_tenant": target["tenant"]})
            continue
        result = move(item, target)
        if result is None:
            lost_claim += 1
        else:
            moved.append(result)

    out = {"record_type": "evidence-reattribution", "dry_run": dry_run,
           "moved": len(moved), "would_move": len(would_move),
           "unmapped_by_account": unmapped, "stuck": stuck,
           "lost_claim": lost_claim}

    if not dry_run and moved:
        by_stream = {}
        for m in moved:
            by_stream[m["to_stream"]] = by_stream.get(m["to_stream"], 0) + 1
        out["moved_to"] = by_stream
        # The system healing, reported so it is visible: digest, not a ticket.
        _emit("platform-evidence-reattributed",
              (f"{len(moved)} evidence record(s) filed under "
               f"{UNATTRIBUTED_TENANT} were moved to the tenant their account "
               "now maps to, each with a fresh sequence number and a pointer "
               "back to the original."),
              {"moved_to": by_stream}, routing="digest")
    if stuck and not dry_run:
        _emit("platform-evidence-reattribution-incomplete",
              (f"{len(stuck)} record(s) were claimed for re-attribution and "
               "never finalised. Not retried: the new record may exist. Check "
               "the target stream for a record whose reattributed_from names "
               "each one."),
              {"stuck": stuck}, routing="digest_and_jira")

    print(json.dumps(out, default=str))
    return out


def handler(event, context):
    return reattribute(event, context)
