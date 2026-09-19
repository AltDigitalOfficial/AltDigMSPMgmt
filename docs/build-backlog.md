# Build Backlog

Technical debt and deferred work arising from the build itself. Distinct from
two other lists:

- [deviations.md](deviations.md) — accepted departures from the design package,
  each with a close condition
- [design/open-items.md](../design/open-items.md) — the design
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
| `reference_metric` | `asg-in-service-below-desired` | Needs a metric-math alarm. **Now buildable** — metric math landed with the `derived` support below; this one specifically was not converted and still raises an exception. |

**Update 2026-09-17 — a third alarm class was missed entirely.** Seven alarms
in 06a carry `metric: derived` with a `source` expression such as
`Errors / Invocations`. The handler took `derived` as a literal metric name and
created alarms watching a metric called `derived` in namespace `AWS/Lambda`.

Those alarms existed, looked plausible in a list, and **could never fire** —
`INSUFFICIENT_DATA` forever. Three were created in the canary, and drift
detection then recreated them hourly. It is the precise failure this layer is
built to prevent, shipped by the layer itself.

Metric math is now implemented for the `A / B` ratio form, which covers five of
the seven, including `lambda-error-rate` and both ALB 5xx rates. The
`minimum_invocations` guard the spec asks for is expressed in the metric math
rather than dropped — without it a function invoked twice with one error reads
as a 50% error rate and alarms on a sample of two:

```
IF(m2 >= 10, 100*(m1/m2), 0)
```

**Still unsupported, two of seven:** `config_resource_age` and
`task_stopped_events_per_window`. Neither is a ratio of two CloudWatch metrics
— they need data the handler does not have. Both raise
`derived-metric-unsupported` rather than being approximated, because an
approximation here produces an alarm that looks right and compares the wrong
things.

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

## B-027 · Alarms do not say what they are about

**Raised** — 2026-09-18, by Jamie: *"we need to be sure to tie them back to the
AWS object(s), and craft the message, in such a way where it's easy to tell
what the alarm is about."*

**The design already states the requirement.** Design doc 07's incident readout
opens with:

> `Alarm: what fired, when, on what, in which account`

and says that on escalation *"this is the PagerDuty payload, so the human opens
their laptop to a dossier rather than a blank console"*. Today's alarms deliver
roughly one of those four.

### What a woken engineer currently sees

The PagerDuty incident title is the CloudWatch alarm name, unaltered:

```
platform-auto-platform-instrumentation-lambda-error-rate
```

Six distinct problems in one string:

1. **It cannot be parsed back.** The format is
   `platform-auto-<resource-id>-<alarm-id>` and both halves contain dashes. Is
   the resource `platform-instrumentation` and the alarm `lambda-error-rate`,
   or the resource `platform-instrumentation-lambda` and the alarm
   `error-rate`? A human can guess; nothing else can. This same ambiguity
   already forced drift detection to run a full sweep rather than parse deleted
   alarm names.

2. **No account, and therefore no tenant.** Nothing says which partner, client,
   application or environment. With one canary account that is survivable. With
   forty member accounts every incident title is a resource id with no owner,
   and the first minute of every response is working out whose it is.

3. **No environment.** `dev` and `prod` alarms are typographically identical.
   Low urgency is currently the only signal, and that is a routing property
   rather than something visible in the title.

4. **The description leads with provenance, not diagnosis.** It currently opens
   with the spec's `note` and then explains which file generated the alarm.
   Useful to a maintainer, close to useless at 3am. It should open with what is
   wrong and what to look at.

5. **Metric-math alarms hide the resource entirely.** `describe-alarms` on a
   `derived` alarm returns no `MetricName` and no `Dimensions` — the resource
   appears only inside the `Metrics` array. Any tooling that reads the obvious
   fields to answer "what is this about" gets nothing.

6. **The tags lost the resource identity.** Alarms carry `platform-managed`,
   `platform-auto-instrumented`, `platform-alarm-id` and `platform-routing` —
   and no resource type or id. An earlier version tagged
   `platform-resource-type`; the rework onto 06a dropped it. So the
   machine-readable path to "what object is this about" does not exist either.

### The two layers, and why this cannot simply wait

Prompt 5.3 builds the dossier: a Lambda between the alarm and PagerDuty that
assembles the full readout from doc 07. That is the real fix for items 2, 3 and
4, and it is phase 5.

But **until 5.3 exists the alarm name and description ARE the payload**, and
phase 5 is several phases away. Meanwhile every incident that arrives is a
resource id with no owner.

The split worth making:

| Do now, cheap, not wasted by 5.3 | Wait for 5.3 |
|---|---|
| Restore resource type and id as TAGS — 5.3 will read them to build the dossier | Assembling the readout |
| An unambiguous separator in the name, or an id that carries account and environment | Recent changes, deployment correlation |
| Description that opens with the condition and what to check | Metrics and logs at time of trigger |

### Careful with the name

Changing the alarm name changes its identity. Drift detection matches on name,
so a rename makes every existing alarm an orphan and creates a parallel set —
which drift will dutifully report and refuse to delete. A rename needs a
deliberate one-off sweep, not a quiet redeploy. That is an argument for doing
it *soon*, while the population is six alarms in one account rather than
several hundred across forty.

**Do before** — the first tenant is vested. After that a rename is a migration,
and before that it is an edit.

