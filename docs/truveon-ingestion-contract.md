# Truveon ingestion contract

What the evidence pipeline needs from Truveon. Written from this side on
purpose: AltDigital builds both ends, so the contract is set by what the
completeness controls require rather than discovered from whatever the
ingestion endpoint happens to do.

Status: **specified, not yet implemented.** See B-088 for the trigger.

The platform is built against this and tested against a stub. Nothing here is
exotic; all of it exists to make the four controls in
[design 04](../design/04-security-controls-and-evidence.md) "Completeness is a
control" actually work.

---

## Why the pipeline cannot just POST and hope

design 04's premise:

> Because we push, we must prove nothing was silently dropped. [...] A silently
> failed pipeline that looks like a clean month is the failure mode that
> destroys an audit.

Three of the four controls need something back from Truveon. Sequence numbering
is ours alone; buffering is ours alone; but **acknowledgement** and
**reconciliation** are a conversation, and an endpoint that only accepts posts
cannot hold up its end.

---

## 1. Ingest

```
POST /v1/evidence
```

Body is one evidence record as built by `evidence/src/record.py`:

```json
{
  "record_type": "evidence",
  "tenant": "acme",
  "partner": "oeight",
  "account_id": "754280127660",
  "category": "security-finding",
  "stream_id": "acme#security-finding",
  "sequence": 4182,
  "source": "securityhub",
  "control_ref": "...",
  "occurred_at": "2026-09-20T09:14:02+00:00",
  "emitted_at": "2026-09-20T09:14:07+00:00",
  "payload": { },
  "content_hash": "sha256..."
}
```

### Idempotent on `(stream_id, sequence)` — REQUIRED

A re-POST of a sequence Truveon already holds is a **success**, not a duplicate
and not an error.

This is the requirement most likely to be dropped as an implementation detail,
and it is the one that breaks everything else. The sender retries — against
timeouts, 5xx, and its own crashes between "sent" and "recorded as sent". A
Truveon that appended on every POST would accumulate duplicates, and the count
reconciliation in control 4 would then report a mismatch on a stream that is
perfectly intact. The control would be wrong in the direction that costs an
audit conversation.

If the stored `content_hash` differs from the one being posted for the same
`(stream_id, sequence)`, that is **not** idempotent-success. Return `409` and
say both hashes. Same number, different content, means one of the two stores is
wrong and neither side should guess which.

### The response means PERSISTED

```json
{ "stream_id": "acme#security-finding", "sequence": 4182, "stored": true }
```

`200` only once the record is durable. Not "queued", not "accepted".

The sender marks its ledger row `delivered` on this response, and a delivered
row is excluded from gap detection forever. An acknowledgement that meant
"received" would let Truveon lose a record that the platform had already
stopped looking for — the exact silent failure this whole design exists to
prevent.

If Truveon buffers internally, do not answer until the buffer is durable.

### Failure

Any non-2xx leaves the record **pending** and it is retried. Truveon does not
need to distinguish retryable from fatal; the sender treats everything except a
`409` as retryable, and a `409` as a finding for a human.

---

## 2. Count

```
GET /v1/evidence/count?stream_id=<id>&from=<iso8601>&to=<iso8601>
```

```json
{ "stream_id": "acme#security-finding", "from": "...", "to": "...",
  "count": 1204, "min_sequence": 3001, "max_sequence": 4204 }
```

Control 4, the periodic count comparison. The window is by `emitted_at`, which
is the platform's clock — `occurred_at` is the source's and a Config evaluation
arriving late would otherwise make a healthy period look short.

`min_sequence` and `max_sequence` matter as much as `count`. Equal counts over a
window prove less than they appear to: the same total can hide one record lost
and one duplicated. With the range, the platform can check that the count
matches the span.

---

## 3. Sequence high-water mark

```
GET /v1/evidence/watermark?stream_id=<id>
```

```json
{ "stream_id": "acme#security-finding", "highest_sequence": 4204,
  "missing": [4188, 4190] }
```

`missing` is Truveon's own view of holes below its high-water mark. Optional to
compute, valuable to have: it lets the two sides compare notes rather than the
platform being the only one with an opinion about its own completeness. A
pipeline that audits itself and reports itself healthy is worth less than two
stores that agree.

---

## Auth

Bearer token per environment, from Secrets Manager, rotatable without a
deployment. One credential per Truveon tenant is **not** required — the
`tenant` field carries the isolation and Truveon enforces it server-side.

Noted because the opposite is tempting: per-tenant credentials sound stronger
and would mean the platform holding N secrets and rotating N of them, with the
failure mode that one expires quietly and that tenant's evidence stops while
every other tenant looks fine. One credential fails loudly for everybody.

---

## What Truveon does NOT receive

**Raw logs.** design 04 is explicit:

> Truveon holds derived findings and correlated evidence. **Raw logs remain in
> the Log Archive account** under Object Lock. An auditor may want the raw
> record; know which store answers which question.

**Personal data.** Approver and requester identities travel as opaque account
identifiers, never names or email addresses. Anything joining an identifier to
a person happens in the system that owns identity, not in the evidence store.

---

## Open, and deliberately

**Retention inside Truveon** is not specified here. design 04 sets a per-tenant
retention floor derived from the union of applicable frameworks, and that value
belongs to the tenant record rather than to this contract. The pipeline does not
delete from Truveon and has no endpoint to do so.
