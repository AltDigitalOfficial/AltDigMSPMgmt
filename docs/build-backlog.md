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

## B-006 · Config archive is not immutable — CLOSED

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

**Resolved by replication** — Closed 2026-09-17. The Config archive replicates
to `altdig-config-archive-replica-868150784436` in `us-west-2`, and **that
bucket has Object Lock COMPLIANCE enabled even though its source cannot**.

**The load-bearing claim was tested before it was relied on.** The premise —
that S3 Replication can write into a locked bucket where AWS Config cannot — is
the entire design, and asserting it would have been the third assumption about
AWS behaviour to be wrong in a day. So an object was written to the unlocked
source and traced:

```
source  altdig-config-archive-868150784436          ReplicationStatus COMPLETED
replica altdig-config-archive-replica-...  COMPLIANCE  2032-09-15T20:30:51Z
```

The retention date was applied by replication, to an object whose source copy
carries no retention at all. The claim holds.

**What the guarantee now is, stated precisely** — the source Config bucket is
still mutable by a sufficiently privileged principal; that has not changed and
is not fixable, because Config will not deliver anywhere else. What changed is
that a second copy exists which **no principal can alter, including account
root**, for six years. An attacker who compromises the platform role can still
delete configuration history from `us-east-2`. They cannot delete it from
`us-west-2`, and the deletion itself is a CloudTrail event in an archive they
also cannot alter.

So the honest answer to "what does immutable evidence cover" is now
"CloudTrail and Config, the latter in the replica" — which is a sentence that
needs saying rather than a checkbox, and belongs in the auditor-facing
description of the archive.

**Depends on the backfill** — the 46 Config objects predating the rule were not
covered by it. See D-011 and
[scripts/backfill-replication.sh](../scripts/backfill-replication.sh).

---

## B-015 · Every alarm in the platform has no action

**Observed** — 2026-09-17, adding replication failure alarms and finding there
was nowhere to send them.

| Alarm | Account | Actions |
|---|---|---|
| `platform-sensitive-data-in-logs` | every member | **none** |
| `platform-replication-failed-log-archive` | Log Archive | **none** |
| `platform-replication-failed-config-archive` | Log Archive | **none** |
| `platform-replication-failed-flow-logs` | Log Archive | **none** |

Verified: `describe-alarms` returns `length(AlarmActions) == 0` for all four.

**Why this is worse than it looks** — this file's sibling
[verification-sweep.md](verification-sweep.md) opens by naming the failure mode
the platform is built against: *"configuration that looks right and does
nothing."* An alarm with no action is the cleanest possible example. It
evaluates, it transitions to ALARM, it shows red in a console nobody has open,
and it notifies no one. Design doc 13's check 2 — "a synthetic test alarm
reached PagerDuty and paged the correct rotation" — cannot pass for any alarm
in the platform.

**Why they were still worth building** — the alarm is the part that has to be
defined per resource and is easy to forget when the routing arrives. Routing is
one topic and one subscription applied to all of them. Building the alarms
first is the right order; leaving them unrouted indefinitely is not.

**What it needs** — an SNS topic per platform account (or one in Audit with
cross-account publish), a subscription, and `AlarmActions` on every alarm
resource. The destination is a phase 5 decision — PagerDuty per design doc 13,
but email to a monitored mailbox would close the "notifies no one" gap in an
afternoon and is worth doing first.

**Close before** — the first tenant carrying an incident-response commitment.
Committing to a detection-and-response SLA while no detection reaches a human
is the kind of gap that turns a control failure into a contractual one.

---

## B-013 · Detective consoles are still single-region after D-011

**Observed** — 2026-09-17, closing D-011. Replication put the *evidence* in two
regions. It did not do the same for the tools used to read it.

| Thing | Regional? | What a `us-east-2` outage costs |
|---|---|---|
| CloudTrail archive (S3) | No — replicated | Nothing; readable from `us-west-2` |
| Config archive (S3) | No — replicated | Nothing |
| Config **aggregator** (Audit acct) | **Yes** | Cannot query configuration history |
| Security Hub findings | **Yes** | Cannot see findings; no new ones arrive |
| GuardDuty findings | **Yes** | Same |

