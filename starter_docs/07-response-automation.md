# 07 — Response Automation

Two related systems: **operational response** (Tier 1 automation for alarms) and
**security incident response** (declaration to containment). They share a
dossier format and a change-record discipline, but differ fundamentally in what
automation is permitted to do.

---

# Part 1 — Operational response

## The common shape

Every runbook is the same four-stage escalation, keyed to
**(alarm type × resource type)**. Each stage re-validates against the original
alarm condition before escalating. If stage 2 resolved it, stop and report — do
not continue.

### Stage 1 — Gather (always runs, always safe)

Read-only. Runs before any action, on every alarm.

- Metrics for the window either side of the trigger
- Recent log errors and stack traces
- Current resource configuration
- **Was there a deployment or configuration change in the last 30 minutes?**
- Related resource health (dependencies, upstream/downstream)
- Whether the same alarm has fired recently, and how it resolved

That deployment question resolves a large share of real incidents on its own.

### Stage 2 — Non-destructive remediation

Idempotent, no user impact.

- Clear log files from a full volume; expand the volume
- Release stuck database connections
- Scale out
- Clear a queue backlog by adding consumers
- Flush a cache

### Stage 3 — Mildly disruptive

Permitted only where the resource is **genuinely redundant** — and the runbook
verifies redundancy before acting, not after.

- Cycle an ECS task
- Fail over an RDS replica
- Drain and replace an instance behind a load balancer
- Restart a service on a multi-instance fleet

### Stage 4 — Stop and page

With everything stages 1–3 learned attached.

---

## The output artifact

Identical structure whether the outcome is repair or escalation:

```
INCIDENT READOUT
  Alarm:            what fired, when, on what, in which account
  Context:          recent changes, deployment correlation, related alarms
  Observations:     metrics, logs, configuration at time of trigger
  Actions taken:    stage, action, result, timestamp — for each attempt
  Current state:    resolved / degraded / unresolved
  Assessment:       probable cause, confidence, supporting evidence
  Next steps:       what stage 4 would have tried, if applicable
```

- **On success** → this is the RCA and the evidence record for Truveon
- **On escalation** → this is the PagerDuty payload, so the human opens their
  laptop to a dossier rather than a blank console

---

## Safety rails

These matter more than the remediations themselves.

| Rail | Behaviour |
|---|---|
| **Circuit breaker** | Same remediation firing 3× in an hour → stop, page. Repeated automated repair is masking a real fault. |
| **Rate limit** | Cap automated actions per account per hour |
| **Change record** | Every automated action generates a change event (P7) — automation must not become a route around change management |
| **Redundancy check** | Stage 3 verifies redundancy exists before acting |
| **Blast radius** | Stage 3 acts on one resource at a time, revalidating between |
| **Kill switch** | Per-account and global disable, usable without a deployment |

---

## Implementation

**SSM Automation documents** are the natural home — they branch, call Lambda,
invoke AWS APIs directly, and log every step natively without additional
plumbing. Step Functions where orchestration is genuinely complex.

See `diagrams/alarm-response.mermaid`.

---

## Global standard and local divergence (P9)

The runbook library is **versioned and published centrally**. Each account
materialises a copy at vesting and records the version it took.

Divergence is permitted but must be **declared** — an explicit local override
with a stated reason and owner. This produces two reports that matter:

1. Which accounts are behind the current global version
2. What exceptions exist, why, and who owns each

Undeclared difference is drift and is treated as a finding.

---

# Part 2 — Security incident response

## Security incidents are not operational incidents

Different clocks, different audience, different rules for automation.

**Critically: the operational remediation tiers above must not apply to security
findings.** Terminating a compromised instance destroys the evidence needed to
understand what happened. Security automation *isolates and preserves*; it never
cleans up.

---

## Declaration

Most damage comes from taking too long to decide something *is* a security
incident. Declaration is therefore triggered without debate on defined
conditions:

- GuardDuty finding at high severity
- Credential exposure (detected in repo, logs, or by Access Analyzer)
- Unexpected data egress volume
- Unauthorised access to a data store
- Ransomware indicators
- Any `ScheduleKeyDeletion`, CloudTrail disable, or vault lock modification attempt

Plus manual declaration by any engineer.

**Bias toward over-declaration.** Declaring is cheap; standing down is fine.

Both paths create the same incident object — Jira ticket, PagerDuty incident,
Truveon record — with a system-set, timestamped `discovery_time`.

> `discovery_time` is legally significant. It is set by the system at detection
> and is never editable. Where a human declares on something detected earlier,
> both times are recorded and **the earlier governs**.

---

## Automated containment

Runs immediately, before anyone answers the page. **Sequence matters.**

