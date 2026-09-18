# 10 — Commercial Model

## Support pricing

**Flat fee per account per month + a percentage of AWS spend.**

| Component | Purpose |
|---|---|
| **Flat fee** (indicative: ~$1,000/account/month) | Covers fixed overhead — baseline, monitoring, on-call presence whether or not anything happens |
| **Percentage uplift** (indicative: 15–20%) | Scales with the volume of infrastructure actually managed |

The floor prevents small accounts from consuming support they do not fund. The
percentage prevents large accounts from underpaying for the effort they generate.

### Percentage basis: gross (list price)

The percentage applies to **list-price AWS spend**, not net of any Savings Plans
or Reserved Instance discounts AltDigital negotiates. AltDigital carries the
commitment risk, so AltDigital retains the arbitrage.

**This must be explicit in the contract.** Nobody can object to a term they
signed; a customer discovering the delta later, unstated, becomes a dispute.

Contract language must avoid anything that reads as cost-plus, because that is
very hard to walk back once optimisation begins.

**Interim position:** no negotiated discounts exist yet, so list price is the
actual price. Showing the raw AWS cost alongside the uplift is good practice —
it makes the margin transparent and gives the customer a reason to care about
right-sizing, which benefits both parties.

**Revisit** once volume makes commitments worth negotiating, with real usage
data to base terms on.

### Pricing varies by service model

Model A is materially less work than Model B. The flat fee and percentage should
differ accordingly. Worth pricing before the first contract rather than after.

---

## Customer-facing cost levers

The customer can influence their own bill through four decisions, which should
be presented at onboarding and revisited whenever cost becomes a topic:

| Lever | Effect | Tradeoff |
|---|---|---|
| Decline UAT | Removes an environment entirely | No formal acceptance stage |
| HOOPs on dev/test | Substantial compute saving | Unavailable outside declared hours |
| HOOPs on UAT | Modest compute saving | Risk of unavailability during user testing |
| Seasonal teardown of UAT | Near-total saving during dormancy | Re-vest lead time per cycle |

**Present HOOP savings as compute only.** Storage, backups and network baseline
continue to bill. Overstating the saving creates a bad conversation at the first
invoice. See [14 — Environment Tiers](14-environment-tiers.md).

---

## Truveon

Not an allocation problem. Truveon is a product with its own account, its own
revenue line, and a **flat per-member monthly fee** that funds its own AWS costs.
It is effectively an internal business unit.

---

## Shared platform costs

Log Archive, Audit, Forensics, Platform Tooling and Shared Services accounts
generate real cost that nobody pays a flat fee for. Three options:

1. Absorb into MSP margin, never allocate
2. Split evenly per member account
3. Allocate by usage (log volume, findings processed)

**Position: absorbed into the percentage uplift.** A percentage roughly tracks
the real cost — a larger workload generates more logs and more findings — without
requiring measurement. It is also a far simpler conversation than a line-item
breakdown nobody understands.

The weakness is that it decouples revenue from effort: a large, quiet workload
pays a lot for little management; a small, messy one pays little while consuming
the team. The flat-fee floor is the mitigation.

---

## Cost visibility and allocation

Billing rolls up **twice**: by client, for the partner's invoicing to their
clients; and by partner, for AltDigital's invoicing to the partner. Cost
Categories derive both from the account ID. See
[15 — Partner Model](15-partner-model.md).

The account boundary gives per-application spend for free. Beyond that:

- **Cost Categories** map account IDs to tenant, application, environment,
  service model and compliance scope — **without requiring a single resource tag**
- Cost Explorer and CUR provide per-account, per-environment breakdown natively
- Budgets and anomaly detection per account, with alerts routed by tenant
- Monthly tenant service report includes raw AWS cost alongside the uplift

---

## Tagging standard

AWS permits 50 tags per resource. **Comprehensive tagging is a trap** — tags are
applied by whoever creates the resource, and dev teams will not maintain twenty
of them. They will type garbage to satisfy the check.

Split by who owns each tag:

### 1. Derived from the account — not tagged at all

Tenant, environment, service model, compliance scope, retention tier, egress
profile, region.

These are properties of the *account*. They live in **Cost Categories** and the
platform account registry, derived automatically. **This is what keeps the
enforced set small enough to hold.**

### 2. Enforced on creation — maximum four

| Tag | Values |
|---|---|
| `Owner` | Team or individual responsible |
| `Application` | Application short code |
| `DataClassification` | public / internal / confidential / regulated |
| `CostCenter` | *optional, where the tenant requires internal chargeback* |

Enforced by SCP denying resource creation without them. **Beyond four, quality
collapses.**

### 3. Platform-managed — applied by automation, never by humans

