# ADR-018 — Flow logs are operational records, kept 400 days, outside the derived evidence floor

**Date:** 2026-10-29
**Deciders:** Jamie (400 days, 2026-10-29); Claude (session), unreviewed (the 30-day noncurrent window, the matching replica rule, MinValue 400 on both parameters)
**Status:** Accepted
**Tags:** flow-logs, retention, log-archive, phase-15.7

---

## Context

Phase 15.7 turned flow logs on once B-012 was shown not to reproduce. They
land in `altdig-flow-logs-<logarchive>` and replicate to
`altdig-flow-logs-replica-<logarchive>` in us-west-2. Neither bucket expired
anything. The source moved objects to Glacier IR at 90 days and kept them
forever. The replica had no lifecycle at all.

The 15.7 prompt asked for 400 days: a 12-month SOC 2 period plus fieldwork
lag. The phase 15 plan (15.7 finding 4) showed that conflicts with design
doc 04's principle that retention is derived from the union of tenant
frameworks. That derivation gives 2,190 days for HIPAA
(`vesting/derivation-rules.yaml`).

The repository already treated flow logs as operational rather than
evidentiary. That is why their bucket and its replica carry no Object Lock,
unlike the CloudTrail and Config archives.

## Decision

1. **Flow logs expire at 400 days** on the source bucket
   (`FlowLogRetentionDays`, `security/10-log-archive.yaml`).
2. **The replica carries its own matching rule**
   (`FlowLogReplicaRetentionDays`, `security/20-archive-replicas.yaml`).
   S3 lifecycle actions are not replicated, so without it the replica would
   keep everything forever.
3. **Noncurrent versions are removed 30 days after expiry.** Both buckets are
   versioned, so expiry only writes a delete marker. The 30 days is also the
   window to recover a flow log deleted by mistake. Effective retention is
   400 to 430 days.
4. **Both parameters have `MinValue: 400`.** Going below the ruling takes a
   template change, not a parameter override.
5. **The derived floor continues to govern the evidence trail.** CloudTrail
   and Config stay under COMPLIANCE Object Lock at 2,190 days.

## Alternatives considered

- **Derive flow-log retention from the tenant floor.** Rejected. The bucket
  is shared by every tenant, so one HIPAA tenant would mean six years for
  everyone, at a cost that rises every month for six years. Six years of
  HIPAA retention is commonly read as applying to required documentation,
  which the evidence trail holds, not to raw network telemetry. That reading
  is not certain: an assessor could take a different view.
- **Per-tenant retention by prefix.** Not built. Lifecycle rules can filter
  on `AWSLogs/<account-id>/`, so a tenant that contracts for longer can have
  a longer rule without changing anyone else's. Revive it when a tenant
  contract or an assessor requires more than 400 days.
- **Keep everything forever (the status quo).** Rejected. It was the absence
  of a decision, not a decision.

## Rationale

Flow logs are for reconstructing an incident and for telling a silent
instance from a stopped one. 400 days covers any incident discovered within
an audit period, and the audit period itself. The evidence an auditor tests
is the trail, and that is held for the derived floor.

## Consequences

**Positive:**
- Flow-log storage stops growing without limit.
- The repository now states what was previously only implied: flow logs sit
  outside the evidence floor.

**Negative:**
- **Extending is forward-only.** Raising the parameter keeps objects that
  have not yet expired. Anything already past 430 days is gone.
- **Shortening is possible, and destructive.** Nothing is locked, so a lower
  value deletes everything older on the next lifecycle run. `MinValue`
  guards only against doing that by accident.
- **The two parameters must be kept equal by hand.** They live in two stacks
  in two regions.

## Related

- B-012 (flow-log delivery, resolved 2026-10-29)
- B-134 (flow logs are not yet in log custody)
- Design doc 04, retention derivation; `vesting/derivation-rules.yaml`