---

## B-028 · SES is in the sandbox, so digests reach nobody

**Observed** — 2026-09-18, creating the SES identity for `altdigital.ai`.

A new SES account starts sandboxed: it may send only **to verified addresses**,
at low volume. **Verifying the sending domain does not lift this** — the two
are separate, and conflating them is the usual reason a first send fails after
DNS was done correctly.

So once the DNS records in [dns-requirements.md](dns-requirements.md) are in
place, the digest will authenticate and still not reach a client's technical
contact, because that address is not verified in our account — and verifying a
customer's mailbox is not something we can or should do.

**What it needs** — a production access request to AWS Support describing the
use case. Approved on the description, which here is straightforward:
transactional monitoring digests to named technical contacts at customer
organisations, low volume, every recipient under contract, bounce and complaint
handling already wired to an SES configuration set.

**Submit early.** It is not instant, and it is the last gate between a built
digest and the evidence trail design doc 06 depends on — *"if something later
fails unmonitored, the trail shows we asked."* Until it clears, we cannot show
we asked.

**Related** — the bounce path is already built (configuration set to SNS on
BOUNCE, COMPLAINT, REJECT, RENDERING_FAILURE), which is part of what the
request will be judged on.

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

---

## B-032 · The revalidation sweep is now load-bearing

**Built** — 2026-09-18, closing a defect rather than adding a feature.

`engine.py` referenced a "scheduled sweep" in three comments. It did not exist:
`baseline/60-response.yaml` had no schedule rule. The comments described a
mechanism that was never built, so `awaiting-settle` was a terminal state that
neither revalidated nor paged.

That mattered more than it first appeared. The settle table is
`clear_log_files` 120s, `restart_service` 180s, `cycle_ecs_task` 300s,
`scale_out` 600s, default 300s, against `MAX_INLINE_SETTLE` of 120s — so every
action but one settles above the cap. `awaiting-settle` was the *ordinary*
outcome of a remediation, not an edge case, which made the platform's normal
behaviour "change something on a customer's resource, then never look again and
never tell anyone".

Now built: a sparse GSI on the history table, `platform-response-sweep` every
two minutes, and `run_sweep()` returning `resolved-after-settle` or
`unresolved-after-settle`. A revalidation the engine could not *read* retries
three times before paging regardless; an alarm that is genuinely still
breaching pages on the first.

**Consequence for operations** — the sweep rule is enabled while the alarm
trigger is still disabled, which looks backwards and is not. Disabling the
sweep restores the silent-parking behaviour above. `platform-response-sweep-
stalled` alarms on it, with `TreatMissingData: breaching`, because a rule that
has stopped emitting reports no data at all.

**Revisit when** — the alarm trigger is enabled (B-031) and real volume shows
whether two minutes is the right cadence.

---

## B-033 · Sweep failures are deliberately not escalated

**Decided** — 2026-09-18, during the first deploy of the sweep rule.

Every terminal path in the engine now escalates unless the state means the
thing is fine, including states the engine does not recognise. Applying that to
the sweep's *own* failure was wrong: the sweep runs every two minutes, so one
broken dependency produced an escalation every two minutes. Observed live — the
GSI was still `CREATING` and every query failed.

A sweep failure now re-raises without escalating. The function's Errors alarm
and `platform-response-sweep-stalled` both deduplicate; a per-invocation
escalation does not, and the reliable end state of an alert every two minutes
is a muted topic that then swallows the incident pages too.

No incident is lost by this: parked items stay parked and are revalidated when
the sweep recovers.

**Revisit when** — anything else in the platform escalates from a schedule. The
general rule this is an instance of: *an else-branch on a timer needs
deduplication that an else-branch on an event does not.*

---

## B-034 · Questionnaire 7.3 is free text and nothing translates it

**Open** — 2026-09-18, prompt 4.3.

7.3 asks for a "success condition" in prose. `canaries/10-canary.yaml` takes it
as two structured parameters instead — `ExpectedStatus` and `BodyMustContain` —
because executing customer-supplied text would be a remote code execution path
into a tenant account reachable through an intake form.

The translation from prose to those two parameters is currently a human step at
vesting and is **not wired into `scripts/derive-vesting.py`**. A vested client
today produces no canary parameters, so the canary stack is deployed by hand.

**Revisit when** — the first real tenant is vested. The likely answer is that
7.3 gains structure in the questionnaire itself rather than being parsed, since
the parse would be guesswork on a field the design already calls the one
customers answer badly.

---

## B-035 · CloudWatch Synthetics now has a native Playwright runtime

**Open** — 2026-09-18.

design/06 says "There is no native Playwright runtime in CloudWatch Synthetics"
and builds the Fargate runner on that basis. That is no longer true:
`syn-nodejs-playwright-8.0` is available, and some Playwright runtime has been
since 2024-11. The design package predates it.

The Fargate runner is still justified, on a narrower basis than the design
gives: the native runtime runs a canary *script* written to the Synthetics
Playwright API, not a tenant's existing suite with its own config, fixtures and
dependencies. Questionnaire 7.5 asks about a suite "to reuse", and a suite that
must be rewritten is not being reused.

**Revisit when** — a tenant turns up whose suite is small enough to port. A
third `RunnerType` would drop the cluster, the image and the results Lambda, and
get screenshots and HAR capture for free.

