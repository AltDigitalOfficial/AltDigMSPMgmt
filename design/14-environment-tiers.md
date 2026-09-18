# 14 — Environment Tiers and Scheduling

Supersedes the three-environment table in [02](02-platform-architecture.md).

---

## The four tiers

| Tier | Purpose | Who is in it |
|---|---|---|
| **dev** | Where developers build. Things break. Nobody stresses much. | Developers |
| **test** | QA verifies function against specification. | QA team |
| **uat** | Do the users accept this, or are adjustments needed? | Real end users |
| **prod** | Production. | Everyone |

**UAT is optional and opt-in.** It is not provisioned by default, because it
raises the customer's AWS cost and not every customer runs a formal acceptance
stage. It is trivially added later — a single additional vesting run — so the
default costs nothing to reverse.

> Opt-in rather than opt-out is deliberate. A customer who never uses UAT should
> not have to notice it in order to stop paying for it.

---

## UAT is not "test with a longer name"

The distinction that matters is not its purpose but **what data it holds.**

Real users performing acceptance testing bring real data with them. Under HIPAA
especially, clinicians test with actual patient records, because synthetic data
does not exercise the workflows they care about. This is not misconduct — it is
how UAT works.

Therefore **UAT splits the two axes** that dev and test keep aligned:

| Axis | dev / test | **uat** | prod |
|---|---|---|---|
| **Data handling** | Non-production | **Production-equivalent** | Production |
| **Resilience** | Non-production | **Non-production** | Production |

### What production-equivalent data handling means for UAT

- **In scope** for the tenant's compliance frameworks
- **Full retention floor** — same as production, not the reduced non-production tier
- Full detective controls (already universal, but now with production severity routing)
- Macie, data protection policies and Inspector findings treated at production severity
- Regulated data present is **expected**, not a scope-change finding

### What non-production resilience means for UAT

