# 13 — Client Provisioning Orchestration

Account vesting ([02](02-platform-architecture.md)) creates AWS accounts.
**This document covers the whole onboarding** — AWS, Jira, PagerDuty, Truveon,
billing, human tasks, verification and go-live.

---

## It is a saga, not a pipeline

None of this can be transactionally rolled back. You cannot un-create an AWS
account. A Jira project half-configured is not a clean failure state.

The design properties that follow:

| Property | Why |
|---|---|
| **Idempotent** | Every step is safely re-runnable |
| **Resumable** | Step 9 can run after step 7 failed at 2am |
| **Fails closed** | A half-provisioned tenant is worse than none — it *looks* supported |
| **Self-cleaning** | Failed provisioning is unwound by the deprovisioning path, not by hand |

---

## Partner precondition

A client cannot be onboarded beneath a partner that does not exist or whose
relationship is incomplete.

**Checked before step 1, and re-checked at go-live** — a partner relationship can
lapse between the two.

| Condition | Result |
|---|---|
| Partner does not exist | Onboarding cannot begin; partner onboarding required first ([15](15-partner-model.md)) |
| Partner contract or BAA incomplete | **Block.** An account live beneath an incomplete partner relationship has no defensible notification path. |
| Partner notification window null | **Block.** The incident clock chain cannot run. |
| Partner state lapsed between start and go-live | Block at the go-live gate |

Partner onboarding is a separate, one-time process — see
[15 — Partner Model](15-partner-model.md).

---

## The registry is the spine

Every system is provisioned **from** the registry record, and each writes back
its own identifiers — AWS account IDs, Jira project key, Truveon tenant ID,
PagerDuty service and routing key IDs, Cost Category mapping.

That record is what makes the saga resumable, and what deprovisioning reads
later.

**The registry entry is created first, in `provisioning` state. Nothing else
starts until it exists.**

---

## Ordering

Dependencies here are real, and reversing them creates silent gaps.

| # | Step | Why here |
|---|---|---|
| **0** | **Partner precondition** | Verify the partner exists, is `active`, and has a non-null notification window. **Blocks, does not warn** — see below |
| 1 | **Registry entry** | The anchor |
| 2 | **Truveon tenant** | Before any account emits evidence, or early evidence is lost rather than buffered |
| 3 | **PagerDuty services, escalation policy, routing keys** | Before alarms exist, or they fire into the void |
| 4 | **Jira project and change workflow** | Before reconciliation has a target |
| 5 | **AWS accounts + baseline** | The vesting pipeline ([02](02-platform-architecture.md)) — three or four accounts depending on the UAT election |
| 6 | **Instrumentation, canaries, backup, restore test schedule, HOOP schedules** | Requires accounts |
| 7 | **Cost Categories, budgets, billing configuration** | Requires account IDs |
| 8 | **Verification sweep** | Proves the above actually works |
| 9 | **Go-live gate** | Human approval |

Steps 2–4 are external systems and are therefore **first**: they are the most
likely to fail on a credential or permission problem, and failing before AWS
accounts exist is far cheaper than failing after.

See `diagrams/provisioning-saga.mermaid`.

---

## States

`provisioned` and `live` are deliberately different, because three things key off
the transition: **SLA measurement starts, billing starts, and the account enters
the on-call rotation.**

```
provisioning ──→ provisioned ──→ verifying ──→ live ──→ dormant
                                      │                    │
                                      └──→ blocked         └──→ (re-vest) ──→ live
```

**`dormant`** applies to seasonal environments — torn down, account retained,
registry record and evidence preserved, re-vestable from the same record. See
[14 — Environment Tiers](14-environment-tiers.md).

An account may sit in `provisioned` indefinitely. It has infrastructure,
controls and evidence flowing — but **no SLA commitment and no page routing.**
That is the correct state for an application still being migrated.

---

## Human tasks

Created in Jira **by the pipeline**, inside the workflow rather than beside it.
The pipeline waits on the blocking set.

### Blocking — no go-live without them

| Task | Owner |
|---|---|
| Contract and BAA executed; notification window recorded (client level) | Art / Wayne |
| Partner relationship confirmed active | Art / Wayne |
| Responsibility matrix signed | Customer + Jamie |
| Critical journey defined (questionnaire 7.1) | Customer, or Jamie where AltDigital builds the app |
| Canary validated — does it actually test the right thing | Platform |
| Licence entitlement evidence, where applicable | Customer |
| Billing configured; first invoice modelled | Tina |
| Go-live approval | Jamie |

### Non-blocking — tracked and dated, not gating

- First restore test review
- Penetration test scheduled
- Tenant contacts confirmed
- Monitoring tuning after the first instrumentation digest

> The distinction matters. If everything is blocking, nothing goes live. If
> nothing is blocking, something goes live without a BAA.

---

## What gets created where

### PagerDuty

- Service per application (production only)
- Escalation policy referencing the on-call schedule
- Routing keys → Secrets Manager, referenced by the alarm layer
- **Business service** for the client-conversation event class
  ([12](12-commercial-access.md)) — notification rules, not paging rules

### Jira

- Project or component, inheriting the **shared** configuration scheme
- Change request workflow wired to the reconciliation engine
  ([08](08-change-and-release.md))