---

## B-036 · Synthetics runtime versions deprecate on a rolling schedule

**Open** — 2026-09-18.

`syn-python-selenium-4.1` was the version the design-era documentation showed.
It is deprecated, and the failure is a 400 at stack creation reading
*"Deprecated runtime version specified"* — clear once seen, invisible until the
first deploy.

`canaries/10-canary.yaml` now pins `syn-python-selenium-12.0`, verified against
`aws synthetics describe-runtime-versions`. Nothing watches for that pin going
stale, and the symptom will be that new canary deployments fail while existing
canaries keep running — so it will surface during an onboarding rather than in
monitoring.

**Revisit when** — building the provisioning saga (Phase 12), which is the
natural home for a precondition check.

---

## B-037 · Fargate canary runner is built but untested

**Open** — 2026-09-18.

`RunnerType=fargate` is complete in the template — cluster, task definition,
scheduled RunTask, and the results Lambda that turns an exit code into the same
metrics — and `results.py` is unit-tested across all five branches (pass, fail,
killed with no exit code, missing suite container, missing timestamps).

It has **never been deployed**, because it needs a tenant Playwright image and
there is no tenant. The synthetics path is deployed and verified in the canary
account; the Fargate path is code review and unit tests only.

Specifically unverified: whether the ECS Task State Change event carries
`containers[].exitCode` in the shape `results.py` expects, and whether the
`awsvpc` networking parameters are right for a private subnet with no public IP.

**Revisit when** — the first tenant with an existing suite, or sooner with a
throwaway image that exits 0 and 1 on alternate runs.

---

## B-038 · Routing layer is built and unit-tested; nothing is deployed

**Open** — 2026-09-18, prompt 5.3.

The routing layer is complete in code and verified against stubs across all
five 06a routing classes, all four sources, the unrecognised-shape fallback,
and the dedup merge that makes a raw alarm and the dossier that follows it one
PagerDuty incident.

Nothing is deployed, and it cannot be until a step that is Jamie's:

1. `scripts/pagerduty-apply.sh --apply` — adds two Events API v2 integrations
   to the existing services. Additive and reversible; the CloudWatch
   integrations are untouched and remain the fallback path.
2. `scripts/sync-pagerduty-secrets.sh` — writes `events_v2_routing_key` into
   the existing secrets. Until this runs the router refuses to page rather
   than failing silently.
3. `alerting/20-router.yaml` into the Audit account.
4. `baseline/70-event-forwarding.yaml` into the sandbox OU with the bus ARN.

Nothing regresses while this sits undeployed. Alarm actions still publish to
the SNS topics exactly as they did, and `ESCALATION_VIA_BUS` defaults to
`false`, so the response engine keeps escalating by SNS.

**Specifically unverified** — the Events API v2 payload shape against a real
PagerDuty tenant, whether `aws:ResourceOrgID` is honoured on
`cloudwatch:ListTagsForResource` (if not, the router cannot read alarm tags
cross-account and every alarm routes to review), and the organization event bus
policy accepting cross-account `PutEvents`.

---

## B-039 · Three routing classes still have no real consumer

**Open** — 2026-09-18.

5.3 gives every 06a routing class a destination, which ends the state where
`digest_and_jira`, `jira_and_commercial` and `review_queue` alarms went to a
topic that does not page and has no reader. What they now have is a durable
queue, not an answer:

| Class | Destination | Consumer |
|---|---|---|
| `digest_and_jira` | `platform-jira-intents` | none until prompt 12.3 |
| `jira_and_commercial` | + `platform-commercial-events` | none until design 12 is built |
| `review_queue` | `platform-review-queue` | none |
| `digest` | recorded only | prompt 4.4 reads the routing log |

Queues retain 14 days. `platform-jira-intents-backlog` alarms at 500 messages,
which is the point at which "we will drain it when Jira exists" stops being
true.

The review queue is the one with no planned consumer at all. It is where an
untagged alarm goes, and an untagged alarm means instrumentation did not create
it — so the queue is a gap detector, and nothing yet looks at it.

**Revisit when** — prompt 12.3 (Jira) and Phase 11 (commercial). The review
queue needs a decision of its own: a weekly digest is probably right, but that
is 4.4's shape and 4.4 is already built.

---

## B-040 · Audit account Lambda concurrency is 10

**Open** — 2026-09-18, found deploying the routing layer.

`alerting/20-router.yaml` asked for `ReservedConcurrentExecutions: 20` and was
refused: the account's total concurrency is 10, AWS's default for an account
that has not used Lambda much, and reserving any amount leaves fewer than the
10 unreserved executions AWS requires.

The reservation is removed, which is worse than it sounds. A cap belongs on the
router — it POSTs to PagerDuty, and an alarm storm across many accounts should
queue rather than arrive as a thousand simultaneous incidents. With a ceiling of
10 the whole account throttles under a storm instead, and a throttled router
dead-letters what it cannot take.

Today that is theoretical: one member account, low event volume. It stops being
theoretical at roughly the tenth tenant.

**Do** — request a concurrency increase for 692869638531, then restore the
reservation.

---

## B-041 · Two-way paging splits, and the bugs they hid

**Fixed** — 2026-09-18, after five false PagerDuty incidents.