- Single-AZ
- No SLA commitment
- Reduced backup frequency and retention (subject to the retention floor above)
- No page routing — alarms route to Jira and the digest, not PagerDuty
- Schedulable (see [HOOPs](#hoops--hours-of-operation))

### Why not treat UAT as test and rely on scope change

The alternative was to provision UAT as test-equivalent and let the
scope-change mechanism in [11](11-developer-access.md) catch real data when it
appears.

Rejected because the scope upgrade would fire on **nearly every UAT account**.
A control that triggers almost always is noise, not signal — and it would train
everyone to dismiss the one case where it matters.

---

## Revised environment matrix

| | dev | test | uat | prod |
|---|---|---|---|---|
| Provisioned by default | Yes | Yes | **No — opt-in** | Yes |
| Availability zones | Single | Single | Single | Multi-AZ |
| Compliance scope | Out | Out | **In** | In |
| Retention floor | Reduced | Reduced | **Full** | Full |
| Regulated data expected | No | No | **Yes** | Yes |
| Detective controls | Full | Full | Full | Full |
| Monitoring depth | Light | Light | **Moderate** | Pervasive |
| Canaries | No | Optional | **Yes** | Yes |
| Alarm routing | Digest | Digest + Jira | **Jira + commercial** | PagerDuty |
| SLA commitment | No | No | **No** | Yes |
| Backup | Reduced | Reduced | Full policy, reduced frequency | Full policy |
| Restore testing | No | No | **Yes** | Yes |
| Schedulable (HOOPs) | Yes, aggressive | Yes, aggressive | **Yes, conservative** | **No** |
| Developer console access | Full within guardrails | Read + limited ops | **Read only** | Read only |
| Path to change | Direct | `AppDeployer` | `AppDeployer` | `AppDeployer` |
| Change reconciliation | No | Yes | Yes | Yes |

**UAT restore testing is included** because the data is real. An environment
holding regulated data with an untested restore path is an audit finding
regardless of its SLA status.

---

## HOOPs — Hours of Operation

A **HOOP** is a declared schedule during which an environment is running. Outside
the HOOP, compute is stopped. Storage persists; compute does not bill.

Available for **all three non-production tiers**. Never for production.

### Why it is a customer-facing lever

HOOPs are the primary mechanism a customer has to manage their own AWS cost
without changing their architecture. A dev environment running 50 hours a week
instead of 168 costs roughly 30% of the always-on figure for its compute.

That is a conversation worth having at onboarding and again whenever cost
becomes a topic — it gives the customer agency rather than leaving them with a
bill they cannot influence.

### Realistic expectations per tier

| Tier | Typical HOOP | Saving |
|---|---|---|
| **dev** | Business hours, weekdays | Substantial |
| **test** | Business hours, weekdays; sometimes extended for release cycles | Substantial |
| **uat** | Extended hours, often including weekends | **Modest — see below** |

**Be honest about UAT.** Users test on their own schedule, frequently outside
AltDigital's working hours, and a UAT environment that is down when a customer's
user group logs in becomes a support ticket and an erosion of trust. The cost
saving on UAT is genuinely smaller than on dev and test, and should be
represented that way when it is priced.

### What a HOOP actually controls

| Stopped | Not stopped |
|---|---|
| EC2 instances | EBS volumes (storage continues to bill) |
| RDS / Aurora instances | S3, backups, snapshots |
| ECS service desired count → 0 | Load balancers (unless torn down) |
| Auto Scaling groups → min/desired 0 | NAT gateways (unless torn down) |
| | Elastic IPs, VPC endpoints |

Storage and network baseline continue to cost money. HOOPs reduce compute spend,
not total spend, and the reporting should say so plainly rather than implying a
proportional saving.

---

## The RDS seven-day problem

**A stopped RDS instance is automatically restarted by AWS after 7 days.** The
same applies to Aurora clusters.

This is not a bug to work around; it is an AWS behaviour with real consequences
for this design:

- A HOOP that keeps an environment down over a long holiday will see RDS come
  back up on day 8 and bill normally
- An environment genuinely idle for a season cannot be handled by scheduling at
  all

**Implications:**

1. The scheduler must **re-stop** instances that AWS has auto-started, and must
   record each occurrence — otherwise the cost report will not match expectation
   and nobody will know why.
2. **Long idle periods require teardown, not scheduling.** See
   [Seasonal lifecycle](#seasonal-lifecycle).
3. The seven-day boundary is worth surfacing in the cost report: *this
   environment was auto-restarted twice this month; consider teardown.*

---

## Turndown and turnup must be tested

Same principle as restore and containment (**P4**): the scheduled path and the
manual path are the same code, and an untested turnup is an outage waiting for a
Monday morning.

**Turnup is the higher risk of the two.** A failed turndown costs money. A failed
turnup costs a QA team or a customer's user group their working day, and in UAT
that is a customer-facing failure.

### Required testing

| Test | Cadence | Asserts |
|---|---|---|
| **Turndown completeness** | Every scheduled turndown | Everything that should stop, stopped. Nothing that should persist, terminated. |
| **Turnup success** | Every scheduled turnup | All resources running, health checks passing, dependencies resolved |
| **Turnup duration** | Every scheduled turnup | Elapsed time trended; creep is visible before it becomes a complaint |
| **Functional validation after turnup** | Every scheduled turnup | The canary passes — the environment is not just running but working |
| **Full cycle from cold** | Monthly, per environment | A complete down-and-up in a window where failure is safe |
| **Data integrity across cycle** | Monthly | Row counts and checksums consistent before and after |

### Failure handling

- **Turnup failure is an incident**, not a scheduling glitch. In UAT it pages;
  in dev and test it raises a ticket and notifies the dev team contact
  immediately rather than in the next digest.
- **Partial turnup** — some resources up, some not — is the dangerous state,
  because it looks available and is not. The validation step must assert the
  full expected set, not just absence of errors.
- **Turnup must be manually triggerable** at any time, through the same path, by
  the customer's contact as well as by AltDigital. A team working an unplanned
  weekend should not need a ticket.

### Interaction with the rest of the platform

Scheduling touches more than it looks:

| Interaction | Requirement |
|---|---|
| **Alarms** | Suppressed during scheduled down, or every turndown pages |
| **Canaries** | Paused during down, resumed and used as the turnup validation |
| **Telemetry gap detection** | Must distinguish scheduled down from an agent being killed ([11](11-developer-access.md)) — otherwise every turndown is a false positive |
| **Restore testing** | Must not collide with a turndown window; schedule inside the HOOP |
| **Patch and maintenance windows** | Must fall inside the HOOP, or patching silently never runs |
| **Backup** | Continues regardless — backup targets storage, not running compute |
| **Change reconciliation** | A deployment attempted against a stopped environment is a legitimate failure, not a finding |
| **Cost reporting** | Actual vs expected HOOP hours, with auto-restart events called out |

The telemetry-gap interaction is the one most likely to be missed. The detection
logic distinguishes *instance alive, telemetry silent* from other cases — a
scheduled stop must be an explicitly recognised state, not left to look like an
anomaly.

---

## Seasonal lifecycle

UAT usage varies enormously and should not be assumed:

- A customer releasing **quarterly** may want UAT stood up for two weeks and gone
  for ten
- A customer releasing **continuously** should keep UAT up year-round, and
  scheduling it aggressively will only frustrate their users

**Neither should be the default assumption.** The intake asks; the answer drives
whether the account is provisioned with a HOOP, a teardown/rebuild cycle, or
neither.

### Teardown and re-vest

For genuinely seasonal environments, **teardown is cheaper and cleaner than
scheduling** — and it sidesteps the RDS seven-day problem entirely.

This is the deprovisioning path from [13](13-client-provisioning.md) doing useful
work outside of exit:

1. Final backup taken and verified
2. Environment torn down; account retained in `dormant` state
3. Registry record preserved with all parameters
4. Evidence retained in Truveon; retention clock continues
5. Re-vest from the same registry record when the next cycle begins
6. Restore data from the retained recovery point

**The account is not deleted.** Deleting and recreating an AWS account loses the
account ID, the evidence continuity and the Truveon linkage. Dormant accounts
retain everything and cost almost nothing.

**Re-vest must be tested**, on the same principle as everything else. A seasonal
environment that cannot be brought back is a much worse failure than one that
was never torn down.

---

## Cost presentation

The commercial view should express the levers clearly, because this is where a
customer can act:

| Lever | Effect | Tradeoff |
|---|---|---|
| **Decline UAT** | Removes an entire environment's cost | No formal acceptance stage |
| **HOOPs on dev/test** | Substantial compute saving | Unavailable outside hours |
| **HOOPs on UAT** | Modest compute saving | Risk of unavailability during user testing |
| **Seasonal teardown of UAT** | Near-total saving during dormancy | Re-vest lead time before each cycle |
| **Right-sizing** | Varies | Requires application knowledge |

Present HOOP savings as **compute only**, with storage and network baseline shown
separately. Overstating the saving creates a bad conversation at the first
invoice.