`platform-managed`, `AlarmTier`, `BackupPolicy`, `PatchGroup`,
`BaselineVersion`, `InstrumentedAt`.

Set by the instrumentation and baseline layers. Modification by member account
principals is denied by SCP.

### 4. Free-form — unenforced

Reserved prefix (e.g. `app:*`) so tenant tags never collide with platform tags.

---

## Service level agreement

### Tiers

| Level | Monthly downtime | Position |
|---|---|---|
| **Commitment** | 99.95% | 21.9 min |
| **Penalty threshold** | ≤99.0% | 7.3 hrs — service credits apply |
| **Bonus threshold** | ≥99.995% | 2.2 min — bonus applies |

> **Note on the bonus tier.** 99.995% is not reliably achievable on a
> single-region multi-AZ baseline with a 4-hour RTO — a single AZ event or an
> RDS failover can consume the entire monthly budget. If the bonus tier is a
> real commercial target, it implies a **premium multi-region architecture at a
> different price**, not the standard build. This needs a decision.

### Measurement

- **Successful synthetic transactions ÷ total attempts**, per service, per
  calendar month
- A check passes on HTTP success **and** completion within a stated timeout —
  preserving end-to-end, performance-independent measurement while excluding the
  technically-up-but-broken case
- **The authoritative measurement source is external**, named in the contract.
  CloudWatch Synthetics canaries serve internal operations. This removes the
  marking-your-own-homework argument permanently and costs nothing.

### Back-to-back with the partner

AltDigital's commitment to a partner must be **at least as strong** as the
partner's commitment to their client, or the partner absorbs the gap. The
exclusion evidence must be presentable to the *client*, not only to the partner.
See [15 — Partner Model](15-partner-model.md).

### Scope by service model

| Model | Commitment |
|---|---|
| **C** | End-to-end, total — AltDigital owns the whole stack |
| **B** | End-to-end, subject to customer-caused exclusion |
| **A** | End-to-end, subject to customer-caused exclusion (broader scope of exclusion) |

Same number, different attribution. This is how a 99.95% commitment can be held
on an application AltDigital did not write.

### Exclusions — enumerated, not general

1. Scheduled maintenance within a published window
2. **Customer-caused unavailability** — their deployment, their code, their
   configuration change
3. Force majeure and AWS regional events beyond multi-AZ mitigation
4. Customer-side network or DNS outside the AltDigital boundary

**Exclusion 2 must be evidence-backed, not asserted.** The change reconciliation
system ([08](08-change-and-release.md)) proves which deployment preceded which
outage. This is where that mechanism pays off commercially as well as at audit.

Maintenance windows must be excluded explicitly, or AltDigital's own patching
consumes the error budget.

---

## Exit

### The boundary

> **The customer owns their application and their data. The platform IP is owned
> by Foundry and licensed to AltDigital, which operates it.**

The full chain — Foundry → AltDigital → partner → client — is in
[15 — Partner Model](15-partner-model.md). The exit clause must say the platform
IP is **licensed**, not owned by the operating entity, or it contracts away
rights whose ownership chain is undocumented.

The customer leaves with the application and the data beneath it. They do not
leave with the intellectual property AltDigital built to manage AWS
infrastructure efficiently with minimal staffing — the baseline, the runbook
library, the instrumentation layer, the vesting pipeline.

### What exit actually involves

The account itself is portable: AWS can transfer an account out of the
Organization. That is a support ticket and a few hours, not a migration.

**What does not transfer** is everything wrapped around it. On leaving the
Organization the account loses:

- SCPs and guardrails
- Centralised log destinations
- Backup vaults held in AltDigital's backup account
- Identity Center federation
- Platform automation roles and the runbook library

It becomes an account with a workload and no governance. **This must be stated
plainly in the contract.** It is not obstruction — it is the natural consequence
of the boundary — but if it is a surprise on the day, it becomes a dispute.

### Designing for exit

1. **Data extraction path is real and tested** — application data, their logs
   from the archive, their Truveon tenant
2. **The boundary is architectural, not merely contractual** — nothing of
   AltDigital's is entangled inside the member account requiring surgical
   removal
3. **A documented exit runbook with a timeline** — a process, not a negotiation

The exit path is the **deprovisioning half of the provisioning orchestration**,
built at the same time and exercised by every failed or abandoned onboarding.
See [13 — Client Provisioning](13-client-provisioning.md).

### Truveon on exit

Because Truveon is multi-tenant by design, a departing customer may **retain
their tenant by paying for it separately**. That is a billing change, not an
engineering project.

Their evidence history stays intact, which is genuinely valuable to them at
their next audit — and makes the exit conversation considerably less
adversarial.
