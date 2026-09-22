# 05 — Resilience: Backup and Recovery

## Default objectives

| | Default | Notes |
|---|---|---|
| **RTO** | 4 hours | Achievable without active-active; keeps cost sane |
| **RPO** | 2 hours | The sharper constraint — rules out daily snapshots |

Per-application objectives may be tighter and are captured at intake. Tighter
objectives imply a different architecture and a different price.

---

## Backup architecture

**AWS Backup, driven centrally from the Organization** via backup policies
pushed down to the Members OU. Coverage is enforced by policy, not by hoping
each account configured it correctly.

- Backup copies land in a **separate backup account**
- **Vault Lock enabled** — a compromised member account cannot destroy its own
  backups. This is the ransomware answer, and auditors ask about it directly.
- Vault deletion and lock modification are quorum-controlled (see [03](03-identity-and-access.md))

### Meeting a 2-hour RPO

Standard daily snapshots do not meet this. Required per resource type:

| Resource | Mechanism |
|---|---|
| RDS / Aurora | Continuous backup with point-in-time recovery |
| DynamoDB | Point-in-time recovery |
| S3 | Versioning + Cross-Region Replication |
| EBS / EC2 | Snapshot every 2 hours, or replication |
| EFS | AWS Backup continuous where supported |

Non-production environments run reduced retention and relaxed frequency.

---

## The unified restore path

**This is the most important decision in this section.**

The restore procedure is **one artifact with a mode parameter**:

| | Test mode | Recovery mode |
|---|---|---|
| Restore target | Isolated test VPC | Production networking |
| Validation | Runs | Runs |
| Teardown | Automatic | Stops; no teardown |
| Trigger | Scheduled, monthly | Break-glass, human-initiated |

Same code path. Same sequence. Same validation steps.

The monthly test is not a rehearsal of the recovery procedure — it *is* the
recovery procedure, with a different flag. The procedure cannot go stale,
because it executes twelve times a year whether or not an incident occurs.

*It is an operational failure to recover one way for testing and another way for
real, and discover the real path does not work.*

---

## Automated restore testing

Implemented as a **Step Functions workflow** per member account, monthly:

1. Select the latest recovery point
2. Restore into an isolated test VPC (test mode)
3. Wait for resource health
4. Run shallow validation — database connects, row counts plausible, volumes
   mount, file checksums match
5. Record elapsed time against the RTO target
6. Tear down completely
7. Emit an evidence record to Truveon

**A human reviews the result. A human does not execute the test.**

Only failures and threshold breaches page. Successful tests produce evidence and
nothing else.

### Trend tracking

Restore duration is tracked as a trend, not just a pass/fail. Duration creeping
toward the RTO target is visible months before it breaches — which is the
difference between a capacity conversation and an incident.

---

## Threshold alerting

Two tiers on recovery metrics, with different routing:

| Tier | Condition | Routing |
|---|---|---|
| **Warning** | Restore duration approaching RTO target | Jira ticket with owner and due date; email; no page |
| **Breach** | RTO target exceeded, or restore failed | PagerDuty page |

> **Design note — thresholds not yet set.** The *principle* is two tiers with
> differentiated routing. The actual percentages are set per application once
> the workload is understood. Indicative discussion suggested warning around
> 75% of RTO and breach at or near 100%, but these are not decided.

The warning tier must produce an owned, dated ticket — not an item in a queue.
A documented known issue nobody closed is worse at audit than no ticket at all.

---

## Environment resilience posture

- **Production** — multi-AZ, always on, full backup policy
- **UAT** *(opt-in)* — single AZ, conservatively schedulable, **full backup
  policy at reduced frequency**, restore testing included
- **Test** — single AZ, aggressively schedulable, reduced retention
- **Dev** — single AZ, aggressively schedulable, reduced retention

Governance, logging and detective controls are identical across all tiers. Only
resilience and cost differ.

**UAT is included in restore testing** because it holds real data. An
environment carrying regulated data with an untested restore path is an audit
finding regardless of its SLA status. See
[14 — Environment Tiers](14-environment-tiers.md).

---

## Multi-region

Not part of the standard tier. The 4-hour RTO does not require it, and it is
incompatible with the 99.995% SLA bonus tier — those two commitments cannot
coexist on a single-region multi-AZ baseline.

Multi-region is therefore a **premium tier** with its own architecture and price.
Where adopted, region pairing is chosen deliberately for replication latency.

**Platform services are multi-region regardless of tenant tier.**

---

## Testing the assumptions

Two things are load-bearing and quietly breakable:

1. **Backup immutability** — periodically verify that a member account with full
   local admin genuinely cannot delete or alter its own backups
2. **Restore isolation** — verify the test VPC is genuinely isolated and a test
   restore cannot reach production

Both are the kind of assumption that breaks silently when an SCP is adjusted.