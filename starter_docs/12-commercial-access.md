# 12 — Commercial Access and Client-Facing Events

Covers the commercial and billing function — Tina and her team — and the event
class that drives client conversations.

**Scope:** cost reporting across the Organization by partner and by client, plus
notification of events that warrant a client conversation. **No operational
responsibilities of any kind beyond that.**

Two audiences use the same permission set at different scopes:

| Audience | Scope |
|---|---|
| **AltDigital commercial** (Tina) | All partners, all clients — full portfolio |
| **Partner business users** (e.g. OEight CEO, sales) | Their own partner OU only — their book, not the portfolio |

Partner business users receive **no AWS access of any kind** beyond this. See
[15 — Partner Model](15-partner-model.md).

---

## Where the access sits

Cost and billing data is Organization-level, which creates tension with keeping
the management account nearly empty and untouched
([02](02-platform-architecture.md)).

**Do not resolve that by granting access to the management account.** Instead:

- **Cost and Usage Report** exported to S3 in a dedicated reporting account
- **Delegated Cost Explorer access** via the billing delegated administrator
- The commercial team consumes from the aggregation layer, never from individual
  member accounts

This works cleanly because **Cost Categories already map account → tenant,
application, environment, service model** ([10](10-commercial-model.md)). The
by-customer view exists by construction rather than needing to be built.

---

## Permission set: `CommercialReadOnly`

Assigned via an Entra group, same federation path as every other role
([03](03-identity-and-access.md)).

**Granted — read only:**

- Cost Explorer, Budgets, Cost and Usage Report, Cost Categories
- Billing console, invoices, Savings Plans and Reserved Instance reporting
- The reporting account's S3 export and its query layer

**Denied:**

- **Member account access of any kind** — not read-only, not via the aggregation
  account, not at all
- CloudWatch, logs, Config, Security Hub, GuardDuty
- Any write action anywhere

> This is **billing only**, not "read-only everywhere plus billing". Cost data
> reveals that an account exists and roughly what shape it is; it reveals nothing
> about its contents. That is the correct exposure for a commercial function.

---

## The client-conversation event class

A **fourth destination** in the routing design of [06](06-observability.md),
alongside PagerDuty, Jira and the review queue.

Defined by **who needs to have a conversation**, not by severity.

### Events routed here

| Event | Timing |
|---|---|
| Outage affecting SLA; SLA credit triggered | **Immediate** |
| Security incident in progress — *awareness only, see below* | **Immediate** |
| Cost anomaly or budget breach on a tenant account | Immediate |
| Vesting complete / onboarding milestone reached | Batched |
| CVE or finding SLA breach where the customer owns remediation | Batched |
| Service model change | Batched |
| Scope change — regulated data found in a dev account | Immediate |
| Exit initiated | Immediate |
| Maintenance window requiring customer coordination | Batched |

Batched by default, same discipline as the other digests. Outage and SLA-credit
events go immediately, because those are the ones where a customer calls before
the commercial team has heard.

Implemented as a **PagerDuty business service** with notification rules rather
than paging rules.

---

## Informed is not notifier

**This distinction must be preserved in the routing.**

Security incident notification to a customer is legally governed — the BAA clock,
and the contract terms owned by Art and Wayne
([07](07-response-automation.md)).

- The commercial team must **know** an incident is in progress, so they are not
  blindsided on a call
- The commercial team must **not be the notifying party**
- The notification record that matters is the one Truveon holds against the clock

Keep *informed* and *notifier* clearly separated in the event routing, or the
distinction erodes the first time someone is being helpful under pressure.

---

## Reporting

| Report | Cadence | Contents |
|---|---|---|
| Per-client cost | Monthly | Raw AWS cost, flat fee, percentage uplift — shown separately. Delivered to the partner for their own invoicing. |
| Per-partner roll-up | Monthly | AltDigital's invoicing basis for the partner |
| Portfolio view | Monthly | All partners and clients, by service model, margin analysis. **AltDigital only.** |
| Budget and anomaly | Event-driven | Per account, routed to the commercial event class |
| Truveon flat-fee reconciliation | Monthly | See gap below |

---

## Open gap — Truveon billing visibility

Reconciling the Truveon flat fee requires per-tenant visibility **inside
Truveon**, which is a Truveon tenant-administration question rather than an AWS
one.

Raise in the Truveon project: the commercial team needs a per-tenant billing or
usage view, and it should be readable without granting access to tenant evidence
content.