**Why this is narrower than it sounds** — the data survives, and it is the data
an auditor asks for. What is lost is the convenient query path, during exactly
the window when someone wants it. Raw CloudTrail JSON in `us-west-2` answers
"what happened" without Security Hub; it just answers it slowly.

**Why it is not simply "turn them on in us-west-2"** — the detective services
are already enabled in all three allowed regions and aggregate to `us-east-2`.
The single-region part is the AGGREGATION, and a second aggregator is not a
supported configuration for Security Hub — there is one aggregation region per
account. The realistic options are to accept it, or to move aggregation to a
region and accept the same exposure there.

**Close when** — a tenant carries a contractual availability commitment on
evidence *retrieval* rather than evidence retention, which is a meaningfully
rarer clause. Until then this is documented exposure, not a gap.

---

## B-014 · Two deviation IDs are used twice

**Observed** — 2026-09-17. `docs/deviations.md` contains two different D-009
entries (account alias prefix; `CommercialReadOnly` assignment) and two
different D-010 entries (per-application Object Lock mode; Identity Center users
created outside Entra).

**Why it matters more than tidiness** — these IDs are cited from other
documents and from template comments. "See D-010" currently resolves to two
unrelated decisions, and the reader has no way to know which. The register is
the artefact an auditor is handed to show that departures from the design were
deliberate; an ambiguous identifier undermines exactly that.

**Fix** — renumber the later duplicates and update inbound references. Small,
but it must be done in one pass across the repository rather than in the file
alone, or the references break silently.

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

## B-009 · Canonical test values are excluded from data protection detection

**Observed** — 2026-09-17, testing CloudWatch Logs data protection on the
canary.

These were **not** masked:

```
4111111111111111          the textbook test Visa
5555555555554444          the textbook test Mastercard
378282246310005           the textbook test Amex
AKIAIOSFODNN7EXAMPLE      AWS documentation example access key
wJalrXUtnFEMI/K7MDENG/…   AWS documentation example secret key
```

These **were**, immediately:

```
4532015112830366   ->  ****************
4539578763621486   ->  ****************   (and its CVV)
123-45-6789        ->  ***********
```

**AWS excludes well-known test and documentation values**, which is sensible —
it stops every CI pipeline and tutorial generating findings. But the
consequence is sharp: **verifying this control with textbook values produces a
false negative and reads exactly like a broken control.** I spent two rounds
believing the card identifier was not working.

**Recorded so nobody repeats it.** Any test of data protection — in the
verification sweep, in a tabletop, in a demonstration to an auditor — must use
Luhn-valid numbers that are not the canonical examples. `4532015112830366`
works and is in no documentation.

**Not a gap in the control.** Real cardholder data is not a test number. But it
is a gap in how the control can be *demonstrated*, and demonstrating controls
to auditors is the product.

---

## B-010 · The `locked` egress profile is not implemented

**Status** — still not selectable, but **materially less urgent**. Two new tiers
now cover most of what `locked` was being asked to do:

| Tier | single-AZ | vs locked |
|---|---|---|
| `isolated` — no NAT at all | $29 | 12x cheaper, and stricter |
| `dns-filtered` — Resolver DNS Firewall | ~$70 | 5x cheaper, ~2% of the appliance cost |
| `locked` — Network Firewall | $350 | — |

`isolated` is the one that changes the picture. Design doc 02 describes the
locked profile as being for "internal-only apps, high-sensitivity data" — and
if an application is genuinely internal-only, removing NAT entirely is both
stronger than an allow-list and cheaper than the default. There is no list to
maintain and no rule to drift.

Build `locked` only when a client's framework or contract specifically requires
L7 egress inspection, and price it at signing. See
[isolation-tiers.md](isolation-tiers.md).

**Still outstanding for the dns-filtered tier:** it uses a hand-written domain
list, which is close to decorative. AWS's managed lists —
`AWSManagedDomainsMalwareDomainList` and
`AWSManagedDomainsBotnetCommandandControl` — are free and maintained by AWS, but
are referenced by a region-specific ID that needs a lookup rather than a
literal. Wire them in before this tier is described to anyone as malware
protection.

