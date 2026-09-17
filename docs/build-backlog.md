# Build Backlog

Technical debt and deferred work arising from the build itself. Distinct from
two other lists:

- [deviations.md](deviations.md) — accepted departures from the design package,
  each with a close condition
- [starter_docs/open-items.md](../starter_docs/open-items.md) — the design
  package's own open questions, owned by Jamie, Art and Wayne

Items here are things the build created or discovered, and that nobody has
asked for yet.

---

## B-006 · Config archive is not immutable

**Established** — 2026-09-17. **AWS Config cannot deliver to an S3 bucket with
Object Lock enabled.** Proved by elimination: `PutDeliveryChannel` failed
against the locked bucket with every condition stripped from the bucket policy,
and succeeded immediately against an identical scratch bucket whose only
difference was Object Lock. Object Lock requires `Content-MD5` on `PutObject`;
Config does not send it. No error message says any of this — the failure is
`InsufficientDeliveryPolicyException`, which points at the policy.

**Consequence** — design doc 02 wants an immutable log destination AND wants
Config delivering to it. Those are mutually exclusive, so the archive splits:

| Destination | Contents | Guarantee |
|---|---|---|
| `altdig-log-archive-<acct>` | CloudTrail | Object Lock COMPLIANCE, 2190 days |
| `altdig-config-archive-<acct>` | Config | Versioned, delete-denied, **not immutable** |

**What is actually missing** — the Config bucket denies `DeleteObject` and
`DeleteObjectVersion` to every principal except the platform deployment roles,
which is the same control the locked bucket has. The gap is the guarantee that
survives a *compromised platform role*. Object Lock holds against any principal
including account root; a bucket policy does not, because whoever can change
the policy can remove the deny.

**Partially mitigated** — Config is not the only copy. The Config aggregator in
the Audit account holds queryable configuration history independently of S3,
with its own retention.

**The route to genuine immutability** — S3 Replication from the Config bucket
into a locked destination. Replication CAN write to an Object Lock bucket where
Config cannot. Costs replication charges plus a delivery delay, and doubles
storage. Worth doing if an auditor challenges the asymmetry, or before a
tenant whose framework demands immutable configuration history.

**Decide before** — the first HIPAA or PCI tenant is vested, since that is when
someone asks what "immutable evidence" covers and the honest answer is
"CloudTrail, not Config".

---

## B-008 · Auto-enable does not cover accounts that already exist

**Observed** — 2026-09-17, enabling GuardDuty, Security Hub, Macie and
Inspector organization-wide.

After delegating administration, enabling all four services in the Audit
account, and setting `--auto-enable` / `--auto-enable-organization-members ALL`
in every region, the Audit account listed **zero members** and the canary had
**no GuardDuty detector and no Security Hub**.

Every console showed the services enabled. Nothing reported a gap.

**Two distinct traps, both now handled in `scripts/enable-security-services.sh`:**

1. **Auto-enable is prospective only.** It governs accounts that join in
   future; it does nothing for accounts that already exist. Existing accounts
   must be enrolled explicitly via `create-members` / `associate-member`.

2. **The management account must enable each service itself first.**
   `create-members` rejects it with *"your organization master must first
   enable GuardDuty to be added as a member"*. Miss this and the one account
   no SCP can constrain — the highest-value target in the Organization — is
   the only account without detection. Exactly backwards.

**Why it is worth a backlog entry rather than just a fix** — the shape recurs.
Delegation names an administrator; enablement turns a service on; enrolment
covers accounts. Three separate steps, each of which looks like completion from
the console, and the first two produce a system that appears monitored and is
not.

**Closed structurally, 2026-09-17.** Not left as a note to remember:

- `scripts/verify-security-coverage.sh` asserts
  `members(service, region) == active accounts − 1` for all four services in
  every allowed region, and exits non-zero on any gap
- `scripts/create-platform-account.sh` runs enrolment and then that assertion
  automatically after creating any account, so a new account cannot come into
  existence unmonitored without it being reported
- Recorded as check **V-A** in [verification-sweep.md](verification-sweep.md),
  which is the working version of design doc 13's fail-closed sweep and the
  specification phase 12.5 implements

Also needs running on: account moves between OUs, any region added to
`PLATFORM_ALLOWED_REGIONS`, and on a schedule — a member can be removed from a
service without the service reporting anything.

**Verified after fixing** — 3 of 3 members enrolled for all four services in
all three regions, and the canary confirmed from inside itself: GuardDuty
detector present, Security Hub `hub/default`, Macie `ENABLED`.

---

## B-001 · SCP policy 01 is close to the 5,120-byte quota

**Observed** — 2026-09-17. `01-protect-detective-controls.json` is **4,983 of
5,120 bytes**, leaving 137 bytes of headroom.

**How it got there** — the exclusion list on each statement grew from two ARNs
to six when Security and Infrastructure OU coverage required aligning the
exclusion sets with policy 03. Each statement carries the full list, so one
additional excluded principal costs roughly 220 bytes across the four
statements in this policy.

**Why it matters** — AWS rejects an over-size policy at `update-policy` time,
not at validation, so the failure lands during a deployment rather than in
review. `scripts/validate-policies.sh` does check the size and will fail first,
but only if it is run.

**Worth digging into, not urgent.** Several directions, in rough order of
appeal:

- **Shorten the exclusion list.** Six ARNs per statement is partly redundant:
  the `arn:aws:iam::*:role/...` forms were kept defensively, but the live run
  confirmed that assumed-role requests present only
  `arn:aws:sts::*:assumed-role/...`. Dropping the three `iam::role` variants
  would recover roughly half the exclusion cost with no behavioural change —
  but it removes a hedge against AWS changing the presented form
