"""Evidence that the raw logs arrived -- without sending the raw logs anywhere.

design 04:

    Truveon holds derived findings and correlated evidence. Raw logs remain in
    the Log Archive account under Object Lock. An auditor may want the raw
    record; know which store answers which question.

That leaves a question neither store answered: were the logs actually there?
An archive that silently stopped receiving CloudTrail from one account looks,
from Truveon, exactly like an account with nothing to report. This module
closes that: once an hour, for every account and every log source, it writes a
`log-custody` evidence record saying what landed in the archive for that hour
-- object count, bytes, first and last file, and a digest of the listing -- and
the ordinary pipeline delivers it. Decided 2026-09-23 as the near-term answer
to "all of a tenant's logs into Truveon": custody evidence now, raw-log access
only once Truveon has a reader for it.

What the listing digest does and does not prove
-----------------------------------------------
It is sha256 over the sorted (key, size, ETag) of every object in the hour. It
proves WHICH objects existed and that none has since been replaced -- a later
listing that disagrees is a finding. It does NOT prove what is inside them:
the bucket holds SSE-KMS objects, whose ETags are not content hashes, and this
role deliberately cannot read object bodies (s3:ListBucket only). Content
integrity has other owners and the record says which:

    cloudtrail   CloudTrail's own signed digest chain (log file validation is
                 on), plus Object Lock COMPLIANCE on the bucket. The record
                 reports whether each region's hourly DIGEST FILE arrived.
    config       Nothing equivalent. The config archive is versioned and
                 delete-denied but NOT immutable -- AWS Config cannot write to
                 an Object Lock bucket (security/10-log-archive.yaml). The
                 record carries `immutable: false` rather than implying
                 otherwise.

A missing digest is a finding; a quiet hour is not
--------------------------------------------------
CloudTrail delivers a digest file for every region every hour whether or not
anything happened. So an absent digest means the trail stopped, or delivery
did, and it is raised. Zero LOG files in an hour is ordinary -- a quiet region
writes none -- and is recorded without being raised. Config writes on its own
schedule (history every six hours, snapshots when asked), so a zero there is
never a finding either.

Hours are closed and self-healing
---------------------------------
An hour is processed only once it is LAG_HOURS old, because the digest for
hour H arrives roughly an hour after H ends. Each run then covers the last
BACKFILL_HOURS as well, skipping any (account, source, hour) already written,
so a failed or throttled run leaves a delay rather than a permanent hole -- the
same property the evidence pipeline asks of Truveon.

The done-marker is written AFTER the ledger row. A crash between the two
produces a second custody record for that hour on the next run, which is
visible and harmless (both carry the hour). The other order would turn the same
crash into a silently missing hour, which is the failure this module exists to
prevent.
"""
import datetime
import hashlib
import json
import os
import re

import boto3

import record

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")
s3 = boto3.client("s3")
sqs = boto3.client("sqs")
ssm = boto3.client("ssm")

LEDGER_TABLE = os.environ.get("EVIDENCE_LEDGER_TABLE", "")
SEQUENCE_TABLE = os.environ.get("EVIDENCE_SEQUENCE_TABLE", "")
QUEUE_URL = os.environ.get("EVIDENCE_QUEUE_URL", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
TENANT_MAP_PARAM = os.environ.get("EVIDENCE_TENANT_MAP_PARAM", "/platform/evidence/tenants")
LOG_ARCHIVE_BUCKET = os.environ.get("LOG_ARCHIVE_BUCKET", "")
CONFIG_ARCHIVE_BUCKET = os.environ.get("CONFIG_ARCHIVE_BUCKET", "")
ORGANIZATION_ID = os.environ.get("ORGANIZATION_ID", "")
REGIONS = [r.strip() for r in os.environ.get("LOG_REGIONS", "").split(",") if r.strip()]
LAG_HOURS = int(os.environ.get("CUSTODY_LAG_HOURS", "3"))
BACKFILL_HOURS = int(os.environ.get("CUSTODY_BACKFILL_HOURS", "12"))

CATEGORY = "log-custody"
CONTROL_REF = "log-custody"

# The timestamp AWS writes into every CloudTrail and Config object name:
#   123456789012_CloudTrail_us-east-2_20260923T1405Z_abc.json.gz
#   123456789012_CloudTrail-Digest_us-east-2_trail_us-east-2_20260923T140512Z.json.gz
#   123456789012_Config_us-east-2_ConfigHistory_AWS::S3::Bucket_20260923T140512Z_20260923T200512Z_1.json.gz
# The FIRST match is the file's own time. Config history names carry a second
# (window end) timestamp, which is why this is not "the last match".
_STAMP = re.compile(r"_(\d{8})T(\d{2})(\d{2})(\d{2})?Z")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _hour_of(key, fallback):
    m = _STAMP.search(key.rsplit("/", 1)[-1])
    if not m:
        return fallback.replace(minute=0, second=0, microsecond=0)
    return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H").replace(
        tzinfo=datetime.timezone.utc)