06a defines five routing classes. Two places reduced them to a boolean —
"pages" or "does not page" — and sent the second half to the low-urgency SNS
topic, which is **subscribed to PagerDuty**. So `digest`, `digest_and_jira` and
`review_queue` all paged.

    instrumentation/src/drift.py   event rules, all four of 06a's event alarms
    instrumentation/src/handler.py metric alarms, every non-paging alarm

It surfaced as five incidents titled "AWS API Call via CloudTrail" — a title
that says nothing — raised by the canary artifacts bucket deployment calling
`PutBucketPublicAccessBlock` to *enable* all four blocks. A hardening action,
paged as an incident, described by nothing.

Now: event rules target the routing layer, and non-paging metric alarms carry
no alarm action at all. Paging classes keep theirs, because that is the
fallback that still works when the router is broken.

**The general shape**, worth remembering: a boolean stand-in for a five-valued
decision does not fail loudly. It picks one of the two answers it has, and the
wrong one looks like a working alert.

---

## B-042 · The instrumentation role could not read any resource

**Fixed** — 2026-09-18.

`applies_when` has two resolvers, `read_replica` and `dlq_configured`. Both
call a describe API. The role had neither `rds:DescribeDBInstances` nor
`lambda:GetFunctionConfiguration`, so both always returned AccessDenied, which
the helpers swallowed into `None`, which means "cannot tell".

Consequences, all silent:

- `lambda-dlq-depth` and the RDS replica-lag alarm were **never created in any
  account**, and could not have been.
- Every Lambda evaluation recorded an exception — 25 in ninety minutes — which
  kept `platform-instrumentation-exception` permanently lit, destroying the one
  signal that says a real resource came up unmonitored. The same failure mode
  that `resource_types_no_alarms` was added to fix on 2026-09-17, from a
  different cause.
- The message read "could not evaluate applies_when 'dlq_configured' against
  platform-response", which points at the function rather than at the missing
  permission.

The describe permissions are granted, and the helpers now record the API error
code so an AccessDenied says so and says that it is a platform fault.

**Still open** — `threshold_mode: reference_metric` and the RDS memory-based
modes were never reachable either, for the same reason. Now that the reads
work they should be re-tested; nobody has seen them succeed. B-025.

---

## B-043 · There is no backup infrastructure, and phase 6 restores from it

**Open** — 2026-09-18, found starting prompt 6.1.

No `AWS::Backup::BackupPlan`, `BackupVault` or `BackupSelection` exists anywhere
in this repository outside `recovery/00-restore-fixture.yaml`, and no prompt in
phases 0–5 creates one. Prompt 6.1 builds a restore workflow; there was nothing
for it to restore from.

This is a gap in the prompt sequence, not an oversight in the build. The
document that would specify backup plan contents — `design/05-resilience.md` —
**is not in the design package**. What the supplied documents give is posture,
not values:

| Source | What it gives |
|---|---|
| design 02 line 62, design 14 line 84 | Reduced / Reduced / Full policy reduced frequency / Full policy |
| questionnaire 4.2 | RPO default 2h, "drives backup frequency and PITR" |
| questionnaire 4.4 | retention override per tenant |
| `org/00-org-structure.yaml:69` | a Backup OU reserved for Vault Lock |
| open-items.md V2 | "member account cannot delete or alter its own backups" |

Absent everywhere: schedule expressions, retention day counts, Vault Lock mode
and duration, cross-account copy targets, the selection tag scheme.

**Decided with Jamie before building** — do not invent them. The workflow is
parameterised on a vault and a source resource, discovers its own recovery
point, and treats "none found" as a first-class OUTCOME rather than an error.
That matters more than it sounds: 6.2 schedules this monthly per account, so an
error would light an alarm every month in every account until somebody muted
it, which is B-042's failure mode from a different cause. Instead each run
reports the true state — this account has no backups — as dated work.

A minimal sandbox FIXTURE was built so the mechanism could be proven live. It
is explicitly scaffolding: one unlocked vault, one 1 GiB empty volume, no plan,
no schedule, no retention rule. Vault Lock in particular is a one-way door, and
guessing its duration is not a reversible mistake.

**Deferred** — 2026-09-18, by Jamie. Not blocking anything today: there are no
tenants, no tenant databases and nothing whose loss would matter. It gates
B-044 and B-045, both of which are deferred behind it for the same reason.

**Revisit when** — design 05 arrives, OR the first tenant is vested, whichever
comes first. Vesting a tenant with no backup policy is the point at which this
stops being a gap in a sandbox and becomes a gap in somebody's production
account. The workflow itself needs no change; it needs a vault name and a
source resource.

---

## B-044 · Four of prompt 6.1's five validation checks are not implemented

**Open** — 2026-09-18.

Prompt 6.1 names five: *resource health, database connects, row counts
plausible, volumes mount, checksums match.* All five are reported by name in
every record. Only `resource_health` runs.

The other four need code running INSIDE the restored guest, or with network
reachability to it, and the worker Lambda runs in neither target VPC. Attaching
it to one breaks P4: a Lambda's VPC configuration is static, so it would need
two functions, one per mode, which is the two-procedures arrangement P4 exists
to prevent.

The mode-neutral answer is an executor launched INTO the target subnet at run
time — a Fargate task taking the subnet as a parameter is one state and one
task definition, and is genuinely P4-clean. It is **not built speculatively**,
and there is no disabled branch in the state machine, because a mechanism that
does not exist must not be described as though it does. Choosing its image and
above all where database credentials come from is design 05's decision.

