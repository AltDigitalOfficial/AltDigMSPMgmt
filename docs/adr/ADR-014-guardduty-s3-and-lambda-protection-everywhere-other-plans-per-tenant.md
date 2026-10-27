# ADR-014 — GuardDuty S3 and Lambda Protection run in every account; the other protection plans are per-tenant

**Date:** 2026-10-26
**Deciders:** Jamie (accepted the recommendation, 2026-10-26)
**Status:** Accepted (applied 2026-10-26 in all three regions; verified on the organization configuration, Audit, Arc8 dev and the management account)
**Tags:** guardduty, security-hub, phase-15.6, b-174, cost

---

## Context

After Security Hub moved to central configuration (ADR-011), Audit reported
GuardDuty.5–.13 and Inspector.3/.4 as failed (B-174). B-174 first read this
as Audit missing what members get through auto-enable. That was wrong.
[scripts/enable-security-services.sh](../../scripts/enable-security-services.sh)
deliberately enabled the GuardDuty detector only, and listed "Malware
Protection, EKS and RDS plans" and "Inspector Lambda scanning" under
*Deliberately NOT enabled*, as per-tenant cost decisions.

The platform is built of Lambda functions. Its evidence, logs and every
tenant's data live in S3.

## Decision

1. **S3 Protection** (`S3_DATA_EVENTS`) and **Lambda Protection**
   (`LAMBDA_NETWORK_LOGS`) are enabled for every account. Organization
   auto-enable is set to `ALL`, which covers existing and new members, and
   Audit's own detector is set explicitly.
2. The other plans stay off at platform level, as per-tenant decisions where
   the workload exists:
   - EKS audit logs;
   - EKS, ECS and EC2 runtime monitoring;
   - Malware Protection for EC2;
   - RDS Protection.

   So does Inspector Lambda scanning.

## Alternatives considered

- **Every plan, everywhere.** Rejected. Plans for EKS, ECS, RDS and EC2
  runtime cost money in proportion to workloads most accounts do not run.
  Where a workload does exist, the tenant's decision is the right place for
  it.
- **Nothing more, and suppress all the findings.** Rejected. S3 and Lambda
  are the platform's own attack surface, and the controls were reporting a
  real absence on the services it depends on.
- **Audit only.** Rejected. The members had the same absence. Security Hub
  surfaced it on Audit because the organization configuration is evaluated
  there.

## Rationale

These two plans protect the services the platform itself is made of, in
every tier, consistent with uniform detective controls (designs 02 and 14).
The rest follow the workload, which is the tenant's.

## Consequences

**Positive:** GuardDuty.6 and GuardDuty.10 pass. Anomalous S3 access and
Lambda network activity raise GuardDuty findings, which reach the router.

**Negative:**
- GuardDuty cost rises in every account. S3 Protection scales with S3 data
  event volume, which ADR-013 makes organization-wide, so the two decisions
  compound.
- GuardDuty.5, .7, .8, .9, .11, .12 and .13, and Inspector.3/.4, keep
  failing until each is either enabled per tenant or suppressed where no such
  workload exists. That suppression is not built.

**Enforcement:**
- `enable-security-services.sh` sets both on every run.
- Auto-enable `ALL` applies them to accounts that join later.
- SCP 01 denies `guardduty:UpdateDetector` to member accounts, outside its
  platform exemptions. That is the call a member would use to turn a
  feature off.
- Nothing alarms if the administrator changes the organization
  configuration.

## Not decided here

- How the per-tenant plans are chosen at vesting (a questionnaire question,
  or derived from declared services).
- Suppressing the remaining plan controls in accounts without those
  workloads.

## Related

ADR-011, ADR-013, B-174, B-008, design 02, design 14.
