# Truveon ingestion contract

What the evidence pipeline needs from Truveon, and what Truveon actually
exposes. AltDigital builds both ends.

Status: **reconciled 2026-09-23.** Every section below is built on both
sides — Truveon commit `8560fd4`, platform commit `bbe9a37` onward — and
**not yet deployed** on the Truveon side. Until Truveon deploys, ingest (§1)
works as it did before and count (§2) is unanswerable, which reconciliation
reports as *unanswerable*, never *agreed*. B-088.

---

## Why this document changed

The first version was written from this side before Truveon's ingestion
endpoint existed, on the premise that the platform's completeness controls
should set the API. On 2026-09-13 Truveon built `POST /api/ingest/evidence`
against its own functional assumptions (4.3.x, 18.1.x) and a CISO review item
(A12). The two did not match, and the platform's sender would have failed
against the live endpoint on the first call.

Resolved by adopting Truveon's shape and adding to Truveon only what the
completeness controls genuinely cannot do without. The substantive change is
**authentication**; everything else is envelope mapping.

### Per-tenant keys, not one shared token

The first version argued for one bearer token across all tenants: per-tenant
keys mean N secrets, and one could expire quietly and silence a single tenant
while the rest looked healthy.

That argument is answered by the pipeline's own second control. The hourly
heartbeat is **per tenant** and is itself a numbered evidence record; a tenant
whose key stops working stops delivering heartbeats, its ledger rows age past
the grace period, and the gap detector raises a finding naming that tenant
within the hour. The quiet failure the first version feared is exactly the one
this pipeline was built to make loud.

What remains is the difference in blast radius. One shared token that leaks
exposes every tenant's evidence store to writes; a per-tenant key exposes one.
Truveon also derives the tenant from the key rather than trusting a field in
the body, so a collector bug that mis-files an account cannot write into the
wrong tenant — it can only fail to authenticate.

---

## 1. Ingest

```
POST {TRUVEON_ENDPOINT}/api/ingest/evidence
X-Truveon-Ingest-Key: truveon_ing.<truveon-tenant-slug>.<secret>
```

Truveon's unit is a **submission**: one delivery, from one account, on one
stream, carrying N events. The platform sends exactly one evidence record per
submission, because the record is the unit the ledger numbers and marks
delivered — batching several into one submission would make "delivered" a
property of a group, and a partial failure would be unrepresentable.

| Submission field | Value | Why |
|---|---|---|
| `stream` | the record's `category` | Truveon scopes streams per tenant already; the tenant is the key. |
| `aws_account_id` | the record's `account_id`, or the pipeline's own account for records that have none | See "Records with no account" below. |
| `idempotency_key` | `<category>.<sequence:012d>` | Unique per stream, stable across retries, and safe inside an S3 key (Truveon puts it in the archive path — `#` would not be). |
| `sequence_number` | the record's `sequence` | Truveon's own gap detection runs on this. |
| `event_time` | the record's `emitted_at` | The platform's clock. §2 windows on this, and it must be the same clock the platform counts by. `occurred_at` travels inside the record. |
| `events` | `[record]` | The record exactly as `evidence/src/record.py` built it, `content_hash` included. |

### Idempotent on `(stream, idempotency_key)` — built

A re-POST of a submission Truveon already holds returns `200` with
`"duplicate": true`. The sender treats that as delivered. This is what makes
retry and `replay_pending` safe: without it every retry would add a row, and
count reconciliation would report a mismatch on a stream that is intact.

### Same key, different content: `409` — built

If Truveon holds `(stream, idempotency_key)` with a different payload from the
one being posted, it must return `409` naming both, **not** `200 duplicate`.

"Content" is the account, sequence number, event time and events, compared
in Postgres so that jsonb's number normalisation cannot turn a faithful
redelivery into a conflict. The refusal is recorded on Truveon's side as an
`idempotency_conflict` rejection carrying both digests. The sender treats
`409` as a finding for a human and does not retry it.

### The response means PERSISTED — built

Truveon writes the archive object, then commits the database row, then
responds. A `200` therefore means durable. The sender still checks the body
says so — `"accepted": true` and a `submission_id` — rather than inferring it
from the status code, because a proxy or gateway can return `200` for
something that never reached a datastore, and this acknowledgement is what
permanently removes the record from gap detection.

### Failure

Any non-2xx other than `409` leaves the record **pending** and it is retried.
`401` is included: a revoked or missing key is retryable once someone fixes the
secret, and the heartbeat gap makes sure someone notices.

### Records with no account

