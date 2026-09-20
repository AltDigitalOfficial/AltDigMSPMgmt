"""The evidence record, and the sequence number that makes gaps detectable.

Prompt 9.1, design/04-security-controls-and-evidence.md "Evidence pipeline to
Truveon".

    Because we push, we must prove nothing was silently dropped.
    [...] A silently failed pipeline that looks like a clean month is the
    failure mode that destroys an audit.

Sequence numbers are allocated ON INGEST, not on send
------------------------------------------------------
This is the decision the whole completeness argument rests on, and it is easy
to get backwards.

If a number were allocated when a record is successfully SENT, the sequence
would be contiguous by construction. It could never show a gap, because a
record that failed to send would simply never receive a number. The stream
would look perfect precisely when it was lossy -- "a clean month" in design
04's words.

Allocating on ingest means the number is minted the moment the platform becomes
aware of a piece of evidence, before anything can go wrong with delivery. A
record that is then lost leaves a hole at a known position, in a known tenant,
at a known time. The gap is the evidence that something went missing, and its
absence is the evidence that nothing did.

The cost is that a gap can also mean "sent but not yet acknowledged", which is
why the gap detector treats recent numbers as in-flight rather than missing.
A control that cried wolf on every in-flight record would be muted within a
week -- B-043, and B-081 after it.

Attribution travels with the record
-----------------------------------
design 15 makes AltDigital's SOC 2 the reference report that partners rely on
through a carve-out, and the chain is three deep: AWS is a subservice
organisation to AltDigital, AltDigital to the partner, the partner to the
client. A partner's auditor reading a record needs to know which entity's
control produced it.

So tenant and partner are ON the record rather than derivable from the account
id at query time. An account can be re-parented; evidence about what was true
in March must not change because an OU moved in September. D-016.

What is NOT on the record
-------------------------
A Trust Services Criteria reference. Framework mapping lives in Truveon, which
is the GRC tool, not here: a CC6.1 tag in the transport goes stale silently and
has to be maintained again for HIPAA, PCI and whatever the next tenant brings.
Records carry AltDigital's own control reference and Truveon maps it. D-016.
"""
import datetime
import hashlib
import json
import os

import boto3

dynamodb = boto3.client("dynamodb")

SEQUENCE_TABLE = os.environ.get("EVIDENCE_SEQUENCE_TABLE", "")

# design 04's "What flows". Anything outside this set is a bug in the collector
# rather than a new category to be invented at runtime.
CATEGORIES = {
    "config-compliance",      # continuous control evidence
    "security-finding",       # Security Hub, normalised and severity-scored
    "change-event",           # CloudTrail-derived, reconciled against the register
    "operational",            # restore tests, instrumentation, remediation,
                              # JIT elevation, notification timestamps
    # The pipeline's own operation. design 04 does not list this, and design 15
    # puts the evidence pipeline inside AltDigital's own SOC 2 scope -- so
    # evidence that the pipeline worked is itself required evidence. D-016.
    "pipeline-completeness",
}


class SequenceUnavailable(Exception):
    """A sequence number could not be allocated.

    Distinct, and the caller must not proceed without one. A record sent
    without a sequence number is a record whose loss would be undetectable,
    which is worse than a record that was never sent -- the second leaves a
    gap and the first does not.
    """


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def stream_id(tenant, category):
    """One sequence per tenant per category.

    Per CATEGORY as well as per tenant, deliberately. A single stream per
    tenant would interleave four sources with very different volumes and
    cadences, so a gap in the rare one -- restore test results, a handful a
    month -- would be invisible next to the chatty one. Separate streams mean a
    missing quarterly control is as detectable as a missing hourly finding.
    """
    return f"{tenant}#{category}"


def allocate(tenant, category):
    """The next sequence number for this stream. Atomic, monotonic, gapless.

    DynamoDB's ADD on a number is atomic, so two concurrent collectors cannot
    receive the same value and cannot skip one between them. That property is
    the reason this is a counter in a table rather than a timestamp or a uuid:
    both of those are unique and neither can prove that nothing is missing
    BETWEEN two records.

    Numbers are consumed even when the record is later dropped. That is the
    intent -- see the module docstring.
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"'{category}' is not an evidence category. design 04 names: "
            f"{', '.join(sorted(CATEGORIES))}")
    if not SEQUENCE_TABLE:
        raise SequenceUnavailable(
            "EVIDENCE_SEQUENCE_TABLE is unset. No sequence number can be "
            "allocated, so nothing may be sent: an unnumbered record is one "
            "whose loss would be undetectable.")
    try:
        result = dynamodb.update_item(
            TableName=SEQUENCE_TABLE,
            Key={"stream_id": {"S": stream_id(tenant, category)}},
            UpdateExpression="ADD seq :one SET updated_at = :now",
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":now": {"S": _now().isoformat()},
            },
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:                       # noqa: BLE001
        raise SequenceUnavailable(
            f"could not allocate a sequence number for "
            f"{stream_id(tenant, category)}: {type(exc).__name__}. "
            "Nothing may be sent without one: an unnumbered record is one "
            "whose loss would be undetectable, which is worse than a record "
            "that was never sent. The second leaves a gap; the first does "
            "not.") from exc
    return int(result["Attributes"]["seq"]["N"])


def build(tenant, partner, category, source, payload,
          control_ref=None, occurred_at=None, account_id=None):
    """An evidence record, numbered and ready to buffer.

    `occurred_at` is when the EVENT happened; `emitted_at` is when the platform
    saw it. Both, because they differ -- a Config evaluation can arrive minutes
    late and an auditor asking "did this control operate on the 3rd" means the
    first, while anyone debugging the pipeline means the second.
    """
    sequence = allocate(tenant, category)
    emitted = _now()
    body = {
        "record_type": "evidence",
        "tenant": tenant,
        # Carried, not derived. See the module docstring: an account can be
        # re-parented and evidence about March must not change in September.
        "partner": partner,
        "account_id": account_id,
        "category": category,
        "stream_id": stream_id(tenant, category),
        "sequence": sequence,
        "source": source,
        "control_ref": control_ref,
        "occurred_at": occurred_at or emitted.isoformat(),
        "emitted_at": emitted.isoformat(),
        "payload": payload,
    }
    # Content hash over everything except the hash itself.
    #
    # Not a security control -- anything that can write the record can
    # recompute the hash. It is a TRANSPORT integrity check: it tells the
    # difference between "Truveon holds a different version of record 41" and
    # "Truveon holds record 41 unchanged", which is the question a count
    # comparison cannot answer.
    body["content_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return body
