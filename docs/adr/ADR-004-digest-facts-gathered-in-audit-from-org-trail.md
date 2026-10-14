# ADR-004 — Digest facts are gathered once a day in Audit from the org trail, Config aggregator and router log; Tooling only renders and sends

**Date:** 2026-10-14
**Deciders:** Claude (session), proposed; Jamie ratified 2026-10-14
**Status:** Accepted — not built. Pending B-171; blocked on `security/50` being deployed and proved.
**Tags:** digest, cloudtrail, athena, audit, cross-account, personal-data

---

## Context

ADR-003 widens the digest from alarms and instrumentation exceptions to the
following:

- who created each resource, and how;
- team activity;
- services used for the first time;
- roles created;
- guardrail denials;
- ClickOps;
- the missing layer tag;
- the router's digest-class findings.

Today the digest runs in Infra Tooling. It assumes `PlatformDigestReader` in
each tenant account (`baseline/50-instrumentation.yaml`), which can read
alarms and one log group.

Where those facts live:

- **Org trail** (Log Archive). Management events, read and write, kept six
  years. It is queryable through Athena once `security/50-trail-query.yaml` is
  deployed; that is built but not deployed.
- **Config aggregator `platform-organization`** (Audit). Current configuration
  of every resource, including role trust policies and tags.
- **Router decision log** (Audit, `/aws/lambda/platform-router`). This holds the
  eligibility sweep's and telemetry gap's digest-class decisions. Nothing reads
  it (B-051).

In this session the first recommendation, made before those sources were
inventoried, was per-account CloudTrail `LookupEvents` through the existing
reader role. It is recorded below as rejected.

## Decision

- **A gatherer in Audit, `platform-digest-facts`, runs daily at 12:15 UTC.** It
  queries:
  - the org trail through Athena, via a narrow Log Archive role,
    `PlatformDigestFactsQuery`;
  - the Config aggregator;
  - the router log.
- **It writes one facts document per tenant account, plus an estate
  document,** to an Audit bucket. The bucket is SSE-KMS with a 30-day expiry.
- **The Tooling digest reads those documents** (`s3:GetObject` only), then
  renders and sends. It keeps its existing per-account read of alarms and
  exceptions.
- **A missing or stale facts document degrades the email; it never suppresses
  it.** It emits `AccountsFactsMissing`, which drives
  `platform-digest-facts-missing` through the router.

Details are in [digest-design.md §6](../digest-design.md).

## Alternatives considered

- **Per-account `LookupEvents` from Tooling through `PlatformDigestReader`.**
  Rejected for four reasons:
  - It is throttled to 2 requests per second per account per region.
  - Guardrail denials are mostly read-only calls, and a day of console browsing
    produces too many read events to page through at that rate inside a Lambda
    run.
  - "First use of a service" needs history it cannot cheaply provide.
  - It would add CloudTrail read to a role that exists in every member account.

  It remains the right tool for a human's one-off question, and it is how the
  Resource Explorer creator was found.
- **Tooling queries Athena in Log Archive directly.** Rejected. Trail read
  would sit in the account that holds the SES identity and has no security
  function. Audit is already the security tooling account, and it already
  holds the aggregator and the router.
- **EventBridge forwarding of CloudTrail events to a central collector.**
  Rejected. It builds a second store of what the org trail already holds as the
  evidence-grade record. Real time buys nothing for a daily read. It would also
  need new rules in every member account.
- **CloudTrail Lake.** Rejected. It is a second, separately billed copy of the
  trail, for a daily query the existing archive can answer.
- **A seen-services state table instead of a 90-day scan.** Deferred, not
  rejected. A scan is simpler and costs cents at today's volume. The trigger is
  a single run scanning more than 5 GB.

## Rationale

The org trail is the only source that has every fact the manager digest
needs, and it is already retained and custody-separated. Querying it once per
day for the whole estate scales with tenants at the cost of one query per
signal, not one call per account. Splitting gathering from rendering keeps
trail access out of Tooling. It also gives both digests (ADR-003) the same
facts, so they cannot disagree.

## Consequences

**Positive:**
- B-128's layer-tag report and B-051's routing-log read both land inside this
  work instead of being built separately.
- The digest decides nothing about routing. It shows what the router decided
  (W-010).

**Negative:**
- **A dependency on `security/50`.** That file is uncommitted, owned by
  another line of work, and needs a `sessioncredentialfromconsole` column
  added.
- **Blind spot for new accounts.** The trail table's account list is projected
  at deploy time, so a newly vested account is invisible until that stack is
  redeployed. Mitigation: the gatherer reports the gap. It does not fix it.
- **More personal data held for six years.** Developer session names enter the
  rendered digest body, which is Object Lock retained for six years (ADR-001),
  and the facts bucket holds them for 30 days. This is accepted on the grounds
  in digest-design.md §9.
- **Cross-account surface.** A new role in Log Archive, a new bucket in Audit,
  and a new read grant from Tooling into Audit.

**Enforcement:**
- `PlatformDigestFactsQuery` is limited to `StartQueryExecution` on the
  `platform-audit` workgroup, plus read on the trail prefix and its own results
  prefix.
- The facts bucket policy admits Tooling's sender role for `GetObject` only.
- `platform-digest-facts-missing` catches a gatherer that stops.

## Not decided here

- Whether the seen-services table replaces the 90-day scan. That has a trigger,
  in digest-design.md §10.
- Where facts go once Truveon exists.

## Related

- [docs/digest-design.md](../digest-design.md)
- ADR-001, ADR-003
- B-051, B-128, B-148, B-155, B-171, B-172
- W-010
- `security/50-trail-query.yaml`, `reporting/src/digest.py`,
  `baseline/50-instrumentation.yaml`
