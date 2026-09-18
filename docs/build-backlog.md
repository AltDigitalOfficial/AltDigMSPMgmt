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

## B-015 · Every alarm in the platform has no action — CLOSED

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

**Progress, 2026-09-17** — the AWS half is built and proved; the PagerDuty
half is waiting on a credential.

| Step | State |
|---|---|
| SNS topic in Audit, org-scoped publish | **Done** — `alerting/10-alert-topic.yaml` |
| Cross-account alarm can actually publish | **Verified** — `scripts/test-alert-path.sh`, 0 → 1 |
| `AlarmActions` on the Log Archive alarms | **Done** |
| `AlarmActions` on the member-baseline alarm | **Done** — StackSet updated, canary verified |
| PagerDuty service, schedule, escalation policy | **Written** — `pagerduty/`, not applied |
| Routing key in Secrets Manager, subscription | Blocked on `PAGERDUTY_TOKEN` |

**One topic, not one per account.** The obvious design puts a topic in every
account, which copies the PagerDuty routing key into every account in the
Organization — including member accounts, which are the ones a tenant
application can compromise. Alarms publish cross-account instead, so the
credential exists once and rotates in one place.

**Status** — Closed 2026-09-17. A synthetic alarm reached Jamie's phone.

That sentence is the whole close condition, and nothing weaker would have done.
Every intermediate signal was green well before it was true:

| Signal | Said | Actually proved |
|---|---|---|
| `set-alarm-state` returns 0 | alarm fired | nothing — succeeds even if the publish is refused |
| `NumberOfMessagesPublished` 0 → 1 | topic accepted it | the SNS and KMS policies admit the publisher |
| Subscription `Confirmed` | endpoint is good | **nothing** — a wrong routing key confirms identically |
| `NumberOfNotificationsDelivered` 1 | PagerDuty returned 200 | close, but a wrong key also returns 200 |
| **A phone buzzed** | — | the chain works |

The fourth row is the trap. PagerDuty answers `200` to the subscription
confirmation regardless of whether the routing key is valid, and then discards
everything. Every AWS-side indicator is indistinguishable between "working" and
"silently dropping every page", which is why the close condition was always the
phone and never the console.

This is design doc 13's check 2 — *"a synthetic test alarm actually reached
PagerDuty and paged the correct rotation"* — passing for the first time.

**Still true, and now the narrower problem** — the alert-delivery-failure alarm
routes through the path it monitors. See B-021.

---

## B-021 · The alert-delivery alarm pages through the path it monitors

**Observed** — 2026-09-17, on closing B-015.

`platform-alert-delivery-failed` watches `NumberOfNotificationsFailed` on the
alerting topic. Its only action is to publish to that same topic, which
forwards to PagerDuty. So the one condition it exists to detect — delivery to
PagerDuty is broken — is precisely the condition under which it cannot tell
anyone.

Not a design oversight so much as an unavoidable shape: any monitor of a
notification channel that uses that channel has this property. It only stops
being circular with a **second, independent** path.

**What it needs** — one channel that shares nothing with the first. Email to a
monitored mailbox via a separate SNS subscription is the cheap version and
mostly sufficient, since the failure being detected is PagerDuty-specific
rather than SNS-wide. A genuinely independent path — something outside AWS
polling a heartbeat — also covers "the topic was deleted" and "the alarm was
disabled", which the email does not.

**Narrow but real.** Every other alarm now pages correctly; this is the one
that cannot. Worth closing alongside B-017, which wants an external heartbeat
for overlapping reasons.

---

## B-022 · The registry records intent; nothing compares it to reality

**Observed** — 2026-09-17, building the registry (phase 3.2).

Prompt 3.2 says *"undeclared difference between an account's actual
configuration and its recorded parameters is drift and must surface as a
finding."* The registry now holds the recorded parameters and reports declared
overrides. **Nothing compares the two sides.**

So `report-overrides` returning "no declared overrides" means precisely that —
nobody has declared one. It does not mean there are no differences, and the
report says so in as many words rather than implying a clean bill of health.

**Why it is genuinely the harder half.** Comparing requires a per-parameter
notion of what "actual" means, and each one is a different API:

| Recorded parameter | Where reality lives |
|---|---|
| `EgressProfile` | route tables, NAT presence, endpoint set |
| `baseline_version` | which StackSet version the account's instance is at |
| `environment` | OU placement, tags |
| retention, Object Lock mode | S3 bucket configuration |

`CloudFormation DescribeStackInstance` gives the baseline half almost for free
— the drift status and the deployed version are both there — and that covers
report 1's underlying question honestly rather than trusting a number someone
typed. The rest needs a comparator per parameter.

**Do the baseline half first.** It is the one where the registry can currently
be wrong without anyone noticing: `put-account --baseline-version` records what
someone *says* the account is at, and nothing checks it against the StackSet.
A registry that confidently reports a wrong version is worse than no report.

**Close before** — the first tenant, since drift detection is a claim
AltDigital makes to clients and this is the part that makes it true rather than
declarative.

---

## B-023 · Truveon tenant granularity: per client, or per application?

**Raised** — 2026-09-17, by Jamie: *"we do have a Truveon tenant set up for
OEight (the company) but not yet Arc8 (the app)."*

**The questionnaire says client.** Field 1.1 — "Client (member organisation)
name" — lists "Truveon tenant" among its consumers. Field 1.2, the application
name, does not. So the design as written creates one tenant per client, and
OEight's existing tenant would already cover Arc8.

**The operational reality described is per application.** Which is a
defensible answer, and possibly the better one, but it is not what the
questionnaire encodes — and the two produce different systems:

| | Per client | Per application |
|---|---|---|
| OEight with Arc8 + a second app | one tenant, two apps inside | two tenants |
| Evidence segregation | shared across the client's apps | separated |
| Truveon flat fee (doc 10) | charged once per client | charged per app |
| Exit of one app | evidence stays in a live tenant | tenant can be retired whole |

The fee row is the one that decides it commercially, and the exit row is the
one that decides it for a client whose apps have different IP owners —
**which is exactly OEight's situation**: OEight owns Arc8, while Avergent
retains the IP for the app OEight builds for them. Those two applications
arguably should not share an evidence tenant at all.

**Not decided here.** It changes the questionnaire schema (a `truveon_tenant`
field, or none), the registry record shape, and the billing model. Owned by
Jamie and Wayne.

**Decide before** — the Truveon integration is built (phase 9.1), because that
integration will encode whichever answer is assumed.

---

## B-024 · Three threshold modes in 06a cannot be resolved

**Observed** — 2026-09-17, implementing prompt 4.1 against
`design/06a-alarm-specification.yaml`.

Four of six relative `threshold_mode` values resolve cleanly against the live
resource. Three do not, and the handler raises an exception for each rather
than substituting a number — because the prompt's own reasoning is that "a
fixed number is wrong across instance classes", and a wrong threshold is worse
than a recorded absence.

| Mode | Alarm | Why not |
|---|---|---|
| `percent_of_max_connections` | `rds-connections-high` | RDS exposes no API for the instance class's memory, and `max_connections` defaults to the parameter-group **formula** `{DBInstanceClassMemory/12582880}` — so reading the parameter group returns the formula, not a number. |
| `percent_of_instance_memory` | `rds-freeable-memory-low` | Same root cause. |
| `reference_metric` | `asg-in-service-below-desired` | Needs a metric-math alarm (`Metrics=[...]`), a different `put_metric_alarm` shape entirely. Not built. |

**The option not taken** was a hand-maintained `db.*.*` class-to-memory table.
It would work today and be silently wrong the first time AWS ships a class
nobody has added — and "silently wrong threshold" is the failure this whole
layer is designed against.

**Fixes, in order of effort:**

1. Set an explicit numeric `max_connections` in the DB parameter group at
   vesting time. The two RDS modes then resolve from the parameter group and
   the problem disappears rather than being worked around.
2. Build metric-math alarm support for `reference_metric`. Self-contained.

**Close before** — the first tenant runs RDS or an Auto Scaling group, since
until then these three raise no exceptions because no such resource exists.

---

## B-026 · Event-based alarms in 06a have nowhere to be created

**Observed** — 2026-09-17. `design/06a` defines six alarms with `metric: event`
— `rds-failover-event`, `rds-auto-restart-detected`, `s3-public-access-change`,
`asg-failed-scaling` and siblings. The spec is explicit: *"EventBridge rule,
not a metric alarm."*