`pipeline-completeness` records (heartbeat, gap detected, count reconciled)
are about a tenant's pipeline, not about any one of its accounts, and carry
`account_id: null`. Truveon requires `aws_account_id`.

The envelope carries the account the pipeline runs in — the Audit account —
and the record itself is left untouched with its `null`. Onboarding registers
the Audit account in every client's Truveon tenant (see
`scripts/truveon-tenant-handoff.sh`). That turns out to be useful rather than a
workaround: Truveon's own `account_silent` detection now watches the heartbeat,
so a stopped pipeline is noticed from both ends independently.

---

## 2. Count — built

```
GET {TRUVEON_ENDPOINT}/api/ingest/evidence/count?stream=<category>&from=<iso8601>&to=<iso8601>
X-Truveon-Ingest-Key: ...
```

```json
{ "stream": "security-finding", "from": "...", "to": "...",
  "count": 1204, "min_sequence": 3001, "max_sequence": 4204 }
```

Authenticated by the ingest key, not a session — the caller is a Lambda. The
window is by `event_time`, which §1 sets to `emitted_at`.

`min_sequence` and `max_sequence` matter as much as `count`. Equal counts over
a window can hide one record lost and one duplicated; with the range, both
sides can check that the count matches the span.

A stream with nothing in the window returns `count: 0` and `null` for both
sequence fields, not a `404`. The platform distinguishes "Truveon holds none"
from "Truveon could not be asked" throughout, and a `404` would blur them.

---

## 3. Watermark — built

```
GET {TRUVEON_ENDPOINT}/api/ingest/evidence/watermark?stream=<category>
X-Truveon-Ingest-Key: ...
```

```json
{ "stream": "security-finding", "lowest_sequence": 3001, "highest_sequence": 4204,
  "held": 1202, "missing_ranges": [[4188, 4188], [4190, 4190]],
  "missing": [4188, 4190], "missing_truncated": false }
```

`missing_ranges` is complete; `missing` flattens it and is capped at 1000,
with `missing_truncated` saying so. Nothing below `lowest_sequence` is
claimed missing — Truveon cannot know whether a stream began at 1.

`missing` is Truveon's own view of holes below its high-water mark. Truveon
already records these as `sequence_gap` events at arrival; this exposes them to
the sender. It lets the two sides compare notes rather than the platform being
the only one with an opinion about its own completeness.

---

## 4. Receipt — built

```
GET {TRUVEON_ENDPOINT}/api/ingest/evidence/receipt?aws_account_id=<id>
X-Truveon-Ingest-Key: ...
```

The same answer as Truveon's session-authenticated
`/api/provisioning/evidence-receipt` — registered, received any, submission
count, last received — reachable by a machine. This is the question the
verification sweep's check 1 asks before go-live, from the Audit account.
Without `aws_account_id` it returns the tenant's whole expected-versus-actual
account picture.

---

## Credentials

One Truveon ingest key per platform tenant, in Secrets Manager in the Audit
account:

```
platform/truveon/ingest/<platform-tenant>     {"key": "truveon_ing.<slug>.<secret>"}
```

The platform tenant name and the Truveon tenant slug need not match. The key
carries the Truveon slug, so which key sits under which platform tenant's
secret *is* the mapping, and the registry records the slug as
`truveon_tenant_id` for humans and for the verification sweep.

A tenant with no secret is not an error the sender swallows: its records stay
pending and a `platform-evidence-no-credential` finding names the tenant.

Keys are minted in Truveon (`POST /api/ingest/credentials`, session
authenticated, shown once) during the onboarding handoff, and rotated by
minting a new one, updating the secret, and revoking the old one. The sender
caches keys per execution environment; a rotated key is picked up on the next
cold start or after a `401`, whichever comes first.

---

## What Truveon does NOT receive

**Raw logs.** design 04:

> Truveon holds derived findings and correlated evidence. **Raw logs remain in
> the Log Archive account** under Object Lock. An auditor may want the raw
> record; know which store answers which question.

Confirmed 2026-09-23 as the near-term position. Truveon will receive evidence
*about* the raw logs — that each account's logs arrived, when, and their
hashes — without the logs themselves. Shipping raw logs into Truveon is a
long-term product question, not an integration task.

**Personal data.** Approver and requester identities travel as opaque account
identifiers, never names or email addresses.

---

## Open, and deliberately

**Retention inside Truveon** is not specified here. design 04 sets a
per-tenant retention floor, set on the Truveon tenant during onboarding
(`PUT /api/member-accounts/config`). The pipeline does not delete from Truveon
and has no endpoint to do so.
