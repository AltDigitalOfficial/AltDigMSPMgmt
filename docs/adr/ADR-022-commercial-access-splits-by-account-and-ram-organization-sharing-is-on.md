# ADR-022 — Commercial access splits by account, and RAM sharing with the organization is on

**Date:** 2026-11-01
**Deciders:** Jamie (the split design, the management-account work run by Claude, enabling RAM organization sharing with an SCP to follow, and not deferring that SCP, 2026-11-01); Claude (session), unreviewed (the shared billing view as the Cost Explorer mechanism, the contents of each permission set)
**Status:** Accepted
**Tags:** commercial, billing, ram, identity, phase-15.9

---

## Context

D-009 assigned `CommercialReadOnly` to the management account, because cost
data existed nowhere else. 15.9 was meant to move it to the new FinOps
account and close D-009.

Three facts, found while building it:
- Invoices, payments, credits and tax are readable only in the payer account
  (D-009 already said so).
- A member account's Cost Explorer shows that account only. A billing
  delegated administrator does not change this.
- The mechanism that does is a **custom billing view** created by the payer
  and shared through AWS RAM. RAM shares billing views only within an
  organization that has RAM organization sharing enabled. This organization
  had not enabled it.

Developers in tenant accounts hold `PowerUserAccess`, which includes RAM. No
SCP restricted RAM.

## Decision

1. **Two permission sets, one group, split by account.**
   - `CommercialReadOnly` → FinOps. Cost Explorer through the shared view
     `platform-organization`, and Athena over the CUR 2.0 export, in one
     workgroup, read-only.
   - `CommercialPayerReadOnly` → management account. Payer billing only.
2. **One organization-wide billing view, shared with FinOps only**
   (`finops/05-billing-view.yaml`).
3. **RAM sharing with the organization is enabled** (2026-11-01), because
   step 2 needs it.
4. **An SCP on Members limits creating and joining RAM shares to platform
   roles**, built straight after this, not on a trigger. Jamie: the same end
   customer is discussing more applications, and the cross-account path
   exists between a client's own dev and prod accounts as much as between
   clients.

## Alternatives considered

- **Close D-009 by registering a billing delegated administrator**, as its
  close condition said. Rejected: it does not make organization-wide Cost
  Explorer readable from FinOps.
- **Athena only in FinOps, Cost Explorer left in the management account.**
  Rejected: it keeps the broad management-account grant that D-009 exists to
  remove, for the console the commercial staff actually use.
- **Don't enable RAM sharing.** Same objection: no Cost Explorer from FinOps.
- **Enable RAM sharing and defer the SCP until a second tenant**, as Claude
  first proposed. Rejected by Jamie. A dev account can share into the same
  client's prod account, so the risk exists with one client.

## Consequences

**Positive:**
- The management account keeps only payer data access, narrowed to invoices,
  payments, credits, tax and purchase orders.
- Commercial staff analyse cost in an account that sits under SCPs.

**Negative:**
- **Until the SCP lands, any principal with RAM rights can share resources
  into any other organization account, with no acceptance.** That includes
  tenant developers.
- **Cost Explorer in FinOps opens on FinOps's own costs.** Staff must choose
  `platform-organization`. The deploy script says so; nothing enforces it.
- **D-009 becomes permanent.** It no longer has a close condition that
  removes the management-account grant entirely.
- **Organizations reads in `CommercialReadOnly` do nothing from FinOps.** They
  are kept for a future delegated-administrator registration.

## Related

- D-009 (narrowed 2026-11-01), design 12
- `identity/00-commercial-readonly.yaml`,
  `identity/05-commercial-payer-readonly.yaml`,
  `finops/05-billing-view.yaml`, `scripts/deploy-commercial-access.sh`
- SCP 07, which is to carry the RAM statement