**They are also not per-resource**, which is the part that decides where they
belong. A CloudTrail rule matching `PutBucketAcl` is one rule for the account,
not one per bucket; creating it per resource would produce N identical rules
all firing together on the same event.

So the instrumentation Lambda classifies them (`account_level_event_rules` in
its result) and creates nothing. **Nothing else creates them either** — that is
the gap. They are deliberately NOT raised as exceptions, because an exception
per resource for something correctly handled elsewhere is exactly the noise
that buries real ones.

**What it needs** — an account-level section in
`baseline/50-instrumentation.yaml` generating EventBridge rules from the
`metric: event` entries, with three source shapes to handle:
`rds_event_category` (RDS event subscriptions), `cloudtrail` (event names), and
`autoscaling_event` (event types).

**Note the severity** — `s3-public-access-change` pages in uat and prod. It
watches `PutBucketPolicy`, `DeletePublicAccessBlock` and siblings, which is a
bucket being opened to the internet. That is currently undetected.

---

## B-016 · Terraform state for PagerDuty is local and unlocked

**Observed** — 2026-09-17, building `pagerduty/`.

State is a local file. Two consequences, of different sizes:

**It holds the routing key in cleartext.** Not a Terraform defect — a routing
key *is* the credential, and anything that can create one can read it back.
Mitigated by `.gitignore` and by a pre-commit block that has no bypass, both
verified to fire. The residual risk is a laptop backup, not a repository.

**There is no locking, and Jamie runs concurrent sessions on this repo.** Two
applies at once against one PagerDuty account is the realistic failure, and
the symptom would be an escalation policy pointing somewhere unexpected —
discovered during an incident, which is the worst possible time.

**Fix** — S3 backend in the Log Archive account with versioning and the same
KMS key as the rest of the evidence, plus DynamoDB or S3 native locking. Not
done here because a backend block pointing at a bucket that does not exist
fails `terraform init` outright rather than degrading, so it has to land with
the bucket in the same change.

---

## B-017 · Alerting is single-region, and a second topic would not fix it

**Observed** — 2026-09-17, deploying `alerting/10-alert-topic.yaml`.

The topic is in `us-east-2` only. The obvious remedy — a second topic in
`us-west-2` — **does not do what it appears to**, and that is the part worth
recording.

CloudWatch is regional. An alarm in `us-east-2` is evaluated by `us-east-2`,
so if that region is impaired the alarm does not fire at all and the existence
of a topic elsewhere is irrelevant. A second topic only helps alarms that
themselves live in `us-west-2`, and there are none.

**What would actually help**, in increasing order of effort:

1. Alarms in `us-west-2` watching the replica buckets, publishing to a
   `us-west-2` topic. Narrow but real: it covers the case where replication
   breaks because the destination is impaired.
2. An external heartbeat — something outside AWS that pages when it stops
   hearing from the platform. This is the only construct that survives a
   region taking the alerting path down with it, and it is also what catches
   "the alarm was deleted" and "the topic policy was edited".

(2) is the right answer and is a phase 5 conversation, not a template change.

**Related** — B-013, which is the same shape for the detective consoles.

---

## B-018 · PagerDuty schedule uses the deprecated v1 resource

**Observed** — 2026-09-17, building `pagerduty/`. `terraform validate` warns
that `pagerduty_schedule` uses the legacy v1 API and will be removed.

**Why it was not migrated immediately.** `pagerduty_schedulev2` replaces a
rotation expressed as "rotate every N seconds" with calendar events carrying
RRULE recurrence, `effective_since`, and explicit start and end times. That is
a more expressive model and a more dangerous one: **a mis-specified RRULE does
not fail, it leaves a gap**, and the gap is found when an incident at 3am on a
Tuesday pages nobody.

Writing it safely needs a verification step that queries actual on-call
coverage across a full week after applying — and neither that step nor the
migration can be run until PagerDuty credentials exist. Clearing a deprecation
warning by writing an unverifiable schedule trades a notice for a silent
coverage hole.

**Close when** — credentials exist and the configuration has been applied once.
Migrate then, and verify with `GET /oncalls` over a seven-day window rather
than by reading the plan.