### 1. Preserve — nothing destructive happens before this completes
- Snapshot EBS volumes
- Capture instance metadata and running configuration
- Copy relevant logs to the Forensics account
- Where feasible, capture memory

### 2. Isolate
- Apply the isolation security group: deny all inbound and outbound except the
  Forensics account collection path
- **The instance stays running.** Only its reachability changes.

### 3. Revoke
- Attach a deny-all policy to the compromised principal — **do not delete it**.
  Deletion loses the audit trail and breaks attribution.

### 4. Contain blast radius
- **Revoke active sessions** via a policy conditioned on token issue time.
  This is the step most often missed — disabling a principal does not invalidate
  sessions already issued.
- Check for the same indicators in other accounts. The difference between one
  compromised workload and a platform-level event.

### What automation may never do

Terminate. Delete. Deregister. Roll back. Purge.

Every action is additive or reversible. If containment would take a production
service down, it **stops and pages** with an explicit prompt for a human to
authorise the disruptive step.

---

## Confidence thresholds by environment

Isolating a healthy production service on a false positive is its own incident.

| Environment | High-confidence finding | Medium-confidence finding |
|---|---|---|
| Production | Auto-contain | Gather and page |
| Test / Dev | Auto-contain | Auto-contain |

---

## The response capability must sit outside the attacker's reach

Containment automation executes from the **platform account** via a role that
member accounts cannot modify, deny or delete (enforced by SCP, see
[04](04-security-controls-and-evidence.md)).

If an attacker holds admin in the member account, the response capability must
still function. **This assumption is load-bearing and must be tested** — verify
periodically that a member account with full local admin cannot block the
containment role.

---

## Forensic readiness — decided now, not during an incident

- A dedicated **Forensics account**, isolated, with restricted access
- An **isolation security group** pre-staged in every VPC, ready to apply
- Ability to snapshot volumes and capture memory before anything is touched
- `SecurityResponder` role, JIT-elevated

Retrofitting this during an incident is not possible.

---

## The readout

Same dossier shape as operational runbooks, security-flavoured:

- What fired, when, and `discovery_time`
- What was preserved and where it is
- What was isolated, and what remains reachable
- **What the principal did in the preceding 24 hours** (CloudTrail)
- **What else that principal can reach** (Access Analyzer, policy evaluation)
- **Whether the same indicators appear in other accounts**
- Current containment state and what was deliberately *not* done

---

## The notification clock

Truveon starts counting at `discovery_time` against the tenant's contractual
notification window and surfaces a **live countdown** on the incident, escalating
at defined fractions of the window.

Nobody should be doing date arithmetic during a live incident at 3am.

### Clock reference

| Framework | Obligation | Notes |
|---|---|---|
| **HIPAA (as subcontractor Business Associate)** | Notify the **partner** (not the covered entity) without unreasonable delay | **AltDigital's BAA window must be materially tighter than the partner's downstream window, or the chain cannot complete. See [15](15-partner-model.md).** |
| **PCI-DSS** | Immediate notification to acquirer and card brands | Measured in hours |
| **SOC 2** | No statutory clock | Our own policy becomes the standard we are held to |
| **State breach laws** | Vary, often tighter than HIPAA | Apply per data subject location |

> **Placeholder — owned by Art and Wayne.** Notification windows are fields in the
> onboarding record at **two levels** — per partner and per client. Both run
> simultaneously from the same `discovery_time`, and an unset window at either
> level blocks go-live. Design treats them as variables; the content and
> negotiation are theirs.

**The clocks run in series:**

```
discovery_time
  ├── AltDigital → Partner   (tightest — must leave the partner time)
  │        └── Partner → Client
  │                 └── Client → individuals / HHS  (≤60 days statutory)
```

The incident view must show the **chain**, not just the next deadline.
AltDigital notifies the partner; **the partner notifies the client.**

Also requiring a defined contact and window: **cyber insurance notification**
(coverage is frequently conditional on notice within a defined period) and
**external counsel**.

---

## Exercising containment automation

Containment has the worst testing problem in the design. The restore path is
exercised monthly by design. Containment automation may sit unused for a year
and then run once, badly, on the night it matters.

**Same treatment as restore (P4):** a scheduled exercise fires the *real*
automation against a purpose-built target in an isolated account.

1. Plant a finding
2. Let it declare
3. Let it preserve and isolate
4. Verify snapshots landed in Forensics and isolation actually isolated
5. Measure elapsed time
6. Tear down

Same code path as production, different target.

This also produces **tabletop evidence with real timings** rather than a narrated
walkthrough — a considerably stronger audit artifact. Annual tabletop exercises
remain, with evidence retained.

See `diagrams/incident-containment.mermaid`.
