"""Delivers evidence to Truveon, and refuses to lie about having done so.

Prompt 9.1. The contract is docs/truveon-ingestion-contract.md, written from
this side because AltDigital builds both ends -- so the API is set by what
design 04's completeness controls require rather than discovered from whatever
the endpoint happens to do. B-088.

The one thing this module must never do
---------------------------------------
Mark a record delivered when it is not.

A ledger row marked `delivered` is excluded from gap detection permanently.
That is the whole point of the ledger: it is how the platform stops looking.
So a premature acknowledgement does not merely lose a record -- it loses it
AND removes the mechanism that would have noticed. The stream reads clean, the
count reconciliation reads clean, and the evidence is gone.

Which is why, with no endpoint configured, this does NOT quietly succeed. It
leaves records pending and says the pipeline is not delivering. A stub that
acknowledged locally would reproduce design 04's "clean month" inside the
component built to prevent it:

    A silently failed pipeline that looks like a clean month is the failure
    mode that destroys an audit.

Retry is safe because ingest is idempotent
-------------------------------------------
The contract requires `POST /v1/evidence` to be idempotent on
`(stream_id, sequence)`: re-posting a sequence Truveon already holds is a
success. Without that, every retry -- after a timeout, a 5xx, or a crash
between sending and recording the send -- would create a duplicate, and the
count reconciliation would report a mismatch on an intact stream.

A 409 is different and is NOT retried. It means Truveon holds that sequence
with a different content hash, so one of the two stores is wrong and neither
side should guess which. That is a finding for a human.
"""
import datetime
import json
import os
import urllib.error
import urllib.request

import boto3

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")
secrets = boto3.client("secretsmanager")
sqs = boto3.client("sqs")

