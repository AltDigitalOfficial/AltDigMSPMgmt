# 06 — Observability

## The core problem

AltDigital does not have advance knowledge of what resources exist in a member
account. Dev teams create things without consultation. Monitoring must therefore
be **discovery-driven and self-applying**, not declared up front.

Monitoring is **light in dev and test, moderate in UAT, pervasive in
production**. Alarm routing differs by tier — see
[14 — Environment Tiers](14-environment-tiers.md).

---

## Discovery

Inventory maintains itself from controls already running:

- **AWS Config** — continuous resource inventory as things appear
- **CloudWatch Application Signals** — discovers services and their dependencies
  without declaration
- **CloudTrail + EventBridge** — near-real-time "something was just created"

No separate inventory process is required.

---

## Auto-instrumentation

**Config detects. EventBridge triggers. Lambda instruments.** Alarms do not
create themselves — this layer is what makes them appear.

### Flow

1. Resource creation event on EventBridge
2. Instrumentation Lambda matches resource type + account tier
3. Applies the standard alarm set for that type
4. Tags the alarms `platform-managed: true` and records the action
5. Anything unhandled → **exception record**, never silence

### Standard alarm sets

**Summary.** The full specification — metric, namespace, statistic, period,
evaluation periods, comparison operator, per-tier threshold and missing-data
treatment — is in
**[`06a-alarm-specification.yaml`](06a-alarm-specification.yaml)**. That file is
the authoritative source and is consumed directly as configuration by the
instrumentation Lambda. The table below is orientation only.

| Resource | Alarms applied |
|---|---|
| EC2 | CPU, status check, disk, memory*, **agent telemetry gap**, instance age |
| RDS / Aurora | Connections, CPU, free storage, freeable memory, replica lag, failover events, **auto-restart detection** |
| ECS / Fargate | Running vs desired, task restart rate (with stopped reason + exit code), CPU, memory |
| Lambda | Error **rate**, throttles, duration vs timeout, DLQ depth, concurrency |
| ALB / Target Group | ELB 5xx rate, target 5xx rate, p99 response time, rejected connections, unhealthy hosts, **no healthy hosts** |
| S3 | 5xx rate, replication failure, public access change |
| DynamoDB | Throttles, system errors, consumed vs provisioned |
| SQS | Oldest message age, DLQ depth |
| ElastiCache | CPU, evictions, memory pressure |
| API Gateway | 5xx rate, p99 latency |
| EFS | Burst credit, percent IO limit |
| ASG | In-service below desired, failed scaling |

\* Memory and disk require the **CloudWatch agent**. That is handled by the
baseline AMI and an SSM association applied to anything new — otherwise this is
a silent gap.

### Three things in the specification worth knowing about

**Thresholds are marked `[DEFAULT]` or `[TUNE]`.** `[DEFAULT]` values are
conventional and may be adopted as-is. `[TUNE]` values are workload-specific and
require per-application review; a `[TUNE]` threshold left null means the alarm
**cannot be created and raises an exception**, not a silent skip.

**Some thresholds are relative, not absolute.** RDS connections is a percentage
of the instance class `max_connections`; free storage is a percentage of
`AllocatedStorage`; Lambda duration is a percentage of the configured timeout.
The instrumentation Lambda resolves these at alarm-creation time. A fixed number
would be wrong across instance classes and function configurations.