- **Condition on a principal tag instead of ARN patterns.** A single
  `aws:PrincipalTag/platform-role` condition replaces six ARNs with one
  expression. Requires the tag to be reliably set on every platform role and
  protected from tampering, which is a control in its own right
- **Split the policy.** Detective controls could divide into logging
  (CloudTrail, flow logs, log groups) and threat detection (GuardDuty, Security
  Hub, Macie, Inspector). There are two free policy slots per OU, though Phase
  10.2's bypass controls want one of them
- **Accept it and make the size check a hard gate** in the pre-commit hook, so
  the limit is hit in review rather than in a deployment

**Do not** simply keep adding exclusions and discover the limit during a
production policy update.

---

## B-002 · cfn-lint and cfn-guard — CLOSED

**Was** — neither tool installed, so the pre-commit hook printed
`cfn-lint not installed — skipping` and exited zero on every commit. Fourteen
guard rules had never executed against six templates. A hook that skips is worse
than no hook: the output looks like validation ran.

**Closed 2026-09-17.** cfn-lint 1.56.3 via `pip --user`, cfn-guard 3.2.1 from
the GitHub release.

**Results of the first run** — better than expected, but the run itself was the
point:

- **cfn-guard: 6 of 6 templates pass all 14 rules.**
- **cfn-lint: no errors, no warnings.** Six informational `I3042` findings,
  all hardcoded `arn:aws:` partitions. Fixed to `arn:${AWS::Partition}:` —
  identical output in the `aws` partition, so redeployment is a no-op, but
  AltDigital does CMMC work and GovCloud is not hypothetical.

**Negative control run, and it mattered.** A validator that passes everything is
indistinguishable from one that is not running — the same trap as the guardrail
harness. A deliberately bad template (unencrypted bucket, no rotation, 7-day KMS
window, port 22 open to the world, `Action: '*'`, no log retention, OU without
Retain) triggered **9 rules across 6 non-compliant resources**. The clean pass on
real templates is therefore real.

**Follow-on, now open as B-007.** `IAM_NO_WILDCARD_ACTION_AND_RESOURCE` checks
only `Action`, not `Resource`, despite its name. The Config setup Lambda holds
enumerated actions on `Resource: '*'` and passes. The rule is narrower than it
claims.

**Hook hardened** — missing tools now FAIL the commit rather than skip.
`SKIP_VALIDATION=1 git commit` bypasses deliberately and visibly. Policy
documents are also validated on commit via Access Analyzer, which skips only
when credentials are absent, since that one cannot be installed away.

---

## B-007 · Guard rule names overclaim what they check

**Observed** — 2026-09-17, on the first cfn-guard run.

`IAM_NO_WILDCARD_ACTION_AND_RESOURCE` tests `Action != '*'` and nothing about
`Resource`. A policy with enumerated actions on `Resource: '*'` passes — which
is most of the real over-permissioning risk, and exactly what the name promises
to catch.

Live examples that pass today and arguably should not, or should carry a
documented exception:

- `PlatformConfigSetupRole` — six enumerated `config:*` actions on `Resource: '*'`
- `PlatformConfigRecorderRole` — `AWS_ConfigRole` managed policy, AWS-authored
  and broad
- The log archive key policy's `kms:*` root delegation — not caught at all,
  because the rules only inspect `AWS::IAM::Policy`, `AWS::IAM::ManagedPolicy`
  and `AWS::IAM::Role` inline policies. **KMS key policies are outside every
  rule in the file.**

**Worth doing** — either rename the rule to match what it does, or extend it to
flag `Resource: '*'` with an allow-list of documented exceptions. The second is
better and more work. Add a key-policy rule either way; a resource policy
granting `kms:*` is worth a deliberate look even when it is correct.

---

## B-003 · StackSets delegated administration is unassigned

**Observed** — 2026-09-16, during delegated admin registration.

Design doc 02 lists CloudFormation StackSets among the services that should run
under delegated administration. It was deliberately skipped: the natural home
is Platform Tooling in the Infrastructure OU, which does not exist yet, and
delegating it to Audit would put the deployment pipeline inside the account
that audits it.

**Blocked on** — creating `altdig-infra-tooling`. Needed before Phase 2.4's
staged rollout pipeline.

---

## B-004 · Design package needs correcting in three places

The build has established facts that contradict the source documents. The
package is the reference an auditor would read, so leaving it wrong is its own
risk.

| Document | Says | Actual |
|---|---|---|
| `15-partner-model.md` | Account alias prefix `ad-` | `altdig-` — `ad-security-audit` is taken by another AWS customer (D-009) |
| `open-items.md` B15 | "`ad-` assumed" | Tested and failed |
| `entity-chain.mermaid` | Arc8 "owns app + data" | OEight owns the Arc8 application; Avergent owns theirs, built by OEight |

Also still absent and cited by the prompts: docs 01, 04, 05, 06, 08, 09, 11, 14,
the Truveon functional assumptions, and four of six diagrams. Doc 04 is the one
that bites — it holds the authoritative SCP denial list, which is why
`policies/scp/` is derived rather than transcribed.

---

## B-005 · CloudTrail is not delivered to CloudWatch Logs

**Decided** — 2026-09-17, when the organization trail was built.

The trail writes to S3 only. Design doc 07 expects incident readouts to answer
"what the principal did in the preceding 24 hours", which against S3 means
Athena rather than a log-group query — slower to write and slower to run, but
materially cheaper at organization scale.

**Revisit when** — the containment automation in Phase 7 is built and the
latency of an Athena query during a live incident can be judged against its
cost. Ingest into CloudWatch Logs is charged per GB and an organization trail
across every account is not a small volume.