**Why it is blocked rather than half-built.** The locked profile routes
`0.0.0.0/0` at a Network Firewall endpoint instead of NAT, so every egress
passes a domain allow-list. Without the firewall, selecting `locked` would
produce private subnets with **no default route at all**: no egress, no Session
Manager, no log delivery. A broken VPC wearing the name of a security profile
is worse than an option that is honestly absent — someone would select it,
observe that nothing reached the internet, and reasonably conclude the control
was working.

**What implementing it requires**, all in one change:

- an `AWS::NetworkFirewall::Firewall` with dedicated firewall subnets per AZ
- a firewall policy and a stateful rule group carrying the domain allow-list
- the private default route pointed at the firewall's VPC endpoint
- a return route so firewall-inspected traffic reaches the internet gateway
- re-adding `locked` to `AllowedValues` in the same commit

**Cost is the reason this is not urgent.** Network Firewall is roughly **$288
per endpoint per AZ per month** — about $576/month for a multi-AZ account,
before any data processing. A multi-AZ locked account runs near $700/month in
networking alone.

That makes the locked profile a deliberate, **priced** decision per tenant
rather than a default anyone drifts into. Design doc 02 lists it for
"internal-only apps, high-sensitivity data"; design doc 10 treats egress
profile as a vesting parameter. It belongs in the commercial conversation
before it belongs in a template.

**Build when** — a tenant's questionnaire 3.2 answer requires it and the cost
has been priced into their contract.

---

## B-011 · Public-facing ingress scaffolding not built

`public-facing` is selectable and currently behaves identically to
`controlled`: NAT egress, no ingress scaffolding. Design doc 02 expects public
accounts to terminate at CloudFront and/or ALB with AWS WAF, Shield Standard by
default.

Less dangerous than B-010 — the profile produces a working VPC, just without
the ingress path — but an account tagged `public-facing` that has no WAF is a
misleading label. The tag currently records intent, not configuration.

**Build with** the first tenant that actually serves public traffic, since ALB
and CloudFront configuration is application-shaped (certificates, origins,
health checks) and a generic scaffold would be rebuilt anyway.

---

## B-012 · VPC flow log delivery to S3 fails — UNRESOLVED

**Status** — blocking flow logs only. `EnableFlowLogs` defaults to `false` in
`baseline/40-network.yaml` so the VPC deploys without them. **The network
baseline has no flow logs.**

**Symptom** — every attempt returns
`LogDestination: <bucket> is undeliverable`, from CloudFormation and from a
direct `create-flow-logs` call alike. The message names nothing useful.

**What was ruled out**, each tested individually against a bare probe bucket
that worked, by adding the suspected difference back and confirming delivery
still succeeded:

| Suspected cause | Verdict |
|---|---|
| S3 Object Lock | not it — fails against non-locked buckets too |
| SSE-KMS with the platform key | not it — probe worked with it |
| `DenyInsecureTransport` | not it |
| Delete-deny conditioned on `aws:PrincipalArn` | not it |
| Bucket versioning | not it |
| `aws:SourceOrgID` on the bucket grants | not it |
| `aws:SourceOrgID` on the KMS grant | not it |
| The `AWSLogs/` prefix in the destination | real, and fixed — but not the cause of this |

**What is known to work** — a bucket created by `aws s3api create-bucket` with
AWS's documented two-statement flow-log policy, SSE-S3 or SSE-KMS, versioning
on, and a delete-deny. It accepted flow logs immediately and kept accepting
them as each difference was added.

**What does not work** — a CloudFormation-created bucket with the same policy
and the same settings. That is the part that makes no sense yet, and it is
where the next session should start: diff the two buckets exhaustively with
`get-bucket-*` across every sub-resource, rather than reasoning about which
difference ought to matter. Candidates not yet compared: `ObjectOwnership`,
the public access block, bucket ACL, and ownership controls.

**Cost of the gap** — flow logs are how design doc 11 distinguishes "instance
alive, telemetry silent" from an instance that is simply off. Without them that
diagnosis is unavailable, and so is most network-level incident reconstruction.
Not blocking for vesting, but it should not reach a production tenant unsolved.

**Time spent** — considerable, and the bisection was the wrong approach once
the third hypothesis failed. Diffing the working and broken artifacts directly
would have been faster than reasoning about them one property at a time.

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