def _prefixes(source, account, region, hour):
    """Every prefix that can hold this hour's objects. One day folder per source.

    CloudTrail pads month and day; Config does not. Getting that wrong would not
    raise -- it would list an empty folder and report a silent hour -- which is
    why both layouts are spelled out rather than formatted from one template.
    """
    if source == "cloudtrail":
        day = hour.strftime("%Y/%m/%d")
        base = f"AWSLogs/{ORGANIZATION_ID}/{account}"
        return [f"{base}/CloudTrail/{region}/{day}/", f"{base}/CloudTrail-Digest/{region}/{day}/"]
    if source == "config":
        day = f"{hour.year}/{hour.month}/{hour.day}"
        return [f"AWSLogs/{account}/Config/{region}/{day}/"]
    raise ValueError(source)


SOURCES = {
    # source: (bucket env, immutable)
    "cloudtrail": (lambda: LOG_ARCHIVE_BUCKET, True),
    "config": (lambda: CONFIG_ARCHIVE_BUCKET, False),
}


def _list(bucket, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            yield obj


def observe(source, account, hour):
    """What the archive holds for one account, one source, one hour."""
    bucket_fn, immutable = SOURCES[source]
    bucket = bucket_fn()
    regions, listing = {}, []
    for region in REGIONS:
        entry = {"objects": 0, "bytes": 0, "first": None, "last": None}
        if source == "cloudtrail":
            entry["digests"] = 0
        for prefix in _prefixes(source, account, region, hour):
            is_digest = "/CloudTrail-Digest/" in prefix
            for obj in _list(bucket, prefix):
                if _hour_of(obj["Key"], obj["LastModified"]) != hour:
                    continue
                listing.append(f"{obj['Key']}|{obj['Size']}|{obj.get('ETag', '')}")
                if is_digest:
                    entry["digests"] += 1
                    continue
                entry["objects"] += 1
                entry["bytes"] += int(obj["Size"])
                stamp = obj["LastModified"].isoformat()
                entry["first"] = min(filter(None, [entry["first"], stamp]))
                entry["last"] = max(filter(None, [entry["last"], stamp]))
        regions[region] = entry

    payload = {
        "hour": hour.isoformat(),
        "source": source,
        "account_id": account,
        "bucket": bucket,
        "immutable": immutable,
        "regions": regions,
        "objects": sum(r["objects"] for r in regions.values()),
        "bytes": sum(r["bytes"] for r in regions.values()),
        "listing_sha256": hashlib.sha256("\n".join(sorted(listing)).encode()).hexdigest(),
        "listed_objects": len(listing),
    }
    if source == "cloudtrail":
        payload["digest_regions_missing"] = sorted(r for r, e in regions.items() if e["digests"] == 0)
    return payload


def _done(marker):
    got = dynamodb.get_item(TableName=SEQUENCE_TABLE, Key={"stream_id": {"S": marker}},
                            ConsistentRead=True)
    return "Item" in got


def _mark(marker, sequence):
    # In the sequence table because it is the one place already keyed on a
    # single string and read by nothing that scans. A marker in the ledger
    # would be visible to replay and gap detection, which have no use for it.
    dynamodb.put_item(TableName=SEQUENCE_TABLE, Item={
        "stream_id": {"S": marker}, "custody_sequence": {"N": str(sequence)},
        "updated_at": {"S": _now().isoformat()}})


def _write(item):
    """Ledger first, then queue -- collect.py's ordering and reason."""
    dynamodb.put_item(
        TableName=LEDGER_TABLE,
        Item={
            "PK": {"S": item["stream_id"]}, "SK": {"S": f"{item['sequence']:012d}"},
            "sequence": {"N": str(item["sequence"])}, "tenant": {"S": item["tenant"]},
            "partner": {"S": str(item.get("partner") or "")}, "category": {"S": item["category"]},
            "emitted_at": {"S": item["emitted_at"]}, "status": {"S": "pending"},
            "content_hash": {"S": item["content_hash"]}, "body": {"S": json.dumps(item, default=str)},
        },
        ConditionExpression="attribute_not_exists(PK)")
    if QUEUE_URL:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(item, default=str))


