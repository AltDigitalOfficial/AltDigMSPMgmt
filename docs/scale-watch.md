# Scale Watch

Things to keep an eye on as the estate grows past one tenant. Nothing here is
a defect and nothing here is scheduled. Each item is something that is
**correct at current scale and stops being correct at some identifiable
point**, recorded with the point.

Distinct from the three lists it sits beside:

- [build-backlog.md](build-backlog.md) — work the build created or discovered
- [deviations.md](deviations.md) — accepted departures from the design package
- [design/open-items.md](../design/open-items.md) — the design package's own
  open questions

**Origin.** `estate/estate.json` was given to an external model with no context
on 2026-09-22, and it returned a review of three areas: response automation,
recovery testing, and instrumentation/PagerDuty. The observations below are the
ones that survived being checked against the code. Where the review's premise
was wrong, that is recorded too — a corrected premise is worth as much as a
finding, because otherwise it comes back.

---

## W-001 · Action properties are enforced in code and declared in prose

**Watch.** The catalog states the permitted/refused decision as a comment block
at the top of `runbooks/catalog.yaml`, argued action by action. The properties
that decision turns on are real and enforced — but they live in two
hand-written preconditions rather than as attributes of the action:

| Property | Where it lives today |
|---|---|
| requires redundancy | `rails.check_redundancy`, per resource type, raises on unknown |
| self-reversing | `rails.check_scale_in_policy`, ECS only |
| settle time | `catalog.settle_seconds`, keyed by action |
| max repetitions | circuit breaker, 3/hour, global rather than per action |
| success signal | `engine.alarm_is_breaching` — the alarm, for every action |
| irreversible | prose only |
| creates ongoing cost | prose only |
| blast radius | prose only |

The review's suggestion is to make all of them declared attributes on each
action and have the engine **derive** the permitted stage, instead of an author
asserting a stage and a reviewer arguing about it.

**Why not now.** There are four permitted actions. A derivation engine over
four actions is more machinery than the thing it replaces, and the two
properties that actually gate behaviour are already enforced in code — where
they belong, because `check_scale_in_policy` needs an API call to answer, not a
YAML field. `runbooks/src/rails.py:416` already carries the distinction the
review names as the important one: *reversible is not the same as
self-reversing.*

**What would change the answer.** The prose block scales badly in one specific
way: a new action's stage is argued from scratch against precedent, and
precedent is a comment. That is fine for the fifth action and bad for the
fifteenth.

**Trigger:** the eighth permitted action, or the first time somebody proposes
**promoting** a refused action. RDS failover is the likely first — it is
refused pending timing data, which is a promotion path by another name.

---

## W-002 · The data that would justify autonomy expires in 30 days

**Watch.** The review's strongest suggestion is promotion by evidence: after a
year, `ecs-memory-high` fired 83 times, the cycle was permitted 72, it resolved
71, no customer-visible impact, median 94 seconds — *that runbook has earned
more autonomy*. Everything needed to say that is emitted today. Almost none of
it is retained where it could be read.

| Signal | Emitted to | Retention | Scope |
|---|---|---|---|
| attempt + outcome | `platform-remediation-history` DynamoDB | **30 days** (`rails.py:150`) | per member account |
| change record | `/aws/platform/change-records` | 2557 days | **per member account** |
| dossier | `/aws/platform/incident-dossiers` | 365 days | **per member account** |

Two separate problems, and the second is the harder one. The 30-day TTL is
deliberate — the rails only ask about the last hour, and the TTL is what bounds
the rate-limit scan — so the durable record is the change record, which does
survive seven years. But it lands in *each account's own log group*, and
nothing aggregates it. There is no path today that answers "how did
`ecs-cpu-high` behave across the estate this quarter" without reading N
accounts by hand. `change/src/reconcile.py` does not read the change-record log
group; it reconciles against Jira.

**The management question this blocks** is the one worth having: *which kinds
of incidents are actually being removed from the human workload, safely?* That
is the number that says whether the operations model scales in reality rather
than on paper.

**Why not now.** One account is generating this data and it is the exercise
account. A cross-account aggregation built before there is anything to
aggregate is a pipeline that gets rebuilt when the first real shape arrives.