What compensates, so this is not simply a hole: five control-plane checks DO
run — existence, health, target placement, encryption preservation, and
geometry against the recovery point. `target_placement` is the important one.
It proves a test restore landed in the isolated network rather than production,
which is the failure the whole arrangement exists to prevent and which would
otherwise look like a clean pass. `geometry_matches_source` catches the case
where a healthy, correctly configured, EMPTY resource passes everything else.

Every record carries `coverage: control-plane` and lists the not-implemented
checks, so no readout can imply more was verified than was. It does **not**
raise a ticket per run — the gap is this one item, not a monthly reminder that
the same thing is still true, which is how a real signal gets trained out of a
rotation.

**Deferred** — 2026-09-18, by Jamie, with more context expected by the time it
matters. The executor would have nothing to validate today: no tenants, no
databases, no backups.

Two things worth carrying forward rather than rediscovering:

  * The **credential question is governance, not engineering**, and has the
    longest lead time of anything here. To connect to a restored database you
    need credentials for it, and the three options have very different blast
    radius — reuse the source's master secret (the restore-test executor can
    then read every tenant's production database credentials, monthly, forever,
    to run a test); reset the master password on the restored copy (a mutation,
    needs `rds:ModifyDBInstance`); or IAM database authentication (requires the
    source database to have it enabled, a tenant configuration decision). Art's
    call as much as Jamie's.
  * `checksums_match` is **not fixed by building the executor**. It needs a
    checksum recorded AT BACKUP TIME to compare against, and nothing produces
    one. That half belongs to B-043.

`row_counts_plausible` is more tractable than it looks and needs no tenant
input: compare against the previous run's count with a tolerance.

**Revisit when** — B-043 delivers real backups of a real database, which is the
first moment this is buildable and the first moment it is worth building.
Practically: it is part of that work, not a separate item discovered later.

---

## B-045 · Teardown is armed for EBS only

**Open** — 2026-09-18.

`TEARDOWN_RESOURCE_TYPES` defaults to `EBS`. The RDS path is written and
reviewable; it is off.

Not because it is unwritten — because CLAUDE.md's hard rule makes an untested
delete path unacceptable, and EBS is the only type that can be exercised end to
end against the sandbox fixture today. A `DeleteDBInstance` call that has never
once run is exactly the code that should not be armed by default.

Same idiom as `ResponseEnabled` in `baseline/60-response.yaml`: the capability
exists and is turned on per account by somebody who has looked at what it would
do there. An unarmed type is reported as an orphan — a ticket and a running
resource, which is the cheap side of this trade.

The RDS path never deletes automated backups (`DeleteAutomatedBackups: False`)
and says so in its record. This platform's automation does not delete backups
by any path.

**Deferred** — 2026-09-18, by Jamie. Behind B-043: there is no RDS backup to
restore, so there is no RDS restore test to exercise the teardown against.

**Revisit when** — the first RDS restore test runs in a sandbox. Until then the
unarmed default is the correct posture, not a limitation.

---

## B-046 · The restore test schedule is not validated against the HOOP

**Open** — 2026-09-18.

design/14 line 211: restore testing *"must not collide with a turndown window;
schedule inside the HOOP"*. The default is the second Tuesday at 15:00 in
`America/Chicago` — inside a weekday business-hours HOOP for a US tenant, and
chosen deliberately over the obvious out-of-hours slot, which would have run
against a turned-down environment.

It is a sensible default, not a checked one. HOOPs do not exist yet — phase 13
— and open-items.md V11 owns the validation.

**Close when** — phase 13 builds HOOPs and validates maintenance and restore
windows against them at configuration time.

---

## B-047 · `platform-isolated` has an internet gateway, and isolation is a route property

**Fixed** — 2026-09-18, found deploying.

The V4 isolation assertion was first written as "does the target VPC have an
internet gateway or an active peering connection". Pointed at the sandbox it
failed, and the VPC it failed was named `platform-isolated`.

`baseline/40-network.yaml` attaches an InternetGateway **unconditionally**, to
every VPC it builds, including one whose `EgressProfile` is `isolated`. Its own
comment is accurate about what the profile means: *"'isolated' has no NAT
gateway, no elastic IP and no default route out of the private subnets. That is
the entire implementation of the profile."* So the VPC has an IGW and a public
subnet routed to it, and its private subnets reach nothing.

The check was wrong, not the VPC. Reachability is a property of the route table
a subnet uses, not of what is attached to the VPC somewhere. The assertion now
resolves each target subnet's route table — explicit association, or the VPC
main table where there is none, which is the case that would otherwise be
missed — and looks for routes via internet gateway, NAT gateway, transit
gateway, peering connection or egress-only gateway. Blackholed routes do not
count; they reach nothing by definition.

That is both stricter and more accurate: it catches a transit gateway and a
peering connection in the same pass, which the gateway-presence check had to
concede in its own evidence that it did not.

