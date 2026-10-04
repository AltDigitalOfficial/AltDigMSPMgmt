"""Turns platform events into numbered evidence. Prompt 9.1.

design/04-security-controls-and-evidence.md, "What flows":

    1. Config compliance state -- continuous control evidence
    2. Security Hub findings -- normalised, severity-scored
    3. CloudTrail-derived change events -- reconciled against the change register
    4. Operational evidence -- restore test results, instrumentation actions,
       remediation outcomes, JIT elevation records, notification timestamps

All four already exist and already flow onto the organization alert bus. This
module does not produce evidence; it recognises it, numbers it, records that it
is owed to Truveon, and hands it to a queue.

The ledger row is written BEFORE the queue send
------------------------------------------------
Same ordering, and the same reason, as execute.py writing its attempt before
performing a quorum action.

If the queue send came first, a crash between the two would leave a record in
flight that the ledger has no row for. It would be delivered, or not, and
either way nothing would ever notice its absence -- the gap detector can only
see holes in a sequence it knows was allocated.

Writing the ledger first means a crash leaves a `pending` row with no message.
That is visible: the row ages past the in-flight grace and is reported as a
gap. A false gap costs somebody ten minutes; a silent loss costs an audit.

An account nobody can attribute is a FINDING, not a skipped record
------------------------------------------------------------------
Evidence arriving from an account absent from the tenant map cannot be filed
against a tenant, and design 15's carve-out means it cannot be filed against a
partner either. The tempting behaviour is to drop it and move on, which would
mean a newly vested account producing evidence that silently goes nowhere --
B-064's missing Forensics account and B-086's empty sweep, in a third costume.

It is queued under the platform's own tenant and raised, so the evidence is
kept and the attribution gap is visible.
"""
import json
import os
import time

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
# Where evidence goes when its account cannot be attributed to a tenant. Not
# discarded; parked somewhere an auditor and an engineer can both find it.
UNATTRIBUTED_TENANT = os.environ.get("EVIDENCE_UNATTRIBUTED_TENANT",
                                     "altdigital-unattributed")

_MAP = {}

# A miss re-reads the map, at most this often, before filing as unattributed.
# The router's tier map had the same bug and the same fix (router.py,
# TIER_MAP_MISS_REFRESH_SECONDS).
TENANT_MAP_MISS_REFRESH_SECONDS = int(
    os.environ.get("EVIDENCE_TENANT_MAP_MISS_REFRESH_SECONDS", "60"))


# How an event on the alert bus becomes one of design 04's four categories.
#
# Matched on detail-type and source rather than on the shape of the payload:
# the shape changes when an upstream module is edited, and a recogniser that
# reads shapes fails silently when it does. A detail-type that stops matching
# produces an UNRECOGNISED event, which is reported.
RECOGNISERS = (
    # (needle, source_prefix, category, control_ref)
    #
    # Sources taken from what the emitters ACTUALLY publish, not from what a
    # reasonable naming scheme would suggest. The first version of this table
    # guessed `altdigital.change` and `altdigital.quorum`; the codebase uses
    # `platform.*` throughout, so five real reconciliation findings were
    # emitted and none were collected. B-089.
    ("Config Rules Compliance Change", "aws.config",
     "config-compliance", "config-continuous-evaluation"),
    ("Security Hub Findings", "aws.securityhub",
     "security-finding", "security-finding-triage"),
    ("GuardDuty Finding", "aws.guardduty",
     "security-finding", "threat-detection"),
    ("change-reconciliation", "platform.change",
     "change-event", "change-management-reconciliation"),
    ("quorum-reverse", "platform.quorum",
     "change-event", "quorum-two-party-control"),
    ("Platform Incident Dossier", "platform.response",
     "operational", "security-incident-response"),
    ("Platform Incident Dossier", "platform.security",
     "operational", "security-incident-response"),
    ("", "platform.recovery",
     "operational", "backup-restore-verification"),
    ("", "platform.exercise",
     "operational", "containment-exercise"),
)


def _map_age():
    loaded = _MAP.get("loaded_at")
    return time.monotonic() - loaded if loaded is not None else float("inf")


def attribution_for(account_id):
    """{tenant, partner} for an account, or None.

    A MISS re-reads the map before concluding the account is unknown. The map
    used to be cached for the life of the execution environment, so an account
    published after the container warmed kept filing as unattributed until AWS
    recycled the container -- and every one of those records was then stranded
    (B-156 item 8). Rate-limited, because an account that is genuinely
    unmapped would otherwise cost an SSM read per event.
    """
    accounts = tenant_map()
    if account_id not in accounts and _map_age() >= TENANT_MAP_MISS_REFRESH_SECONDS:
        _MAP.clear()
        accounts = tenant_map()
    return accounts.get(account_id)


