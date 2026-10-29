# ADR-010 — Config records in every allowed region, as Config's service-linked role, configured from one home-region stack per account

**Date:** 2026-10-25
**Deciders:** Jamie (the service-linked role, approving the 15.6a plan on 2026-10-25; ratified the whole record 2026-10-28); Claude (session), proposing one home-region stack configuring every region
**Status:** Accepted (ratified 2026-10-28)
**Tags:** config, phase-15.6, baseline, stacksets, hipaa, security-hub

---

## Context

On 2026-10-20 AWS Config recorded in 4 of 30 account-regions: the two Sandbox
accounts and the two Arc8 accounts, in us-east-2 only. The six platform and
management accounts recorded nothing in any region, and nothing recorded in
us-east-1 or us-west-2, both of which `policies/scp/06-region-restriction.json`
allows. Design 04 asks for the detective stack "in every environment, without
exception", and founder decision F-6 (`docs/phase-15-plan.md` §6.6) makes
recording continuous everywhere.

`baseline/10-config-recorder.yaml`, deployed by the `platform-config` StackSet,
had two properties that shaped the choice:

- It creates fixed-name IAM roles (`PlatformConfigRecorderRole`,
  `PlatformConfigSetupRole`). IAM is global. `PlatformConfigSetupRole` is
  exempted by exact name in SCP 01 v2's `ProtectConfigRecorder`, a policy object
  attached to every OU.
- The recorder ran as the custom role `PlatformConfigRecorderRole`. Security Hub
  control Config.1 (and CIS 2.5) requires the service-linked role
  `AWSServiceRoleForConfig`, so every account carried a CRITICAL finding the
  platform caused (B-147). `policies/scp/09-hipaa-eligible-only.json` has no
  exemptions, so a custom recorder role would also be subject to it in a HIPAA
  account.

The Config archive bucket and the Log Archive KMS key already grant
`config.amazonaws.com`, scoped by `aws:SourceOrgID`
(`security/10-log-archive.yaml`, `ConfigCheckAcl` / `ConfigWrite` and the key
policy). The HIPAA eligibility sweep (`eligibility/src/sweep.py`) already skips
every role whose path starts `/aws-service-role/`.

## Decision

1. **The recorder runs as `AWSServiceRoleForConfig`.** The setup function
   creates it if absent (idempotent), and `PlatformConfigRecorderRole` is
   removed from the template.
2. **One stack per account, in the home region, configures recorders in every
   region of `RecordRegions`.** The setup function calls Config with a
   per-region client. Global resource types are recorded only in
   `GlobalTypesRegion`. The `platform-config` StackSet keeps one instance per
   account.
3. **The function fails the stack if any region does not reach
   `recording=true`, and names which.** A partial success is not reported as
   success.

`RecordRegions` and `GlobalTypesRegion` are parameters with no default, passed
from `PLATFORM_ALLOWED_REGIONS` and `PLATFORM_HOME_REGION` in
`config/platform.env`.

## Alternatives considered

- **One StackSet instance per region (us-east-1, us-east-2, us-west-2).** The
  obvious fix, and the one in the original 15.6a plan. Rejected: the template
  creates fixed-name IAM roles, so the second region's stack fails with
  "already exists".
- **Per-region instances with region-suffixed role names
  (`PlatformConfigSetupRole-<region>`).** Avoids the collision. Rejected: SCP
  01 v2's exemption names `PlatformConfigSetupRole` exactly, so it would need a
  `PlatformConfigSetupRole*` pattern. That means editing a policy object
  attached to every OU, and B-135 just removed exemption patterns in favour of
  exact names.
- **IAM roles only in the home-region instance; other regions reference them by
  constructed ARN.** No SCP change. Rejected: it creates an ordering dependency
  between a StackSet's regions. For a new account, auto-deployment gives no
  ordering the template can rely on, so the first deploy fails in the
  non-home regions and depends on `settle-baseline.sh`'s single retry (B-062)
  to recover. That is a known failure mode built in on purpose.
- **Keep the custom recorder role and suppress Config.1 in Security Hub.**
  Rejected: it hides a finding rather than fixing its cause. It also leaves the
  recorder subject to SCP 09 in HIPAA accounts, and keeps one more `Platform*`
  role to protect and list in SCP exemptions.

## Rationale

Changing which account-regions record is a change to coverage, not to the
StackSet's shape, so it should not need more instances, more IAM or an SCP edit.
One home-region stack meets that: the setup role keeps its exact name, SCP 01 v2
is untouched, and no cross-region ordering exists to fail. Using the
service-linked role removes the platform-caused CRITICAL finding at its cause.
It also takes the recorder out of SCP scope, which keeps recording whole in
HIPAA accounts. Both serve design 04's "every environment, without exception"
and the CLAUDE.md preference for explicit over clever.

## Consequences

**Positive:**
- Config.1 / CIS 2.5 clear at the cause (B-147).
- Recording reaches every allowed region without new StackSet instances.
- SCP 01 v2 is unchanged.
- A service-linked role is outside every SCP, so SCP 09 cannot leave holes in
  recording in a HIPAA account.

**Negative:**
- A recorder stopped by hand in any region is corrected only when the stack
  next updates, not immediately. Same as before this change, now across three
  regions. `verify-security-coverage.sh` is to report it (15.6a step 5).
- Config settings stay invisible to CloudFormation drift detection, as before
  (the custom-resource choice of 2026-09-17).
- The setup function now makes cross-region calls. If a region's Config
  endpoint is degraded, the whole stack update fails, by design, rather than
  half-succeeding.
- Continuous recording in 30 account-regions costs more than in 4. F-6 accepted
  continuous recording everywhere. Cost is reported as a ratio, not figures.

**Enforcement:**
- The function refuses `GlobalTypesRegion` outside `RecordRegions`, and fails
  on any region not recording.
- `verify-security-coverage.sh` will fail on any account-region without a
  recorder (15.6a step 5, pending).
- cfn-lint and cfn-guard on the template.

## Not decided here

- Recording in the Security and Infrastructure OUs and in the management
  account. Those are 15.6a steps 3 and 4, each a STOP for Jamie. This ADR covers
  how an account records, not which accounts do.
- Security Hub central configuration and the wider B-147 triage (15.6b).
- Whether any resource types should ever be excluded from recording. All
  supported types are recorded, as before.

## Related

- B-147 (platform-caused findings, Config.1), B-062 (StackSet ordering, retry
  in `settle-baseline.sh`), B-135 (exemptions by exact name), B-161 (HIPAA
  sweep excludes service-linked roles)
- F-6 (`docs/phase-15-plan.md` §6.6), design 04
- `baseline/10-config-recorder.yaml`, `security/10-log-archive.yaml`,
  `policies/scp/01-protect-detective-controls-v2.json`,
  `policies/scp/09-hipaa-eligible-only.json`
