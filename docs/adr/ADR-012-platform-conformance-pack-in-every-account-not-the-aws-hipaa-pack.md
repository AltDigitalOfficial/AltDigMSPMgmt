# ADR-012 — The platform conformance pack runs in every account, and AWS's HIPAA sample pack is not deployed

**Date:** 2026-10-26
**Deciders:** Jamie (every account, 2026-10-26); Claude (session), unreviewed (the rule list and the two-pack split)
**Status:** Accepted (deployed 2026-10-26: platform-conformance-regional in every account in all three regions, platform-conformance-account in every account in us-east-2, the management account included)
**Tags:** config, conformance-pack, hipaa, phase-15.6, security-hub

---

## Context

Phase 15.6 step 4 asked for the delta between the Config rules already
running and the HIPAA rules, kept as "the platform conformance pack".
[docs/hipaa-control-mapping.md](../hipaa-control-mapping.md) §2 and §5
measured it on 2026-10-26:

- AWS's *Operational Best Practices for HIPAA Security* maps 35 HIPAA
  citations to 129 managed Config rules.
- Security Hub, under the central policy (ADR-011, D-032), already runs 110
  of them in every account and region as service-linked rules. The platform
  runs 2 more (`platform-secret-uses-cmk`, `verify-security-coverage.sh`). 3
  are moot or decided, and 1 is a manual process check.
- 13 are run by nothing.

[vesting/derivation-rules.yaml](../../vesting/derivation-rules.yaml) mapped
`hipaa` to AWS's full sample pack, `Operational-Best-Practices-for-HIPAA-Security`.
Nothing deployed it (phase 15 plan, 15.6 finding 6). Design 04 says packs
are "driven by the tenant's frameworks". Designs 02 and 14 say detective
controls are uniform across tiers.

## Decision

1. Ten rules form the platform conformance pack
   ([security/60-platform-conformance-pack.yaml](../../security/60-platform-conformance-pack.yaml)).
   These are the 9 of the 13 adopted in the mapping §5, plus
   `cloudtrail-s3-dataevents-enabled`, which ADR-013 makes satisfiable.
2. The pack is an **organization conformance pack from Audit**, the Config
   delegated administrator, applied to **every account**. That includes
   accounts with no HIPAA scope, and accounts that join later.
3. It is **two packs**:
   - `platform-conformance-regional` is created in each allowed region;
   - `platform-conformance-account` holds only the account-level CloudTrail
     rule, and is created in the home region only.
4. `derivation-rules.yaml` maps `hipaa` to no pack and lists it under
   `covered_in_every_account`. `derive-vesting.py` then reports it as
   covered, rather than warning that nothing is applied.
5. Three of the 13 rules are not adopted:
   - `iam-no-inline-policy-check`;
   - `internet-gateway-authorized-vpc-only`;
   - `dynamodb-throughput-limit-check`.

## Alternatives considered

- **AWS's full HIPAA sample pack, per HIPAA account (the prior mapping).**
  Rejected. It runs 110 rules that Security Hub already runs in the same
  account, so every evaluation is paid twice and every failure arrives
  twice, under two names. That is the noise ADR-011 exists to remove.
- **The delta pack in HIPAA-scoped accounts only** (design 04's "driven by
  the tenant's frameworks"). Rejected by Jamie. None of the rules is
  specific to ePHI, and a tenant without HIPAA still benefits from backup,
  certificate and routing checks. It would also need per-account targeting,
  which organization packs do not offer; they only exclude.
- **One pack in every region.** Rejected. The CloudTrail rule describes the
  account, not the region, and would raise three findings for one fact.
- **A StackSet of per-account packs.** Rejected. Organization packs reach new
  accounts without a StackSet instance, and Config owns their lifecycle.
- **Adopting all 13.**
  - `iam-no-inline-policy-check` would fail every platform role, which use
    stack-scoped inline policies by design. KMS.2 covers the dangerous case.
  - `internet-gateway-authorized-vpc-only` needs a per-account VPC list that
    nothing maintains yet.
  - `dynamodb-throughput-limit-check` is capacity, not a safeguard.

## Rationale

The pack is only what Security Hub cannot see, so the HIPAA mapping stays
true without doubling cost or findings. Applying it everywhere matches
uniform detective controls across tiers (designs 02 and 14), and it removes
a vesting dependency: no tenant onboarding step has to remember to deploy a
pack.

## Consequences

**Positive:**
- The 9 HIPAA-relevant regional checks run in every account, including the
  management account if the organization pack reaches it (verified at
  deploy).
- `derive-vesting.py` stops implying that a HIPAA tenant lacks Config rules.

**Negative:**
- `platform-backup-plan-min-frequency-and-retention` fails everywhere until
  B-043's organization backup policy exists. `platform-no-unrestricted-route-to-igw`
  fails on B-047's public subnets. Both are accurate, and both add findings.
- The frequency and retention minimums (1 day, 35 days) are AWS's defaults.
  If a tier's backup policy is set below 35 days, that tier will fail the
  rule, and the parameter will need to become per-tier.
- Design 04's "packs driven by the tenant's frameworks" now holds only for
  frameworks other than HIPAA. PCI and SOC 2 still map to AWS's samples.

**Enforcement:**
- SCP 01 denies `config:DeleteConformancePack` and `DeleteConfigRule` to
  member accounts.
- `test-derive-vesting.py` pins the `hipaa` derivation.
- Keeping the pack equal to the delta is a manual discipline: re-run the
  mapping in docs/hipaa-control-mapping.md §2 when Security Hub adds or
  retires controls.

## Not decided here

- Whether PCI and SOC 2 get the same treatment (only the delta). PCI is out
  of scope (D-025). SOC 2 has no tenant yet.
- `internet-gateway-authorized-vpc-only` once the registry holds VPC lists.

## Related

ADR-011, ADR-013, D-032, design 04, docs/hipaa-control-mapping.md, B-043,
B-047, phase 15 plan 15.6.