def tenant_map():
    """account_id -> {tenant, partner}. Cached; see attribution_for."""
    if "accounts" not in _MAP:
        raw = ssm.get_parameter(Name=TENANT_MAP_PARAM)["Parameter"]["Value"]
        data = json.loads(raw)
        accounts = {}
        for tenant, facts in (data.get("tenants") or {}).items():
            for account_id in (facts or {}).get("accounts") or []:
                accounts[str(account_id)] = {
                    "tenant": tenant, "partner": (facts or {}).get("partner")}
        _MAP["accounts"] = accounts
        _MAP["loaded_at"] = time.monotonic()
    return _MAP["accounts"]


def classify(event):
    """Which of design 04's categories this is, or None."""
    detail_type = event.get("detail-type") or ""
    source = event.get("source") or ""
    detail = event.get("detail") or {}
    # Platform-emitted events carry their own record_type, which is more
    # specific than the detail-type they share.
    record_type = detail.get("record_type") or ""
    for needle, src, category, control_ref in RECOGNISERS:
        if not source.startswith(src):
            continue
        if needle in detail_type or needle in record_type:
            return category, control_ref
    return None, None


def _emit(name, cause, detail, routing="review_queue"):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "platform.evidence",
        "DetailType": "evidence-completeness",
        "Detail": json.dumps({
            "record_type": "evidence-collection-finding",
            "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": detail.get("account_id")
                      or "evidence"},
            "assessment": {"probable_cause": cause},
            "evidence_collection": detail,
        }, default=str),
    }])


def collect(event, context=None):
    """One event in, one numbered evidence record buffered."""
    category, control_ref = classify(event)
    account_id = str(event.get("account") or
                     (event.get("detail") or {}).get("account_id") or "")

    if category is None:
        # NOT silently ignored. The alert bus carries things that are not
        # evidence, so this is often correct -- but a recogniser that stops
        # matching because an upstream detail-type changed looks identical,
        # and that would quietly stop a whole category flowing.
        out = {"record_type": "evidence-collection", "collected": False,
               "reason": "not an evidence category",
               "detail_type": event.get("detail-type"),
               "source": event.get("source")}
        print(json.dumps(out, default=str))
        return out

    try:
        attribution = attribution_for(account_id)
    except Exception as exc:                       # noqa: BLE001
        # The map is unreadable. Everything is unattributed until it is not,
        # and that is said out loud rather than each record being dropped.
        attribution = None
        _emit("platform-evidence-tenant-map-unreadable",
              (f"The tenant map at {TENANT_MAP_PARAM} could not be read "
               f"({type(exc).__name__}). Evidence is still being collected and "
               "numbered, filed under the unattributed tenant, but it cannot "
               "be routed to a Truveon tenant until this is fixed."),
              {"account_id": account_id})

    if attribution is None:
        _emit("platform-evidence-unattributed-account",
              (f"Evidence arrived from account {account_id or 'unknown'}, "
               f"which is not in the tenant map. It has been numbered and "
               f"buffered under '{UNATTRIBUTED_TENANT}' so it is not lost, but "
               "it cannot reach a tenant's Truveon until the account is "
               "mapped. A newly vested account will look like this."),
              {"account_id": account_id, "category": category})
        attribution = {"tenant": UNATTRIBUTED_TENANT, "partner": None}

    item = record.build(
        tenant=attribution["tenant"],
        partner=attribution.get("partner"),
        category=category,
        source=event.get("source"),
        payload=event.get("detail") or {},
        control_ref=control_ref,
        occurred_at=event.get("time"),
        account_id=account_id or None,
    )

    # Ledger FIRST. See the module docstring.
    dynamodb.put_item(
        TableName=LEDGER_TABLE,
        Item={
            "PK": {"S": item["stream_id"]},
            "SK": {"S": f"{item['sequence']:012d}"},
            "sequence": {"N": str(item["sequence"])},
            "tenant": {"S": item["tenant"]},
            "partner": {"S": str(item.get("partner") or "")},
            "category": {"S": item["category"]},
            "emitted_at": {"S": item["emitted_at"]},
            "status": {"S": "pending"},
            "content_hash": {"S": item["content_hash"]},
            "body": {"S": json.dumps(item, default=str)},
        },
        # A sequence number is allocated once, so a collision means two
        # collectors somehow got the same number -- a bug worth hearing about
        # rather than an overwrite of one record by another.
        ConditionExpression="attribute_not_exists(PK)")

    if QUEUE_URL:
        sqs.send_message(QueueUrl=QUEUE_URL,
                         MessageBody=json.dumps(item, default=str))

    out = {"record_type": "evidence-collection", "collected": True,
           "stream_id": item["stream_id"], "sequence": item["sequence"],
           "category": category, "tenant": item["tenant"]}
    print(json.dumps(out, default=str))
    return out


def handler(event, context):
    return collect(event, context)
