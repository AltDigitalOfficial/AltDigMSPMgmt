# ADR-017 — Card data found by Macie is a scope change in every tier; automated discovery stays on, and its cost is watched, never capped

**Date:** 2026-10-28
**Deciders:** Jamie (3% ceiling, exceeding alerts and never switches off, no absolute floor for now, 2026-10-28; cardholder data a scope change in every tier, 2026-09-25); Claude (session), unreviewed (the tripwire's design, the card-type set, the routing class, the cost watch as a script)
**Status:** Accepted (deployed 2026-10-28: router and tripwire in Audit, Macie configuration in all three regions; proven end to end with a Macie sample finding)
**Tags:** macie, pci, scope-change, cost, phase-15.6, tiers

---

## Context

Phase 15.6 step 5 asked for scope-change detection. Cardholder data is out of
scope: the platform has no cardholder data environment, and vesting halts on
any PCI answer (D-025). So its appearance is a finding. The prompt said to
follow `regulated_data_is_finding`. The phase 15 plan (15.6 finding 7) showed
that would invert the intent: that attribute is false in uat and prod, where
card data would actually land. The 2026-09-25 decisions made it a separate
attribute, true in every tier.

What was found on 2026-10-28:

- **Automated discovery was already on**, in all 10 accounts, since
  2026-09-17, with the organization auto-enabling it for members.
  `enable-security-services.sh` said it was "deliberately NOT enabled".
  Macie turns it on by default when Macie is enabled, and nothing turned it
  off. The template used AWS's default identifier set, and no bucket was
  excluded.
- **Macie is 0.2% of the organization's October bill.** Almost all of it is
  bucket inventory evaluation. Discovery's own share rounds to zero, and
  every bucket in the estate is the platform's: no tenant holds data in S3.
- **Sensitive-data findings were going nowhere.** The router has no Macie
  normaliser, and `publishClassificationFindings` to Security Hub is off.
- **The router's commercial queue has no consumer.** The working commercial
  path is the bus rule `platform-route-commercial-declared`, which matches
  `commercial_event`.
- **Cost Explorer can see every account only from the management account**,
  which holds nothing by design. 15.9's FinOps account is not built.

F-7 (2026-09-25) set the shape of the ceiling: a ratio of the account's
monthly AWS cost, held in SSM, with no figures in the repository or in
reports, and a refusal when it is absent. On 2026-10-28 Jamie set it at 3%,
said that exceeding it alerts and never switches discovery off, and declined
to set an absolute floor without the context for one.

## Decision

1. **`cardholder_data_is_scope_change: true`** on the `data_handling` axis of
   `config/tiers.yaml`, for every value. It is not in `tier_specific`, because
   it does not depend on the tier's name.
2. **The tripwire** ([security/70-scope-tripwire.yaml](../../security/70-scope-tripwire.yaml),
   Audit) takes Macie `SensitiveData:*` findings from EventBridge.
   - Card data means `CREDIT_CARD_NUMBER`, `CREDIT_CARD_NUMBER_(NO_KEYWORD)`,
     `CREDIT_CARD_MAGNETIC_STRIPE` or `CREDIT_CARD_SECURITY_CODE`. An expiry
     date or bank account number on its own does not count.
   - It emits one `Platform Incident Dossier` from `platform.scope`,
     declaring `digest_and_jira`, with `commercial_event: scope-change`. The
     result is a ticket in every tier, never a page, and an immediate
     commercial conversation.
   - Its paths:
     - A tier outside `ScopeChangeTiers` is recorded to the digest with no
       conversation.
     - An account missing from the tier map is treated as in scope.
     - Macie sample findings go to the digest only and are marked SAMPLE.
       That is how the path is tested.
   - Failures raise, and `platform-scope-tripwire-errors` reaches the router.
3. **`ScopeChangeTiers`** is computed from `tiers.yaml` by
   `scripts/deploy-scope-tripwire.sh`, never typed by hand.
4. **Automated discovery is managed explicitly.**
   `enable-security-services.sh` sets it on for all members in every region.
   It adds the card identifiers to the inspection template through
   `includes`, which extends AWS's default set rather than replacing it. No
   bucket is excluded.
5. **The ceiling** is `/platform/macie/discovery-cost-ceiling-ratio` in the
   management account, set to Jamie's ratio.
   - It measures discovery cost only: object monitoring plus sampled
     inspection. The inventory baseline is not counted.
   - `scripts/check-macie-cost.sh` compares 30 days of Macie's own estimate
     with 30 days of the account's bill.
   - It prints only WITHIN or EXCEEDS.
   - For each account that exceeds, it raises `commercial_event: cost-anomaly`
     on a digest-only dossier from `platform.cost`.
   - **It never switches discovery off.**
6. **No absolute floor.** This is a watch item while pricing is settled.

## Alternatives considered

- **Following `regulated_data_is_finding`.** Rejected (plan finding 7). It
  is silent in uat and prod, which is exactly where card data would be.
- **Restricting the inspection template to the card identifiers.**
  Rejected. The cost does not depend on which identifiers are selected, and
  narrowing would throw away detection of other sensitive data for nothing.
  `includes` adds to the defaults.
- **Excluding the platform's archive buckets from discovery** (my first
  proposal). Rejected once the cost was measured. Discovery's share of the
  bill rounds to zero, and an exclusion list is one more thing to keep right.