---

## B-019 · The PagerDuty provider panics on a documented-optional block

**Observed** — 2026-09-17, provider `PagerDuty/pagerduty` v3.36.0.

`pagerduty_alert_grouping_setting` — the provider's own recommended replacement
for the deprecated inline `alert_grouping_parameters` — **panics with a nil
pointer dereference** when the `config` block is omitted. Terraform reports
`Plugin did not respond` and a Go stack trace, neither of which names the
missing block.

Compounding it: the obvious field to put in `config` is `timeout`, which the
provider then rejects with *"'timeout' is only applicable when type is time"*.
The correct field for intelligent grouping is `time_window`. So the path from
the deprecation warning to working configuration runs through a crash and a
misleading field name.

**Recorded rather than fixed** because the fix is upstream. The working
configuration is in `pagerduty/main.tf` with the reason in a comment, so the
next person does not repeat the hour. Worth reporting to the provider
maintainers; not worth blocking on.

---

## B-020 · PagerDuty Service Graph — wanted for customer demonstrations

**Asked for** — 2026-09-17, by Jamie: *"I'm still going to want the nice big
services map inside PagerDuty as we build out — it's a cool impressive thing to
show customers."*

Recorded because it is a **stated commercial requirement**, not a technical
one, and those are the ones that get lost between phases and then reappear as
an expectation a week before a prospect meeting.

**What it is** — PagerDuty's Service Graph: `pagerduty_business_service`
resources with `pagerduty_service_dependency` edges to technical services.
Fully declarative in the provider, so it belongs in `pagerduty/` alongside
everything else.

**Already in the design** — doc 12 asks for a business service for the
client-conversation event class, with notification rules rather than paging
rules. Doc 13 repeats it in the onboarding task set. So this is scoped work
that happens to also be a demonstration asset, which is the good case.

**Live state, 2026-09-17** — alert grouping is **off** on the platform service
(`alert_grouping_type = "none"`). Recorded here because `terraform.tfvars` is
gitignored, so the repository otherwise shows a default of `intelligent` that
is not what is running and cannot run on this account.

The cost of `none` is bounded but real: a flapping alarm raises an incident per
evaluation cycle rather than one incident. Acceptable while nothing generates
alarms; worth revisiting before the first tenant. `content_based` may work on
this tier — it was not tested, so that is an open question rather than a known
limitation.

**Two things that must be true before it is worth building:**

1. **Tier.** Business services are a Business / Digital Operations feature.
   This account returned `403 Access Denied` creating an Intelligent Alert
   Grouping setting on 2026-09-17, which places it below that line. The map is
   therefore a **pricing decision**, not a build decision, and finding that out
   the week of a prospect meeting would be bad.
2. **Content.** Today the graph would be one business service pointing at one
   technical service — a picture of a single box. It earns the description
   "impressive" once Arc8, Avergent's application and the platform's own
   services are all present with real dependency edges.

**Build it with** — the per-tenant PagerDuty module in phase 12.2, so a graph
node is a by-product of onboarding a tenant rather than a diagram somebody
maintains by hand. A hand-maintained graph is wrong within two tenants, and a
wrong dependency map shown to a customer is worse than no map.

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

## B-003 · StackSets delegated administration is unassigned — CLOSED

**Observed** — 2026-09-16, during delegated admin registration.

Design doc 02 lists CloudFormation StackSets among the services that should run
under delegated administration. It was deliberately skipped: the natural home
is Platform Tooling in the Infrastructure OU, which does not exist yet, and
delegating it to Audit would put the deployment pipeline inside the account
that audits it.

**Status** — Closed 2026-09-17. `altdig-infra-tooling` (751479507989) is
registered for `member.org.stacksets.cloudformation.amazonaws.com`, and
`cloudformation describe-organizations-access` reports `ENABLED`.

**Two calls, and the second is the one that gets missed.**
`register-delegated-administrator` names the account;
`activate-organizations-access` is what lets it target OUs. Without the second,
the delegated account can create a service-managed StackSet and every OU target
fails with an error that does not mention organizations access.

**What this does NOT do** — move the four StackSets already in the management
account. See D-012.

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