**Response-time thresholds derive from the questionnaire.** Field 7.4
(acceptable response time) is used **three times**: the canary timeout, the SLA
pass condition, and the ALB / API Gateway p99 alarm. It is never hardcoded — see
[Canaries cannot be inferred](#canaries-cannot-be-inferred).

### Tier overrides worth noting

Two alarms deliberately override their tier's default routing:

- **No healthy hosts** pages in UAT as well as production. Real users are in UAT
  and a dead environment is a customer-facing failure.
- **Agent telemetry gap** escalates by tier but never pages in dev — it is a
  visibility failure there, not an outage.

### Drift detection

If a platform-managed alarm is deleted or modified, it is **recreated and the
event is logged**. Without this, coverage erodes quietly and the discovery is
made during an incident.

---

## Synthetic monitoring

Generic infrastructure alarms tell you a thing is *running*. They do not tell you
it *works*. Canaries close that gap without requiring understanding of the
application's internals.

**CloudWatch Synthetics** is the native tool. Canaries are Node or Python
scripts; the Node runtime uses Puppeteer, the Python runtime uses Selenium — so
real browser automation, not just an HTTP ping.

- Heartbeat checks, API canaries, and multi-step user journeys
- Screenshots and HAR files captured on failure — genuinely useful for triage
- Runs from inside the account, so private endpoints are reachable

### Existing Playwright suites

There is no native Playwright runtime in CloudWatch Synthetics. Where a dev team
already maintains Playwright tests, run them on **Fargate on a schedule** and
publish results to CloudWatch as custom metrics. The alarm path downstream is
identical.

### Canaries cannot be inferred

A canary needs to know what a meaningful transaction looks like. This is the one
piece that cannot be auto-discovered, so it becomes an intake question:

> *What does "working" look like? What would a user notice first if it broke?*
> Endpoint, success condition, acceptable response time.

Dev teams answer this well when asked directly. Where AltDigital is building the
application (e.g. the spreadsheet-and-Python case), AltDigital defines it.

**That answer is used twice** — as the canary definition *and* as the SLA
measurement condition. Same definition, two consumers.

---

## Alerting and routing

**PagerDuty** owns on-call rotations and escalation.

```
CloudWatch alarm ─┐                                     ┌─→ PagerDuty (actionable now)
Config finding    ├─→ EventBridge ─→ routing ───────────┼─→ Jira (owned, dated)
Security Hub      │                                     ├─→ Review queue (no page)
GuardDuty         ┘                                     └─→ Commercial (client conversation)
```

The fourth destination is defined by **who needs to have a conversation**, not by
severity — see [12 — Commercial Access](12-commercial-access.md).

Routing is by severity **and** by environment. Production canary failure pages.
A dev environment CPU alarm does not.

Nothing pages that a human cannot act on. Low-severity findings accumulate for
review rather than waking someone — the fastest way to destroy an on-call
rotation is to page it for things it cannot fix.

---

## The dev team notification loop

When new resources appear in an account, a **batched digest** goes to the named
dev team contact, CC'd to an AltDigital address:

> Here is what appeared. Here is what we instrumented generically. Reply if it
> needs something more specific.

**Batched, not per-resource.** A Terraform run creating forty resources must not
generate forty emails; that gets filtered to a folder within a week. Daily or
weekly digest per account.

This does compliance work quietly: it is documented evidence that monitoring
requirements were communicated to the application owner. If something later
fails unmonitored, the trail shows we asked.

**Contact freshness** — the contact is a field in the onboarding record,
reviewed whenever anything else about the account changes. The CC to AltDigital
means a bounce or a departed contact is visible to us rather than lost. Contacts
rot because nobody revisits them; tie the review to a moment that already
happens.

---

## Reporting

Consistent with P5 — the platform produces a readout, not a task list:

| Report | Cadence | Contents |
|---|---|---|
| Instrumentation digest | Daily/weekly, per account | Resources appeared, alarms applied, exceptions pending |
| Platform health | Weekly | Coverage, drift events, restore test results, exception backlog |
| Dev team digest | Daily/weekly, per account | Extends the instrumentation digest — see [11](11-developer-access.md) |
| Tenant service report | Monthly | Availability vs SLA, incidents, changes, patch posture, cost |

---

## Service level objectives

Availability is measured as **successful synthetic transactions over total
attempts**, per service, per calendar month. Binary pass/fail.

A check passes only if it returns success **and** completes within the stated
timeout. This preserves the intent that availability is measured end-to-end and
independent of performance tuning, while excluding the pathological case of an
application that responds in 45 seconds and is functionally broken but
technically "up".

Commercial tiers, exclusions and measurement authority are in
[10 — Commercial Model](10-commercial-model.md).