**A second live finding inside the first.** The sandbox's only private subnet
routes to an S3 **gateway VPC endpoint**. Counting that as an escape would have
left no subnet in the account able to pass, which would have read as "the
isolation check works" while actually meaning "nowhere passes". A gateway
endpoint is not another network — it reaches an AWS service through an endpoint
policy — so it is reported under `service_endpoints` in the evidence and does
not fail the assertion. Whether a restore test VPC that can reach S3 counts as
isolated is a design 05 question, and recording it is how that gets decided on
evidence rather than discovered later.

**Still open, as a question for `baseline/40-network.yaml`** — a VPC named
`platform-<profile>` that always has an IGW attached is accurate about its
private subnets and overstated in its name. Nothing is presently wrong: a
resource placed in its public subnet would be reachable, and `isolated` is the
profile where somebody would least expect that to be possible.

---

## B-048 · `ListRecoveryPointsByResource` denied, and the message named nothing

**Fixed** — 2026-09-18, found on the first live run.

The worker role granted every `backup:*` action the workflow uses. The call
still failed:

    AccessDeniedException ... Insufficient privileges to perform this action

`iam simulate-principal-policy` said `allowed` for the same action and the same
role. The same call as an admin succeeded. CloudTrail recorded
`errorCode: AccessDenied` with `errorMessage: "An unknown error occurred"`.

The cause is a parameter default. `ManagedByAWSBackupOnly` defaults to
**false**, which asks AWS Backup to also enumerate recovery points it does not
manage — raw EBS snapshots taken by anything else. To do that, Backup queries
EC2 using the CALLER's credentials, so the call also needs
`ec2:DescribeSnapshots`. Admin had it; the worker deliberately does not.

Granting `ec2:DescribeSnapshots` would have made the error go away and been the
wrong fix. A recovery point AWS Backup does not manage **cannot be restored by
`StartRestoreJob` at all**, so listing one only defers the failure to the
restore step, where it costs a started job and a worse message. The workflow
restores through Backup and now only considers what Backup can restore.

**The general shape**, worth remembering alongside B-042: an IAM policy that is
complete for the API you think you are calling can still be denied, because a
parameter DEFAULT widened the call into a second service. The simulator will
not show it, because the simulator evaluates the action and not the arguments.

---

## B-049 · The list API does not return the resource type the restore needs

**Fixed** — 2026-09-18, found on the second live run.

`ListRecoveryPointsByResource` omits `ResourceType` — it is implied by the
resource you asked about, so the API leaves it out. The workflow selects a
restore metadata override by resource type, so it refused with:

    no restore metadata override for resource type ''

which reads as an unsupported resource rather than a missing field.

`DescribeRecoveryPoint` does return it, so the authoritative record is now
fetched for the ONE point selected rather than for every point listed. Where
the point sits in a vault this account cannot describe, the type is derived
from the source ARN instead.

That fallback carried its own bug, caught by a unit test rather than by AWS:
field 5 is the resource segment in both ARN layouts, but EC2 uses
`.../volume/vol-123` (slash) while RDS uses `...:cluster:name` (colon). Reading
the LAST colon-delimited field gives `vol-123` for one and the cluster's *name*
for the other, so every Aurora cluster looked like a plain RDS instance.

---

## B-050 · CloudWatch cannot alarm on a monthly metric

**Fixed** — 2026-09-18, found deploying prompt 6.2.

Prompt 6.2 asks for duration trend tracking so that creep is *"visible months
before it breaches"*. That was first built as a CloudWatch alarm over the
projection metric, alongside a second alarm for "the monthly test stopped
running". The stack rolled back:

    Metrics cannot be checked across more than a week
    (EvaluationPeriods * Period must be <= 604800) for alarms using period >= 3600

So the longest window any alarm with an hourly-or-longer period can examine is
**one week**. The restore test runs **monthly**. Every alarm over its metrics
would look at an empty window three weeks in four — permanently breaching or
permanently blind depending on `TreatMissingData`, and both are worse than not
having the alarm at all.

The trend warning moved into the workflow, which already has the projection in
hand at the moment it records: a run trending at the RTO declares
`digest_and_jira` instead of `digest`, and the router turns that into a dated
ticket and a digest line. Same single routing layer as everything else, and no
metric-freshness problem. `recovery/20-restore-schedule.yaml` now contains no
alarms at all.

**Still open — nothing detects that the monthly test stopped running.** That
detector was the second alarm, and it cannot be a CloudWatch alarm for the same
reason. It needs something running DAILY that evaluates freshness and publishes
it, which is exactly the heartbeat prompt 9.1 already specifies: *"Heartbeat
per tenant, so absence of data is an event rather than a quiet month."*
Building a one-off here would mean two heartbeat mechanisms.

Prompt 6.2 does not ask for this alarm; it was added and then removed.
`RestoreRunsRecorded` is published on every run including failures, so the data
such a detector needs is already being written.

**Deferred** — 2026-09-18, by Jamie. Risk today is near zero: one sandbox
account, no tenants, and the consequence of an unnoticed stoppage is a test not
running rather than data not being recoverable.

It stops being near zero at the **first tenant**, because from that point the
schedule is the only thing proving the restore path works and nothing watches
it.

If phase 9 has not landed by then, the cheap interim is the digest rather than a
second heartbeat: `reporting/10-digest-sender.yaml` already runs on a weekday
cron and already reads routing records, so restore-test freshness becomes a line
in a report a human reads. That fits P5 — automation reports, humans review —
and costs far less than building a heartbeat mechanism twice.

