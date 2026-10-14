# ADR-003 — The digest is two emails: a per-account manager digest for the client, and a cross-tenant platform digest for AltDigital only

**Date:** 2026-10-14
**Deciders:** Jamie
**Status:** Accepted — not built. Pending: B-171 (manager digest), B-172 (platform digest).
**Tags:** digest, reporting, audience, client-facing

---

## Context

The instrumentation digest (`reporting/src/digest.py`, prompt 4.4) sends one
plain-text email per tenant account to the client's development manager, with
AltDigital CC'd. On 2026-10-10 it reported two Arc8 dev resources as "could not
instrument". The lines gave a raw ARN and a reason code, and did not say who
made the resources or why. It took a CloudTrail lookup to find out that AWS
had created them when a developer signed in to the console.

Reviewing that digest, Jamie named two different questions it should answer:

- for the development manager, a 24-hour view of what the team did;
- for Jamie, whether AltDigital missed opportunities to keep the platform
  scalable and supportable.

Design 11's *Manager digest* covers the first, and B-155 records that it was
never built. Nothing in the design package covers the second.

## Decision

- **Two emails.** The manager digest stays per account, goes to the client and
  remains the evidence record (ADR-001). A separate platform digest, one per
  day across every tenant, goes to AltDigital only.
- **Where each signal goes**, as Jamie set it:
  - ClickOps (resources created by hand, outside IaC) is itemised in the
    manager digest.
  - The missing `platform-layer` tag appears in both.
  - Spec gaps and guardrail friction across tenants are in the platform digest.
  - The full allocation is in [digest-design.md §3](../digest-design.md).
- **Mountain time** for the platform digest.
- **Design all three increments before building:**
  1. who, what, when and how on resource lines;
  2. the manager digest;
  3. the platform digest.

  Then build the manager digest.

## Alternatives considered

- **One email, with an AltDigital-only section in the CC copy.** Rejected.
  Every account would need two renders, and a single routing mistake would send
  AltDigital's internal assessment to the client. That assessment can amount to
  "the team did something the platform should have made easy or prevented".
- **Fold Jamie's view into design 06's weekly Platform health report.**
  Rejected for now. A weekly cadence would have let the Resource Explorer gap
  run for a week instead of the four days it did. Design 06's weekly report is
  still unbuilt and can absorb this digest later.
- **Ship the who/what/when/how increment first, design the rest after.**
  Offered as the recommendation and declined. The manager digest was promised to
  Arc8's developers (developer guide §4.15–4.16) and never built, so designing
  the whole shape first avoids building the exception lines twice.

## Rationale

The two readers act on different things. Only the client's team can change how
it deploys or tag its own resources. Only AltDigital can close a gap in the
alarm specification or move a guardrail. The client copy is also the evidence
that monitoring requirements were communicated (design 06), so its content has
to stay client-appropriate permanently: it is held under Object Lock for six
years.

## Consequences

**Positive:**
- The client gets the manager view design 11 specifies and the developer guide
  already promised.
- AltDigital gets one daily read across tenants instead of N per-account CCs.

**Negative:**
- Two renderers to maintain.
- The CC copy of each manager digest becomes partly redundant with the platform
  digest. B-155's move of the CC to a shared mailbox still stands, because the
  CC is how a bounce becomes visible.

**Enforcement:**
- The platform digest has its own sender path and recipient. It never adds the
  client's address.
- B-172 adds a test asserting that the platform render is never called with a
  client recipient.

## Not decided here

- Where the facts come from. That is ADR-004.
- Whether AltDigital's own human sessions in a client account are itemised in
  that client's manager digest (digest-design.md §12).

## Related

- [docs/digest-design.md](../digest-design.md)
- ADR-001, ADR-004
- B-155, B-128, B-171, B-172
- design 06 (*The dev team notification loop*), design 11 (*Manager digest*)
- developer guide §4.15–4.16
