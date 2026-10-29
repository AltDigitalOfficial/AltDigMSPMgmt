# ADR-011 — Security Hub is configured centrally from Audit; a control is disabled only when no account can satisfy it, and platform-only noise is suppressed per resource

**Date:** 2026-10-26
**Deciders:** Jamie (central configuration as phase 15.6b; standards option A, 2026-10-26; ratified the whole record 2026-10-28); Claude (session), proposing the disable-versus-suppress rule and the specific lists
**Status:** Accepted (ratified 2026-10-28)
**Tags:** security-hub, phase-15.6, central-configuration, findings, b-147

---

## Context

On 2026-10-26 Security Hub in this organization was configured locally
(`describe-organization-configuration`: `ConfigurationType LOCAL`,
`AutoEnable true`, `AutoEnableStandards DEFAULT`). There was no finding
aggregator. `scripts/enable-security-services.sh` enabled default standards in
Audit and the management account, so every account ran FSBP and CIS 1.2.0.

A read of every active failed control across all three regions, from Audit,
grouped by control and by whether the failing resource was the platform's,
showed three kinds of finding (B-147, B-174):

- **Never satisfiable here, in any account, by design.** CloudTrail.5 (the
  organization trail does not deliver to per-account CloudWatch Logs),
  EC2.172 (VPC Block Public Access deliberately not enforced, 15.4), and the
  VPC-endpoint controls EC2.10/.55–.60 (endpoints are an egress-profile cost
  decision; every member VPC is the platform's).
- **Right in general, wrong only for the platform's own resources.**
  CloudFormation.3/.4 on `StackSet-platform-*` and `platform-*` stacks, 65 of
  65 failures. IAM.6 on the management root, an accepted deviation (D-006).
- **Real.** Tenant security groups open to the internet on high-risk ports in
  Arc8 dev, Audit's own GuardDuty and Inspector coverage, account settings
  never applied outside Sandbox (B-139), DynamoDB deletion protection on the
  registry, and others. These are recorded as work in B-174.

Jamie chose the standards on 2026-10-26: FSBP and NIST 800-53 r5, not CIS and
not PCI (D-032).

## Decision

1. Security Hub runs under **central configuration** from Audit, in three
   ordered steps (`scripts/enable-securityhub-central.sh`):
   - a finding aggregator in us-east-2 linking us-east-1 and us-west-2
     (`security/35-securityhub-aggregator.yaml`);
   - the organization configuration switched to `CENTRAL` by an idempotent API
     call;
   - one configuration policy (`platform-security-hub`) associated at the
     Organization root (`security/36-securityhub-policy.yaml`).
2. **A control is disabled in the policy only when a platform decision makes
   it unsatisfiable in every account, tenants included.** The list is
   CloudTrail.5, EC2.172, EC2.10, EC2.55, EC2.56, EC2.57, EC2.58 and EC2.60.
   Each is reasoned in the template.
3. **A finding is suppressed, by an automation rule matching control AND
   resource or account, when the control is right in general and wrong only
   for the platform's own resources.** CloudFormation.3/.4 on platform stacks,
   and IAM.6 on the management account
   (`security/30-securityhub-suppressions.yaml`). A tenant's resource still
   raises the finding.
4. **Real findings are neither disabled nor suppressed.** They are work.

## Alternatives considered

- **Stay local and fix standards per account.** Rejected: a member
  administrator can change local standards and controls, and nothing would
  restore them. A newly vested account would get whatever auto-enable's
  DEFAULT is at the time, which is AWS's choice, not ours.
- **Disable every noisy control in the policy.** Simpler, one list. Rejected:
  disabling removes the control for tenants too. CloudFormation.3/.4 would
  then never flag a tenant's own hand-deployed stack, and IAM.6 would never
  flag a member root. The cost of being blunt lands on the tenant.
- **Suppress everything per resource, disable nothing.** Rejected for the
  by-design controls. Every new account, VPC and region would raise them
  again, and a suppression rule would then be the only thing standing between
  a tenant's view and a page of findings that can never be fixed. That is
  noise managed rather than removed.
- **Associate the policy per OU rather than at the root.** Rejected for now.
  One policy has no per-OU difference to express yet. A per-OU policy is the
  route if a tenant's frameworks ever need different standards; the root
  association is then narrowed.

## Rationale

The tenant's security view should show the tenant's findings, and every
finding it shows should be fixable by someone (B-147). Disabling is reserved
for what no one can ever fix. Suppression is reserved for what only the
platform causes, and keeps tenant coverage whole. Real findings stay visible.
Central configuration makes that one policy hold for every account, including
ones vested later, which is the property design 04's "every environment,
without exception" needs.

## Consequences

**Positive:**
- One source of truth for standards and controls. A member cannot drift from
  it.
- New accounts inherit it at vesting with no extra step.
- CIS 1.2's 14 metric-filter controls and its password-policy rows retire with
  the standard.
- A single cross-region view in Audit.

**Negative:**
- The switch to `CENTRAL` is an API call, not a CloudFormation resource.
  `AWS::SecurityHub::OrganizationConfiguration` was tried on 2026-10-26 and
  failed with "Organization Configuration resource already exists": the
  organization configuration is a singleton that always exists, so it cannot
  be created. The stack rolled back cleanly and the configuration stayed
  `LOCAL`. Drift detection cannot see the switch, so
  `verify-security-coverage.sh` checks it.
- The disabled list is organization-wide. If a tenant ever needs one of those
  controls (one that runs its own VPCs, say, which SCP 07 forbids today), the
  root association has to give way to per-OU policies.
- `enable-security-services.sh` still calls `enable-security-hub
  --enable-default-standards` and sets auto-enable. Under CENTRAL those calls
  conflict with or are overridden by the policy. The script must be changed
  in the same unit that deploys this (B-174).
- Switching to CENTRAL is an organization-wide change to every account's
  standards at once. Findings from CIS 1.2 are archived.
- Automation-rule tags must be applied by API (`security/30-...` header).
- **Unverified:** whether a root-associated policy covers the management
  account itself as well as the members. Check after deploy, and associate it
  explicitly if not.

**Enforcement:**
- The configuration policy itself: members cannot override it.
- Each suppression's criteria name control AND resource or account.
- The disabled list lives in a template, so a change is a reviewed commit.

## Not decided here

- Conformance packs (HIPAA operational best practices via the NIST mapping,
  design 04): 15.6 step 4, separate.
- Macie automated discovery for the cardholder tripwire (15.6c, gated on F-7).
- Whether the default VPCs, the source of much EC2.15/EC2.6 noise, should be
  removed from accounts (B-174).

## Related

- D-032 (standards versus design 04), D-006 (management root MFA)
- B-147, B-174, B-139, B-012
- ADR-010 (Config recording, which Security Hub's controls evaluate)
- `security/36-securityhub-policy.yaml`,
  `security/30-securityhub-suppressions.yaml`,
  `scripts/enable-security-services.sh`, design 04