**Revisit when** — prompt 9.1, or the first tenant is vested, whichever comes
first.

---

## B-051 · Every `digest` event lands in the review queue

**Open** — 2026-09-18, noticed reading the routing log during 6.1 verification.

B-039's own table says the `digest` routing class is *"recorded only"*, with
prompt 4.4 reading the routing log. `alerting/src/router.py` does something
else — `deliver()` ends with:

    if not spec["pages"] and not spec.get("ticket"):
        enqueue(REVIEW_QUEUE_URL, ...)

`digest` pages nothing and tickets nothing, so every digest-class event is
enqueued to `platform-review-queue`.

Pre-existing and not introduced by phase 6; visible because the restore test's
own `digest` record landed there next to a stream of Security Hub findings.

Why it matters: the review queue is described as the gap detector — *"where an
untagged alarm goes, and an untagged alarm means instrumentation did not create
it"*. Diluting it with every routine digest event makes the one message that
means something indistinguishable from the ones that do not. That is B-041's
shape again: not a loud failure, just a signal quietly losing its meaning.

**Decide** — either `digest` stops being enqueued and 4.4 reads the routing log
as B-039 says, or genuine gaps get a queue of their own. Not changed here: it
alters routing behaviour for every digest-class event in the platform, which is
not a phase 6 change.

**Deferred** — 2026-09-18, by Jamie. The review queue has no consumer at all
today (B-039), so a queue nobody reads being diluted by events nobody reads
changes nothing yet.

**Revisit when** — the review queue gets a consumer. That is the moment the
dilution starts costing something, and the moment there is enough context to
choose between the two options above rather than guessing.


---

## B-052 · UAT alerting sensitivity should be a per-tenant field

**Open** — 2026-09-18, from Jamie's resolution of D-013.

Prompt 6.2 says a restore duration breach pages. design/14 line 84 says UAT has
no page routing. The build followed the tier, and recorded the difference as
D-013.

Jamie's resolution is that both documents were asserting a default for something
that legitimately varies per tenant. The question belongs in the onboarding
questionnaire — *"how sensitive do you want us to be on UAT?"* — and it is a
commercial choice as much as a technical one, because paging on UAT means
somebody has to be available to answer.

**Why it is not a one-line change.** Making UAT sensitivity per-tenant means the
ROUTER has to know it, and the router is deliberately the single place the "who
gets woken up" decision is made. Putting a second copy of that decision
anywhere else is B-041's shape.

The mechanism already exists in the right shape:
`/platform/routing/account-tiers` in SSM, written by
`scripts/sync-account-tiers.sh` from deployed stackset parameters, is already a
per-account map the router reads on the alert path. A sensitivity value rides
that rather than needing anything new.

**Three pieces, in order:**

1. `design/onboarding-questionnaire.md` section 3a gains the field. Design
   package source material — not edited by build work.
2. `vesting/questionnaire.schema.json` implements it, alongside `uat_required`
   and `release_cadence`, and `scripts/derive-vesting.py` derives from it.
3. The router reads it for UAT accounts, defaulting to today's behaviour
   (ticket, no page) when unset — an unset value must not silently start
   paging somebody.

**Note the scope boundary.** The support-cost and resource-availability
consequences Jamie identified are pricing and SLA work, which CLAUDE.md places
out of scope for this build. What is in scope is the field, its derivation, and
the routing behaviour.

**Revisit when** — the first tenant opts into UAT, or the questionnaire is next
revised.

---

## B-053 · Memory is not captured during preservation

**Open** — 2026-09-19.

design 07's preserve stage asks for four things: snapshot volumes, capture
instance metadata and running configuration, copy logs to Forensics, and
**"where feasible, capture memory"**.

Three are implemented. Memory is not, and it is not feasible from where
containment runs. Capturing memory requires executing inside the guest — an
agent plus a capture tool, driven by SSM Run Command — and the responder role
holds no `ssm:SendCommand`, deliberately: a containment role that can run
arbitrary commands on a tenant instance is a much larger thing than one that
can change a security group.

Reported on every incident as `memory_capture: {attempted: false, reason: ...}`
rather than silently skipped, so a readout cannot imply memory was considered
and found unnecessary.

Note the ORDERING problem this creates, which is why it is not simply deferred
and forgotten: isolation cuts the network path a memory capture would travel
over. If memory capture is ever built it has to run inside stage 1, before
isolation, and retrofitting it after the fact means revisiting the sequence
rather than adding a step.

**Revisit when** — there is a real incident where volatile state matters, or
design 04 (Security Controls and Evidence) arrives and specifies the agent.

---

## B-054 · The responder role could not read where evidence goes — B-042 again

**Fixed** — 2026-09-19, found on the first live containment.

The first end-to-end drill preserved evidence correctly, isolated the instance
correctly, revoked correctly — and left every snapshot sitting in the account
being contained. `describe-snapshot-attribute` showed no share.

The cause: `PlatformSecurityResponder` granted no `ssm:GetParameter`, so
reading `/platform/security/forensics-account` was denied. The helper caught
every exception and returned `None`, which the calling code read as "no
Forensics account is configured" — and wrote exactly that into the incident
record. The parameter existed, in the right account, with the correct value.

**This is B-042 with different nouns.** That item is three weeks old and says
so in its own words: two describe calls denied by a missing permission,
swallowed into "cannot tell", producing alarms that were never created in any
account. The same mistake was made again here, in new code, by the same author,
having read the backlog entry.

