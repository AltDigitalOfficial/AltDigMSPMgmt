# ADR-008 — The org trail is queried in place through Athena in Log Archive, with partitions projected from a deploy-time account list

**Date:** 2026-10-08
**Recorded:** 2026-10-14
**Deciders:** Jamie (to build a query layer over the trail, 2026-10-08: "let's go on and build it"); Claude (session), unreviewed (its shape)
**Status:** Proposed
**Tags:** cloudtrail, athena, audit, log-archive, cost

---

## Context

The org trail is retained for six years under COMPLIANCE Object Lock in
`altdig-log-archive-<acct>`. Until this work, nothing could search it:

- It is not delivered to CloudWatch Logs (B-005).
- `LookupEvents` covers 90 days of management events, one account and one
  region at a time.
- There was no Athena and no CloudTrail Lake.

Six years of retention bought ninety days of answerable questions. The audit
questions this blocks include who assumed `OrganizationAccountAccessRole`
over the period, and which calls the SCPs refused.

Asked on 2026-10-08 whether to build the query layer with the audit
permission set or defer it, Jamie said to build it. What follows is the
shape that was built.

## Decision

[security/50-trail-query.yaml](../../security/50-trail-query.yaml), deployed to
**Log Archive** by `scripts/deploy-audit-access.sh`:

- **Athena workgroup `platform-audit`.**
  - `EnforceWorkGroupConfiguration` is on, so the result location and its
    encryption cannot be overridden by a client.
  - A per-query scan cutoff, 50 GiB by default.
- **Glue table `platform_audit.cloudtrail`.**
  - Reads the archive in place.
  - Partitions are projected, not crawled: `account`, `region`, `dt`
    (`yyyy/MM/dd`).
- **`account` values are passed in at deploy time**, derived from
  Organizations: every account, any status, because closed accounts keep
  their history.
- **`region` values are every commercial region**, not only the three allowed
  ones. Denied-region attempts are recorded by the multi-region trail, and they
  are evidence for the region SCP.
- **The day partition is named `dt`, not `timestamp`.** `TIMESTAMP` is a type
  keyword in Athena SQL, and an unquoted `timestamp` in a WHERE clause is the
  first thing someone typing a query would trip on.
- **Results go to their own bucket.**
  - Bucket `altdig-audit-query-<acct>`, under its own key,
    `alias/platform-audit-query`.
  - Expired by S3 lifecycle after 30 days. They are derived working copies;
    the archive is the evidence.
- **Six saved queries**, matched to auditor-guide sections.
- **Column `sessioncredentialfromconsole`**, added 2026-10-14 for ADR-004's
  ClickOps signal.

## Alternatives considered

- **CloudTrail Lake.** Rejected:
  - it is a second, separately billed copy of a trail the archive already
    holds as the evidence-grade record;
  - it only covers events from when the event data store was created, unless
    old data is imported, which is billed again.

  ADR-004 rejected it for the digest, for the same reasons.
- **Deliver CloudTrail to CloudWatch Logs (B-005).** Rejected for this
  purpose: retention and ingestion cost at six-year scale, and it still
  leaves the past unqueryable. B-005 stays open for real-time metric filters,
  which is a different need.
- **A Glue crawler or `MSCK REPAIR` to discover partitions.** Rejected: one
  more scheduled thing that silently stops. Projection computes partitions at
  query time, with nothing to maintain except the account list.
- **`account` as an `injected` projection, so no list is needed.** Rejected:
  every query would then have to name one account, which defeats the
  estate-wide questions the table exists for.
- **Project only the three allowed regions.** Rejected: it hides exactly the
  denied-region attempts an auditor testing SCP 06 looks for.
- **Encrypt results with the log archive key.** Rejected: the auditor's
  identity would then need `GenerateDataKey` on the archive key, and could
  produce objects encrypted as though they were archive content.
- **Put the workgroup in Audit.** Rejected: Athena would then need
  cross-account read on the archive and its key. In Log Archive, the existing
  IAM-delegated key policy is enough. ADR-004's gatherer, in Audit, reaches
  this workgroup through its own narrow role instead.

## Rationale

Query the evidence where it already sits, under the custody it already has,
and add nothing that must be kept running. The derive-don't-copy rule
`deploy-security-access.sh` follows for assignments applies here to the
partition list.

## Consequences

**Positive:**
- Six years of trail are searchable with no second copy.
- The table now has a second consumer, the digest facts gatherer (ADR-004),
  so it is shared platform infrastructure and not only an audit convenience.

**Negative:**
- **A newly vested account is invisible to the table** until
  `deploy-audit-access.sh` runs again. The script says so, and ADR-004's
  gatherer reports the gap. Nothing fixes it automatically. With a second
  consumer, this stack's redeploy probably belongs in the vesting flow, not
  only in the audit grant script. Not done.
- **Cost depends on the query.** Athena bills per byte scanned, and objects
  older than 90 days are in Glacier Instant Retrieval, with a per-GB retrieval
  charge. The cutoff bounds a single query, not a day of them.
- **The schema follows AWS's documented column set.** Fields AWS adds later
  are absent until a column is added, as `sessioncredentialfromconsole` was.
- **Not deployed and not proved.** The table, the SerDe and the projection
  template have not been queried against the real archive.

**Enforcement:**
- The workgroup's enforced configuration and its scan cutoff.
- `AuditEvidenceReader` grants Athena on `platform-audit` only, not on the
  `primary` workgroup.
- cfn-lint and cfn-guard pass.

## Related

- ADR-004 (second consumer), ADR-005, ADR-007
- B-005
- auditor-guide §1.3 and §6
- `security/10-log-archive.yaml`, `scripts/deploy-audit-access.sh`