LEDGER_TABLE = os.environ.get("EVIDENCE_LEDGER_TABLE", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
TRUVEON_ENDPOINT = os.environ.get("TRUVEON_ENDPOINT", "").rstrip("/")
TRUVEON_SECRET = os.environ.get("TRUVEON_SECRET_NAME",
                                "platform/truveon/ingest")
TIMEOUT = int(os.environ.get("TRUVEON_TIMEOUT_SECONDS", "20"))
QUEUE_URL = os.environ.get("EVIDENCE_QUEUE_URL", "")
# Matches the gap detector's grace. The two must agree: a record replayed
# before it is considered a gap is wasted work, and one considered a gap before
# it is replayed is a finding raised against something already being fixed.
GRACE_MINUTES = int(os.environ.get("EVIDENCE_INFLIGHT_GRACE_MINUTES", "60"))

_AUTH = {}


class NotDelivered(Exception):
    """The record was not persisted by Truveon. Retryable."""


class ContentConflict(Exception):
    """Truveon holds this sequence with different content. NOT retryable."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _token():
    """Bearer token, cached for the execution environment.

    One credential for all tenants, not one per tenant. The `tenant` field
    carries the isolation and Truveon enforces it server-side.

    Per-tenant credentials sound stronger and are worse here: the platform
    would hold and rotate N secrets, and the failure mode is that one expires
    quietly and that tenant's evidence stops while every other tenant looks
    fine. One credential fails loudly for everybody, which is the direction
    this codebase keeps choosing.
    """
    if "token" not in _AUTH:
        raw = secrets.get_secret_value(SecretId=TRUVEON_SECRET)["SecretString"]
        try:
            _AUTH["token"] = json.loads(raw)["token"]
        except (ValueError, KeyError):
            # Tolerated: a bare token string rather than JSON. The secret is
            # set by hand and this is the likely shape of a mistake.
            _AUTH["token"] = raw.strip()
    return _AUTH["token"]


def _post(body):
    """One ingest call. Returns on persistence, raises otherwise."""
    req = urllib.request.Request(
        f"{TRUVEON_ENDPOINT}/v1/evidence",
        data=json.dumps(body, default=str).encode("utf-8"),
        headers={"Authorization": f"Bearer {_token()}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:                          # noqa: BLE001
            pass
        if exc.code == 409:
            raise ContentConflict(
                f"Truveon holds {body['stream_id']} sequence "
                f"{body['sequence']} with different content: {detail}. "
                "One of the two stores is wrong and the platform will not "
                "guess which.") from exc
        raise NotDelivered(
            f"Truveon returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotDelivered(f"could not reach Truveon: {exc}") from exc

    # The contract says 200 means persisted. Verify it SAYS so rather than
    # inferring persistence from the status code alone -- a proxy, a gateway or
    # a misconfigured stub can return 200 for something that never reached a
    # datastore, and this acknowledgement is what stops the platform looking.
    try:
        answer = json.loads(payload) if payload else {}
    except ValueError:
        raise NotDelivered(
            f"Truveon returned 200 with a body that is not JSON: "
            f"{payload[:200]}")
    if answer.get("stored") is not True:
        raise NotDelivered(
            "Truveon returned 200 but did not confirm storage "
            f"(stored={answer.get('stored')!r}). Treated as undelivered: an "
            "acknowledgement that does not mean persisted is one that would "
            "stop the platform looking for a record Truveon may still drop.")
    return answer


def _mark_delivered(stream_id, sequence):
    dynamodb.update_item(
        TableName=LEDGER_TABLE,
        Key={"PK": {"S": stream_id}, "SK": {"S": f"{sequence:012d}"}},
        UpdateExpression="SET #s = :d, delivered_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": {"S": "delivered"},
                                   ":t": {"S": _iso(_now())}},
        # Only a row that EXISTS may be marked delivered. Creating one here
        # would invent a delivery for a record the ledger never knew about,
        # which is a gap being papered over rather than closed.
        ConditionExpression="attribute_exists(PK)")


def _emit(name, cause, detail, routing="review_queue"):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.evidence",
        "DetailType": "evidence-completeness",
        "Detail": json.dumps({
            "record_type": "evidence-delivery-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name,
                      "resource_id": detail.get("stream_id", "evidence")},
            "assessment": {"probable_cause": cause},
            "evidence_delivery": detail,
        }, default=str),
    }])


def send(event, context=None):
    """Deliver one batch. Invoked by SQS, or directly with {"records": [...]}.

    Returns the SQS partial-batch shape so that a failure in one record redrives
    only that record. Failing the whole batch would re-deliver records already
    persisted -- harmless, because ingest is idempotent, but it would also
    delay every healthy record behind one poisoned one.
    """
    records = event.get("records")
    if records is None:
        records = [json.loads(r["body"]) for r in (event.get("Records") or [])]

    if not TRUVEON_ENDPOINT:
        # Deliberately not a silent success. See the module docstring: records
        # stay pending, the gap detector will see them age past the grace
        # period, and the pipeline reports that it is not delivering.
        _emit("platform-evidence-no-endpoint",
              (f"{len(records)} evidence record(s) could not be delivered: "
               "TRUVEON_ENDPOINT is not configured, so Truveon does not exist "
               "for this deployment yet (B-088). The records remain buffered "
               "and pending -- they are NOT lost, and they are not marked "
               "delivered. Set the endpoint and the backlog drains."),
              {"pending": len(records)})
        out = {"record_type": "evidence-delivery", "delivering": False,
               "pending": len(records), "reason": "no endpoint configured"}
        print(json.dumps(out, default=str))
        # Raising would redrive the batch forever and eventually DLQ it. The
        # records are already durable in the ledger; the queue has done its
        # job by handing them over, and the LEDGER is what remembers they are
        # undelivered.
        return out

    delivered, conflicts, failures = 0, [], []
    for body in records:
        stream_id = body.get("stream_id")
        sequence = body.get("sequence")
        try:
            _post(body)
            _mark_delivered(stream_id, sequence)
            delivered += 1
        except ContentConflict as exc:
            # Not retried. Retrying cannot resolve a disagreement about
            # content, and burning the redrive budget on it would hide it.
            conflicts.append({"stream_id": stream_id, "sequence": sequence,
                              "error": str(exc)})
            _emit("platform-evidence-content-conflict", str(exc),
                  {"stream_id": stream_id, "sequence": sequence},
                  routing="digest_and_jira")
        except NotDelivered as exc:
            failures.append({"stream_id": stream_id, "sequence": sequence,
                             "error": str(exc)})
        except Exception as exc:                   # noqa: BLE001
            failures.append({"stream_id": stream_id, "sequence": sequence,
                             "error": f"{type(exc).__name__}: {exc}"})

    out = {"record_type": "evidence-delivery", "delivering": True,
           "delivered": delivered, "conflicts": len(conflicts),
           "failed": len(failures)}
    print(json.dumps(out, default=str))

    # Partial batch failure, so only the undelivered records redrive. A
    # conflict is NOT returned here: it will not succeed on retry and belongs
    # in front of a person instead.
    if failures and event.get("Records"):
        by_seq = {}
        for r in event["Records"]:
            b = json.loads(r["body"])
            by_seq[(b.get("stream_id"), b.get("sequence"))] = r["messageId"]
        out["batchItemFailures"] = [
            {"itemIdentifier": by_seq[(f["stream_id"], f["sequence"])]}
            for f in failures
            if (f["stream_id"], f["sequence"]) in by_seq]
    return out


def replay_pending(event=None, context=None):
    """Re-queue ledger rows that are still pending past the grace period.

    This is what closes the loop, and the pipeline is incomplete without it.

    The queue is a transport, not the record. Once `send` has taken a message
    it is gone from SQS whether or not delivery succeeded -- including the case
    where no endpoint is configured at all and nothing was even attempted. The
    LEDGER is what remembers a record is owed, so something has to read the
    ledger and put it back on the queue.

    Without this, the gap detector would find a hole, report it correctly, and
    nobody would have any way to fill it: the finding would be permanent and
    the evidence permanently missing. A detection with no remedy is only half a
    control.

    Safe to run repeatedly because ingest is idempotent on
    (stream_id, sequence) -- a record replayed after it was in fact delivered
    is accepted and changes nothing. That is the contract requirement in
    docs/truveon-ingestion-contract.md earning its place: without it, replay
    would manufacture duplicates and the count reconciliation would then report
    a mismatch on a stream this function had just repaired.
    """
    if not LEDGER_TABLE or not QUEUE_URL:
        out = {"record_type": "evidence-replay", "ran": False,
               "reason": "ledger table or queue not configured"}
        print(json.dumps(out, default=str))
        return out

    cutoff = _now() - datetime.timedelta(minutes=GRACE_MINUTES)
    requeued, scanned = 0, 0
    paginator = dynamodb.get_paginator("scan")
    for page in paginator.paginate(
            TableName=LEDGER_TABLE,
            FilterExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":p": {"S": "pending"}}):
        for item in page.get("Items") or []:
            scanned += 1
            emitted = item.get("emitted_at", {}).get("S")
            if emitted and emitted > _iso(cutoff):
                # Still in flight. Re-queueing now would duplicate work for
                # every healthy record on every run.
                continue
            body = item.get("body", {}).get("S")
            if not body:
                continue
            sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=body)
            requeued += 1

    out = {"record_type": "evidence-replay", "ran": True,
           "pending_scanned": scanned, "requeued": requeued}
    if requeued:
        # Not a finding in itself -- replay working is the system healing. It
        # is reported so that a replay running every cycle, which means
        # delivery is failing persistently rather than transiently, is visible
        # rather than looking like quiet success.
        _emit("platform-evidence-replayed",
              (f"{requeued} evidence record(s) were re-queued for delivery "
               "after sitting undelivered past the grace period. One cycle of "
               "this is recovery; every cycle means delivery is failing and "
               "the replay is masking it."),
              {"requeued": requeued}, routing="digest")
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"send": send, "replay_pending": replay_pending}


def handler(event, context):
    # SQS batches arrive with Records and no action; schedules name one.
    action = (event or {}).get("action")
    if action and action in HANDLERS:
        return HANDLERS[action](event, context)
    return send(event, context)