**Trigger:** the second tenant with response automation live — or earlier, the
first time anyone proposes letting a runbook **suppress or delay a page**. That
proposal must not be answerable from impression. Running automation alongside
paging rather than in front of it is the current deliberate choice (B-041); the
only thing that should be allowed to change it is this dataset.

---

## W-003 · Restore tests read the local copy, not the protected one

**Watch.** `recovery/src/worker.py:484` selects the most recent COMPLETED
recovery point via `ListRecoveryPointsByResource`, running in the account that
owns the resource. The backup policy copies every recovery point to
`platform-backup-copies` in the backup account precisely because *"a copy
beside the original survives neither account compromise nor a mistake"*
(`recovery/30-backup-policy.yaml:244`).

So the monthly exercise proves the local copy restores. The disaster story says
the member account is untrusted and recovery comes from the cross-account copy
— and that path is never traversed by the test.

This is the same failure shape the containment exercise already avoids: that
test verifies evidence arrived by reading it from the **receiving** Forensics
account, rather than trusting the sending side's report.

**Why not now.** B-043 — there are no backup plans and no vaults deployed
outside the sandbox fixture. "No recovery point" is the correct answer in every
account today, and the workflow reports it as an outcome rather than an error.

**Trigger:** the first account where `30-backup-policy.yaml` is actually
deployed. At that point at least some exercises must restore **from the copy**,
which needs a cross-account restore role and a source-selection parameter the
workflow does not have.

---

## W-004 · RTO is measured; RPO is captured and never evaluated

**Watch.** `evaluate_thresholds` (`recovery/src/worker.py:1089`) compares
elapsed restore time to the tenant's RTO at 50%/80% and trends it across runs.
There is no equivalent for RPO. The recovery point's `CreationDate` is carried
in `_point_summary` and written into the record — the input exists — but
nothing compares it to the 2-hour RPO that `30-backup-policy.yaml` is built to
deliver.

A restore that completes in 18 minutes against a 4-hour RTO, from a recovery
point six hours old against a 2-hour RPO, currently reports as a pass. It is
the failure the customer actually cares about.

Two clean empirical numbers fall out of one test run:

```
observed recovery-point age   → RPO evidence
observed restore duration     → RTO evidence
```

**Why not now.** Same as W-003 — no real recovery points. But this one is
cheap: an RPO parameter beside the existing RTO parameter, a subtraction, and a
third classification alongside the two duration thresholds.

**Trigger:** the same deployment as W-003. Do these together — they touch the
same two functions.

---

## W-005 · Coverage reconciliation, and rotation of the test subject

**Watch.** Two related gaps that become real at the same moment.

**Coverage.** A monthly restore test proves *the resource it selected* is
recoverable. It cannot prove nobody launched three databases and left two
outside backup selection. The chain that needs reconciling is:

```
resource requiring protection
  ↔ backup plan selection
    ↔ recent successful recovery point
      ↔ cross-account copy
        ↔ eligible for restore test
```

The machinery for this already exists in a different subsystem: the
instrumentation drift sweep reconstructs expected alarms, corrects modified
ones, recreates missing ones and reports orphans without deleting them. The
same sweep shape over backup coverage is the answer, not a new invention.

**Rotation.** Restoring everything every month is expensive theatre; restoring
the same friendly database forever proves one database. A selection policy —
never tested first, then longest since tested, then most critical, with some
randomisation — turns "we run monthly restore tests" into a materially stronger
statement: *every production recovery class is exercised monthly, and protected
resources rotate through live restoration by criticality and time since last
successful recovery.* That is a scheduler change, not a workflow change.

**Trigger:** the first account holding more than one eligible protected
resource. Below that, selection is not a choice and coverage is one row.

---

## W-006 · The validation split is a contract question, not just a build one

**Watch.** B-044 tracks the missing in-guest executor. What B-044 does *not*
record is who defines what "alive" means, and that is the part that needs
somebody else's agreement.

There is a five-level ladder here and the build sits on level 3:

| | |
|---|---|
| 1 | AWS says a recovery point exists |
| 2 | AWS Backup says the restore succeeded |
| 3 | **the restored resource reaches the expected AWS state** ← today |
| 4 | the contents are intelligible and internally consistent |
| 5 | the application can use them |

Levels 4 and 5 are not AltDigital's to define. The responsibility split the
review proposes is the right one, and belongs in the partner / app-team
agreement rather than being discovered during the first real test:

- **AltDigital MSP** — can we recover the infrastructure and the data?
- **Application team** — what proves this recovered application is usable?
- **Platform** — run both, combine the evidence, retain the result.

**Trigger:** before the in-guest executor is built (B-044). Building an
execution harness without knowing who supplies the checks produces a harness
with our guesses hard-coded into it.

---

## W-007 · A Recovery Confidence Report is the product-shaped output

**Idea, not a gap.** Once W-003 through W-006 are closed, the evidence supports
a one- or two-page per-tenant artefact generated from data already held:
coverage N/N, protected copies N/N, worst observed recovery-point age against
RPO, latest live restore against RTO, 12-month restore success count,
application validation result, isolation validation result, duration trend,
last hostile-account exercise, exceptions — with the machine evidence retained
behind every green line.

The difference it sells is the difference between *"your backups run nightly"*
and *"last month we recovered your system from a protected off-account copy,
measured how stale the data was and how long it took, ran your team's health
checks, and here is the evidence."*

**Trigger:** the first tenant month with real backups. Fold into R2 (the
monthly service report template) rather than standing up a second report
pipeline.

---

## W-008 · A hostile-account recovery exercise

**Watch.** The recovery analogue of the containment blast-radius test: behave
as though the workload account is unavailable or compromised, and demonstrate
that AltDigital can locate the protected material, obtain the KMS and IAM
access, restore into the recovery environment and validate it **without relying
on anything mutable inside the failed account**.

Quarterly or annual, not monthly, and not for every tier.

**Trigger:** after W-003 (the cross-account path exists) and B-044 (validation
means something). Before the first customer-facing DR attestation, whichever is
sooner.

**Explicitly not doing:** chaos engineering. AZ failure injection, latency
injection, random termination. The effort/value curve strongly favours closing
the chain that already exists — discover → prove coverage → recovery point →
copy outside the blast radius → restore through the real mechanism → validate
infrastructure → validate data → validate application → measure RPO → measure
RTO → tear down → retain → trend → report — before making the resilience story
more exotic.

---

## W-009 · Per-object PagerDuty services: premise wrong, threshold real

**Corrected premise.** The review flagged "one technical service per object" as
a scale risk, reading *object* as *AWS resource* and projecting thousands of
services. It is not that. `pagerduty/modules/tenant/main.tf:76` builds one
service per **tier × app**, where apps come from the `app` field on the account
record, plus one reserved `infra` object per tier (B-127). The arithmetic is:

```
30 clients x 3 tiers x (2 apps + 1 infra) = 270 services
```

Manageable, and each one is a meaningful ownership and escalation boundary
rather than a resource.

**What survives the correction.** The registry's `app` field is the finest
granularity the platform has, and the review is right that the *logical
service* boundary, not the AWS object, is the useful one. That is already the
design. What to watch is pressure to subdivide below `app`, which would be the
first step toward the failure mode described.

**Trigger:** the first request for a technical service that is not a tier, not
an app and not `infra`. Refuse it once and ask what escalation difference it
expresses; if there is a real one, the registry needs a new field — PagerDuty
does not need a new convention.

---

## W-010 · The routing asymmetry is now a hard rule

**Closed into policy.** The review's framing matches what the router already
enforces:

> Discovery decides what must be monitored. The central router decides who must
> care, when, and how urgently. Individual components never promote themselves
> into paging.

An emitter may downgrade its own event; it cannot escalate beyond what the
tier's routing policy permits. This was violated once — meta-alarms bypassed
tier routing and dev drift paged people (B-117) — which is why it is worth
stating rather than leaving as an implementation property. Promoted to
CLAUDE.md's hard rules on 2026-09-22.

The companion property, also worth keeping stated: **no supported resource
should be able to appear in a managed account and quietly become invisible to
the MSP.** Unknown resource types become instrumentation exceptions rather than
being filtered out at the EventBridge layer, and exceptions are retained 365
days because an unmonitored resource is a compliance-relevant gap, not a log
line.

---

## The sentence underneath all of it

The catalog's governing test — *the test is reversibility, not cost* —
generalises, and the generalisation is worth having as the operating principle
for the whole platform:

> **Automate when the action is bounded, observable, reversible, and its
> success can be independently verified. Escalate when any of those four stops
> being true.**
