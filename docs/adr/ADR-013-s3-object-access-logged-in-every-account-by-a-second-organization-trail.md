# ADR-013 — S3 object access is logged in every account, by a second organization trail

**Date:** 2026-10-26
**Deciders:** Jamie (object-access logging, every account, organization trail, 2026-10-26); Claude (session), unreviewed (the separate trail, the prefix, the loop exclusion)
**Status:** Accepted (deployed 2026-10-26/27; verified: delivering from every account, archive writes absent, replication reads excluded, `verify-security-coverage.sh` passes)
**Tags:** cloudtrail, s3, data-events, hipaa, log-archive, phase-15.6

---

## Context

The organization trail ([org/30-organization-trail.yaml](../../org/30-organization-trail.yaml))
records management events only. Its header says data events "belong to a
per-tenant decision driven by the questionnaire's data classification, not
to the platform baseline". So no S3 object read was recorded in any account
(auditor guide §6). [docs/hipaa-control-mapping.md](../hipaa-control-mapping.md)
§3 names this as the gap an assessor is most likely to press under
164.312(b).

On 2026-10-26 Jamie was offered three scopes:
- HIPAA accounts only;
- every account through an organization trail;
- every account, writes only.

Jamie chose every account, through an organization trail.

The trail delivers to `altdig-log-archive-<acct>`. That bucket replicates
everything (`Filter: {}`) to `altdig-log-archive-replica-<acct>` in
us-west-2, under Object Lock COMPLIANCE for 2190 days (D-024). Its policy
admitted CloudTrail only under `AWSLogs/*`. The Athena table (ADR-008) and
the digest-facts gatherer (ADR-004) read that prefix.

## Decision

1. A **second organization trail**, `platform-s3-data-events`
   ([org/31-s3-data-events-trail.yaml](../../org/31-s3-data-events-trail.yaml)),
   in the management account. It is multi-region, with log file validation,
   and is encrypted with the log archive key.
2. It selects **S3 object data events only, reads and writes, in every
   bucket**, and no management events.
3. **Writes** to `altdig-log-archive-*` (the archive and its replica) are
   excluded. **Reads** of them are recorded.
4. Files land under `data-events/` in the same bucket. The bucket policy
   gains a `CloudTrailWriteDataEvents` statement scoped to that prefix and
   that trail name ([security/10-log-archive.yaml](../../security/10-log-archive.yaml)).

## Alternatives considered

- **HIPAA accounts only, through a per-account trail deployed by vesting.**
  This was my recommendation. Jamie chose otherwise: uniform evidence, and
  no vesting step to forget.
- **Writes only.** Much cheaper, but 164.312(b) is about access, and access
  to ePHI is mostly reads. Rejected.
- **Data event selectors on the existing organization trail.**
  Rejected for three reasons:
  - it mixes data-event volume into `AWSLogs/`, which ADR-004 and ADR-008
    scan;
  - it modifies the stack of the one detective control that does not depend
    on the baseline;
  - narrowing the scope later would mean editing that same trail again.
- **A separate destination bucket.** Rejected. It needs a new locked bucket,
  replica, policy and key grants in Log Archive, to separate what a prefix
  already separates.
- **No exclusion.** Rejected. Each delivery is a PutObject, and so is the
  replication write it causes. Logging them generates events that are
  delivered and logged in turn: a loop that grows on its own.
- **Excluding the log archive entirely.** Rejected. Who read the audit
  logs is exactly the access worth evidencing, and the trail never reads,
  so reads cannot loop.

## Rationale

Every account matches the uniform-detective-controls principle (designs 02
and 14), and the pack's `cloudtrail-s3-dataevents-enabled` rule (ADR-012)
then holds everywhere. The separate trail and prefix leave every existing
reader and the management trail untouched.

## Consequences

**Positive:**
- Every S3 object read and write in every account and region is recorded,
  apart from the trail's own deliveries and their replication.
- Reads of the audit archive itself are evidenced.

**Negative:**
- **Cost scales with every tenant's S3 request volume**, including tenants
  without PHI, and it is not knowable in advance. Under Object Lock
  COMPLIANCE every byte written is **kept for six years and cannot be
  removed**. Narrowing the selectors reduces cost only from that point on.
  This is the largest open-ended cost the platform now carries.
- Platform buckets with steady machine traffic (the Config archive,
  evidence, digest facts, Athena results) are logged too. That is volume
  with little evidential value.
- The pack's `cloudtrail-s3-dataevents-enabled` rule may judge a trail with
  an ARN exclusion as not covering "all buckets". This is unverified. If it
  does, the finding is explained here, not suppressed.

**Enforcement:**
- SCP 01 denies `StopLogging`, `DeleteTrail`, `UpdateTrail` and
  `PutEventSelectors` to member accounts. An organization trail is not a
  member's resource in any case.
- `platform-cloudtrail-s3-dataevents-enabled` (ADR-012) reports any account
  where the trail stops covering S3.

## Not decided here

- **A volume trigger for narrowing scope**: excluding platform machine
  buckets, or moving to per-classification selectors. It wants a week of
  billing data first. When it is set, record it in docs/scale-watch.md.
- **Lambda data events.** Not asked for, and not enabled.
- **An Athena table over `data-events/`.** Nothing queries it yet.

## Related

ADR-004, ADR-008, ADR-012, D-024, B-005, docs/hipaa-control-mapping.md,
auditor guide §6.

## Amendment — 2026-10-26

**Deployed the same day**, and two things learned from the first files.

1. **AWS's managed rule cannot recognise this trail.** In every account,
   including the management account that owns the trail,
   `CLOUDTRAIL_S3_DATAEVENTS_ENABLED` reported *"No AWS CloudTrail Trail is
   configured to log data events for Amazon S3"* while the trail was logging
   and delivering. Member accounts could read the trail's selectors, so the
   cause is the ARN exclusion, not visibility. This was the unverified risk
   under *Consequences*.

   Jamie, 2026-10-26: keep the exclusion and replace the managed rule with a
   check of what was built (option 1 of three). The alternatives were:
   - dropping the exclusion: a loop risk, with every byte locked for six
     years and no non-production trail to test on;
   - suppressing the finding, which would also hide a real stop.

   The replacement has two parts:
   - `platform-s3-data-events-trail-configured`, a Guard policy rule in the
     account-level pack (ADR-012 amendment). It checks Config's record of the
     trail: logging, organization-wide, multi-region, validated, under
     `data-events`, with custom selectors.
   - `verify-security-coverage.sh`, which checks what the record cannot see:
     the trail's absence, and the exact selectors.

2. **Replication reads were most of the archive's volume.** In the first
   twenty minutes, 931 of the 1,125 data events in Log Archive were S3
   replication reading each delivered file (`GetObject` and `GetObjectAcl` by
   `PlatformArchiveReplication`). This is bounded, because it scales with file
   count, but it buried the human reads. The reads selector now excludes both
   replication roles by `userIdentity.arn`. The template takes a new
   parameter, `LogArchiveAccountId`.

   Writes to the archive and its replica were confirmed absent, so the
   exclusion works.

**Verified after the change:**
- Replication reads of the log archive fell from about 1,900 in the hour
  before to none.
- Events with no ARN are kept. The hourly cross-account `ListObjects` burst
  from Audit (identity type `AWSAccount`, no ARN; 60 on the archive, 30 on
  the Config archive) was recorded at 03:00 UTC as it was at 02:00, under the
  new selector.
- `platform-s3-data-events-trail-configured` is COMPLIANT in the management
  account.

**Found:** the management account forwards no events, so tampering with
either organization trail pages nobody (B-175).