What makes it worse than a missing permission: the record BLAMED THE
CONFIGURATION. An operator reading that incident would have gone looking for an
unset parameter rather than a missing policy, and the evidence would have
stayed reachable by whoever compromised the account.

Both halves fixed. The role grants `ssm:GetParameter` on
`/platform/security/*`, and `_forensics_account` now returns `(value, problem)`
and distinguishes `ParameterNotFound` — genuinely unconfigured — from anything
else, which is reported as a PLATFORM FAULT in the incident record.

**The general shape, stated for the third time:** a bare `except: return None`
around an AWS call converts "I was denied" into "the answer is no". Those are
different answers and only one of them is actionable. If a helper swallows an
exception, it must return the reason alongside the absence.

---

## B-055 · The router's else had a hole, and a security incident fell through it

**Fixed** — 2026-09-19, found deploying prompt 7.1.

Containment emitted its summary as `platform.security`. Nothing routed it. No
page, no ticket, no review-queue entry, no routing-decision log line — a
complete security incident, correctly contained, and no record anywhere that
anyone should be told.

`alerting/20-router.yaml` has four rules. `DossierRule` is the only one that
filters on SOURCE as well as detail-type. `UnmatchedRule` — the catch-all whose
whole job is "an event nobody planned for becomes visible instead of vanishing"
— excludes by DETAIL-TYPE alone:

    detail-type: [anything-but: [..., 'Platform Incident Dossier']]

So a dossier from an unlisted source matched neither. Not `DossierRule`,
because the source was not in its list; not `UnmatchedRule`, because the
detail-type was in its exclusion list. EventBridge discarded it silently, which
is what a bus does with an event no rule matches.

Fixed by `UnmatchedDossierRule`, which matches the dossier detail-type with
`source: anything-but` the three known emitters.

**Worth generalising.** An "else" written as `anything-but` over ONE field is
only an else for that field. Any rule that narrows on a second field creates a
gap the catch-all cannot see, and the gap is invisible precisely because
nothing arrives to report it. Every future rule that filters on source, detail,
or account needs its own matching escape hatch — or the catch-all has to be
written over the same fields.

The immediate trigger was mundane — the router had not been redeployed after
`platform.security` was added to its rule — but the deploy order was not the
bug. The bug is that getting the deploy order wrong produced SILENCE rather
than a review-queue entry saying "unrecognised dossier source".

---

## B-056 · A compliance control finding declared a security incident

**Fixed** — 2026-09-19, found on the first live declaration.

The declaration recogniser for Security Hub matched design 07's four conditions
by keyword, and then fell back to declaring on any `CRITICAL` or `HIGH` severity
label when no keyword matched. The first live run declared an incident for:

    SSM.7 SSM documents should have the block public sharing setting enabled

An AWS Foundational Security Best Practices control. Rated CRITICAL. Describing
a configuration default that has never been touched.

It was harmless on the day only because the finding named an ACCOUNT
(`AWS::::Account:754280127660`) and no instance, so containment had nothing to
preserve, isolate or revoke and completed with a shrug.

**The dangerous version is one line away.** `EC2.8 instances should use IMDSv2`
is the same family, the same severity, and names an EC2 INSTANCE. In a
production account at high confidence that would have isolated a healthy
running server — cutting it off from the network — because of a metadata
service setting. design 07 names that exact outcome as the thing to avoid:
"Isolating a healthy production service on a false positive is its own
incident."

Fixed in three parts:

  * findings whose `Types` begin `Software and Configuration Checks` are never
    declarations — that is the compliance family, and compliance drift is the
    alerting layer's business
  * the attack families ARE declarations: `TTPs`, `Effects`,
    `Unusual Behaviors`, which describe observed adversary behaviour rather
    than configuration state
  * severity NEVER decides declaration on its own. It modulates confidence,
    which is a different question

**What this says about "bias toward over-declaration".** design 07's instruction
is about not DEBATING — declaring is cheap, standing down is fine. It is not a
licence to treat every highly-rated finding as a breach. The cost of declaring
is bounded by what containment then does, and containment acts on production, so
the filter has to sit at declaration where it can still be reasoned about.

---

## B-057 · Step Functions fails a state whose Parameters name a missing path

**Fixed** — 2026-09-19, found deploying prompt 7.1.

`Finalise` reads `$.error`, `$.isolate`, `$.revoke` and `$.blast_radius` so that
one state can report every outcome. None of them exist on every path: `$.error`
is written only by a Catch, and the gather-and-page route skips isolation
entirely. Step Functions does not treat a missing JSONPath in `Parameters` as
null — it fails the state:

    The JSONPath '$.error' specified for the field 'error.$' could not be found

Both the happy path and the gather path died at the LAST state, after
containment had already preserved, isolated and revoked. That is the worst
possible place for it: the actions had happened and the record of them had not.

Fixed with an `Initialise` Pass state that seeds every optional field as null
before `Triage`, so `Finalise` has one shape for every route.

**Worth remembering for any future state machine** — `recovery/10-restore-
workflow.yaml` avoided this only by accident, because its record step reads the
whole state object (`state.$: "$"`) rather than naming individual fields. Naming
fields is better for readability and strictly more fragile.