def _emit(name, cause, detail, routing="review_queue"):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.evidence",
        "DetailType": "evidence-completeness",
        "Detail": json.dumps({
            "record_type": "log-custody-finding",
            "recorded_at": _now().isoformat(),
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": detail.get("account_id") or "log-archive"},
            "assessment": {"probable_cause": cause},
            "log_custody": detail,
        }, default=str),
    }])


def _accounts():
    """account -> (tenant, partner), from the same map the collector uses.

    Raises rather than returning empty: a custody run over zero accounts writes
    nothing and reports success, which is indistinguishable from an archive in
    perfect health. B-086.
    """
    data = json.loads(ssm.get_parameter(Name=TENANT_MAP_PARAM)["Parameter"]["Value"])
    out = {}
    for tenant, facts in (data.get("tenants") or {}).items():
        for account in (facts or {}).get("accounts") or []:
            out[str(account)] = (tenant, (facts or {}).get("partner"))
    if not out:
        raise RuntimeError(f"{TENANT_MAP_PARAM} lists no accounts; custody cannot check nothing.")
    return out


def run(event=None, context=None):
    missing_config = [n for n, v in (("LOG_ARCHIVE_BUCKET", LOG_ARCHIVE_BUCKET),
                                     ("CONFIG_ARCHIVE_BUCKET", CONFIG_ARCHIVE_BUCKET),
                                     ("ORGANIZATION_ID", ORGANIZATION_ID),
                                     ("LOG_REGIONS", REGIONS),
                                     ("EVIDENCE_LEDGER_TABLE", LEDGER_TABLE),
                                     ("EVIDENCE_SEQUENCE_TABLE", SEQUENCE_TABLE)) if not v]
    if missing_config:
        # Loud, and a finding: a custody job that cannot run is the archive
        # going unwatched, not a configuration detail.
        _emit("platform-evidence-log-custody-unconfigured",
              f"Log custody did not run: {', '.join(missing_config)} not set.", {})
        raise RuntimeError(f"log custody is not configured: {', '.join(missing_config)}")

    accounts = _accounts()
    newest = (_now() - datetime.timedelta(hours=LAG_HOURS)).replace(minute=0, second=0, microsecond=0)
    hours = [newest - datetime.timedelta(hours=i) for i in range(BACKFILL_HOURS)]

    written, skipped, gaps, failed = 0, 0, [], []
    for hour in sorted(hours):
        for account, (tenant, partner) in sorted(accounts.items()):
            for source in SOURCES:
                marker = f"custody#{source}#{account}#{hour.strftime('%Y-%m-%dT%H')}"
                try:
                    if _done(marker):
                        skipped += 1
                        continue
                    payload = observe(source, account, hour)
                    item = record.build(
                        tenant=tenant, partner=partner, category=CATEGORY, source=source,
                        payload=payload, control_ref=CONTROL_REF,
                        occurred_at=hour.isoformat(), account_id=account)
                    _write(item)
                    _mark(marker, item["sequence"])
                    written += 1
                    if payload.get("digest_regions_missing"):
                        gaps.append({"account_id": account, "hour": hour.isoformat(),
                                     "regions": payload["digest_regions_missing"]})
                except Exception as exc:              # noqa: BLE001
                    # One account failing must not stop the rest; it is retried
                    # on the next run because no marker was written.
                    failed.append({"marker": marker, "error": f"{type(exc).__name__}: {exc}"[:300]})

    if gaps:
        _emit("platform-evidence-log-custody-gap",
              (f"{len(gaps)} account-hour(s) are missing a CloudTrail digest file in at least one "
               "region. CloudTrail writes a digest every hour for every region whether or not "
               "anything happened, so an absence means the trail or its delivery stopped for that "
               "account and region."),
              {"gaps": gaps[:50], "gap_count": len(gaps)})
    if failed:
        _emit("platform-evidence-log-custody-failed",
              (f"{len(failed)} custody observation(s) failed and will be retried next run. "
               "Persisting failures mean an hour of an account's archive is going unattested."),
              {"failed": failed[:20], "failed_count": len(failed)}, routing="digest")

    out = {"record_type": "log-custody-run", "hours": len(hours), "accounts": len(accounts),
           "written": written, "skipped": skipped, "digest_gaps": len(gaps), "failed": len(failed)}
    print(json.dumps(out, default=str))
    return out


def handler(event, context):
    return run(event, context)