- **Routing card findings through Security Hub** (turning on
  `publishClassificationFindings`). Rejected. Every sensitive-data finding
  would then be routed by severity like any other finding, so expected PHI in
  prod would page. The tripwire sees only what it is for.
- **The router's commercial branch.** Rejected. Its queue has no consumer.
  The bus rule on `commercial_event` is the path that works.
- **Paging on card data in prod.** Rejected. It is a scope and compliance
  question for the client, not an outage, and the commercial channel is
  immediate in any case.
- **Switching discovery off above the ceiling** (F-7's original "STOP if it
  exceeds"). Rejected by Jamie. It would remove the control, and automation
  does not remove controls.
- **A ratio plus an absolute floor.** Recommended, and declined for now.
  Jamie has no basis yet for the amount.
- **A scheduled cost watch now.** Not possible without a home that can read
  Cost Explorer for every account. See *Not decided here*.

## Rationale

Card data anywhere changes what the platform is in scope for, whatever the
tier. The client has to hear it from AltDigital before an assessor tells them.
The emitter declares only a downgrade (D-028): a ticket in every tier and an
immediate conversation, with urgency left to the router and the commercial
channel. Discovery was already running and costs almost nothing today, so the
right move was to take ownership of it, not decide whether to turn it on.

## Consequences

**Positive:**
- Card data in any bucket, in any account, in any tier becomes a ticket and
  an immediate client conversation within one discovery cycle.
- The repository now describes Macie's actual state.

**Negative:**
- **The cost watch runs by hand.** Discovery cost grows with tenant data, and
  nothing checks it on a schedule until it is automated.
- **No floor.** A dev account with a near-zero bill will report EXCEEDS on a
  trivial amount the first time it stores many small objects. That alert is
  how the floor will be learned.
- **Automated discovery samples; it does not scan everything.** Card data in a
  bucket can go unfound for some time. This is a tripwire, not a guarantee.
- **The object key travels with the dossier**, into Jira, the digest and the
  commercial channel. Card values never do: Macie carries types and counts.
- Other sensitive-data findings (PII, PHI) are logged and dropped by the
  tripwire. Routing regulated data by tier is not built.

**Enforcement:**
- `test-scope-tripwire.py` (13 tests) runs the function's inline code
  exactly as deployed, and pins every tier to true.
- The router refuses any escalation from the emitter (D-028).
- `enable-security-services.sh` restates the discovery state on every run.

## Not decided here

- **Automating the cost watch.** Revival trigger: 15.9's FinOps account
  landing, or the first tenant bucket holding objects, whichever comes first.
- **The absolute floor.** Revival trigger: the first EXCEEDS from a dev or
  test account, or pricing being settled.
- **Routing other regulated data by tier** (`regulated_data_is_finding`),
  using the same Macie source.
- **Whether the Macie charge is part of the platform fee or passed through**
  to the client.

## Related

D-025, D-028, phase 15 plan 15.6 findings 7–9 and F-7, design 12, ADR-011,
`config/tiers.yaml`, `commercial/src/route.py`.