- Onboarding epic with the task set above
- Recurring obligations: annual penetration test, annual responsibility matrix
  review, quarterly access review

### Truveon

- Tenant
- Framework configuration from questionnaire §2
- Retention floor
- BAA notification window
- Evidence receipt confirmation endpoint for the verification sweep

### Billing

- Cost Category mapping
- Budget with anomaly detection
- Flat fee + percentage configuration
- Truveon flat fee
- First monthly report scheduled

---

## The verification sweep

**This is what makes provisioning a control rather than a script.** Before
go-live, prove:

- [ ] Truveon received evidence from all three accounts; sequence check clean
- [ ] A synthetic test alarm actually reached PagerDuty and paged the correct rotation
- [ ] The canary is running and passing
- [ ] A test change reconciled correctly against Jira
- [ ] Backup policy attached; a first recovery point exists
- [ ] Guardrail test harness passes against these specific accounts
- [ ] Cost Category mapping resolves — the account appears correctly in the
      commercial by-customer view
- [ ] **BAA notification windows set and non-null at BOTH levels** — AltDigital→partner and partner→client — and AltDigital's is tighter
- [ ] **Where HOOPs are configured: a full turndown/turnup cycle completed
      successfully, with the canary passing after turnup**

**Fail closed.** Any failure blocks go-live rather than raising a ticket for
later.

> The BAA check matters more than it looks. An unset value means the incident
> notification clock cannot run — and that would be discovered during an
> incident.

---

## Automating the external systems

### PagerDuty — near-total

Full REST API plus a mature, officially maintained Terraform provider. Services,
escalation policies, schedules, integrations and routing keys, event
orchestration rules, business services and notification rules are all
declarative.

PagerDuty configuration lives in the repository alongside the CloudFormation. A
tenant's on-call setup is a module invocation; routing keys come back as outputs
and are written to Secrets Manager.

**Treat it exactly like the AWS baseline:** versioned, staged, drift-detected.
A manual console change to a provisioned service is a finding.

### Jira — automatable in one direction

| Easy | Hard |
|---|---|
| Creating projects, issues, components, versions, boards | Configuring workflows, screens, custom fields, permission schemes and JSM request types **per project** |
| Creating the onboarding epic and task set | |
| Reading change records for reconciliation | |

**The way around the hard half is not to do it.**

Build **one golden project configuration by hand** — workflow, screens, custom
fields, permission scheme, request types — and make it a **shared scheme**.
Provisioning then creates projects that inherit it.

*Automate project creation, not project configuration.*

This is better practice regardless: a hundred tenants with independently
configured workflows is a reconciliation nightmare, because the change engine
would have to understand a hundred approval semantics. One shared scheme means
one set of field names and one definition of "approved."

Where per-tenant separation seems necessary, consider whether **components within
fewer projects** achieve the same isolation with far less configuration surface.

> Verify current API capability before building. Atlassian has been actively
> improving this area, and Cloud and Data Center differ meaningfully.

### Truveon — shape it to fit

AltDigital owns Truveon, so it should expose whatever provisioning API makes this
orchestration clean:

- Tenant creation
- Framework configuration
- Retention floor
- BAA notification window
- **Evidence-receipt confirmation endpoint** for the verification sweep

Specify this from the platform side before it is built the other way round.

### Common requirements for all three

| Requirement | Detail |
|---|---|
| **Service accounts** | Platform identity, not personal. Tokens in Secrets Manager, rotated. A pipeline running as Jamie's account breaks on a password change and muddies the audit trail. |
| **Idempotency** | Neither Jira nor PagerDuty gives it free. Check-then-create, keyed on the registry ID stamped into a label or custom field. |
| **Rate limit handling** | Both throttle. Bulk provisioning will hit it. Backoff and resume, not a failed saga. |
| **Drift detection** | Someone reconfiguring a PagerDuty escalation policy by hand must surface as a finding, not be discovered during an incident. |

### Build order

**PagerDuty first** — clean, fully declarative, and the system where manual
configuration actually hurts during an incident.

**Jira second, scoped narrowly** — golden scheme by hand once; project and issue
creation automated. Resist automating the configuration layer; effort-to-value
is poor and the shared-scheme approach is superior anyway.

---

## Deprovisioning — built at the same time

Same machine, reverse order, reading the same registry record.

Not a "later" item, for three reasons:

1. **It is the tested exit path**, which is a contractual commitment
   ([10](10-commercial-model.md))
2. **It is how failed provisioning is cleaned up.** Without it, a run that dies
   at step 6 leaves debris someone unpicks by hand, badly
3. **It has two variants needed sooner than exit:**
   - *Scope change* — a dev or test account upgraded to production-equivalent
     posture because regulated data turned up in it
     ([11](11-developer-access.md))
   - *Seasonal teardown* — a UAT environment torn down between release cycles
     and re-vested from the same registry record, with the account retained in
     `dormant` state ([14](14-environment-tiers.md))

   Same orchestration, different target states.

---

## Why this matters more than it looks

Provisioning is the **first and most frequent exercise of the entire platform.**

If vesting works, the baseline works. If the verification sweep passes, the
controls are real.

It is not administrative overhead around the design — it is the design's
regression test, running every time a tenant is onboarded.
