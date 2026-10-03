# Build Backlog

Technical debt and deferred work arising from the build itself. Distinct from
three other lists:

- [deviations.md](deviations.md) — accepted departures from the design package,
  each with a close condition
- [design/open-items.md](../design/open-items.md) — the design
  package's own open questions, owned by Jamie, Art and Wayne
- [scale-watch.md](scale-watch.md) — things that are correct at today's scale
  and stop being correct at an identifiable point, each with its trigger

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

**DECIDED 2026-09-23, Jamie: one Truveon tenant per client.** Closed.

The Avergent case above does not argue against it once "client" is read the way
the questionnaire already reads it. The application OEight builds for Avergent,
with Avergent keeping the IP, is onboarded with Avergent as the client — so it
gets Avergent's tenant, not OEight's. The IP owner is the client, and the
tenant follows the client. Two applications share a tenant only when the same
organisation owns both, which is the case where sharing is right.

What encodes it:

- registry: `truveon_tenant_id` on the CLIENT record (`registry.sh
  link-truveon`), not on an application or account record
- questionnaire section 16 (`truveon:`), with `existing_tenant` for a second
  application of an onboarded client; applications are modelled inside the
  tenant (Truveon's `applications`)
- evidence: one platform tenant per client, `<partner>-<client>`
  (`scripts/lib/evidence_tenants.py`), one ingest key each
- billing: design 10's flat Truveon fee is per client. Wayne should confirm
  that is what the commercial model meant; nothing in the build depends on the
  answer.

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

**Requested 2026-10-03** — `sesv2 put-account-details` from Infra Tooling
`751479507989`, mail type TRANSACTIONAL, review `PENDING`. The use case says
what the account is (the tooling account of AltDigital's AWS MSP platform),
what it sends (B-155's daily digest to named client contacts, under contract),
the volume, and the bounce path. It also points AWS at Truveon's account
`852064978666`, which already has SES production access, as the same company
and the same people, in case reputation is the concern. It does not claim bounce
routing is finished: B-155 records that nobody subscribes to the topic yet.
AWS answers through a support case on the Tooling account.

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

## B-014 · Two deviation IDs are used twice — CLOSED

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

**Closed 2026-09-29.** "Later" is by commit, not by position in the file:

| Old | New | First recorded |
|---|---|---|
| D-009 `CommercialReadOnly` assignment | D-009, unchanged | d2fdc21, 2026-09-16 18:20 |
| D-009 account alias prefix | **D-023** | be9726b, 2026-09-16 19:32 |
| D-010 Identity Center users outside Entra | D-010, unchanged | d2fdc21, 2026-09-16 18:20 |
| D-010 per-application Object Lock mode | **D-024** | 89a580a, 2026-09-17 07:06 |

Inbound references moved in `CLAUDE.md`, this file and
`vesting/derivation-rules.yaml`. References in `identity/`,
`scripts/deploy-commercial-access.sh` and `README.md` already meant the entries
that kept their numbers, so no deployed resource text changes. Each renumbered
entry carries a note giving its old number, because commits and estate dumps
from before today cite the old one and cannot be edited.

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

**Correction, 2026-09-29: the first option above is backwards.** A harness
role at `/xrole/PlatformSquatPath` was denied `access-analyzer:DeleteAnalyzer`
by SCP 01 (B-135). If `aws:PrincipalArn` were the assumed-role ARN, which drops
the path, 01's `arn:aws:sts::*:assumed-role/Platform*/*` exemption would have
matched `assumed-role/PlatformSquatPath/gr-squat` and let it through. It did
not. SCPs see the IAM role ARN, path included, which is what the AWS
documentation says. So the three `assumed-role` variants are the dead weight,
and the `iam::role` variants are the ones doing the work. Dropping those would
have removed every exemption in 01–03 at once. The earlier "live run
confirmed" observation is not reproducible from anything recorded, and it is
superseded. Dropping the `assumed-role` variants recovers the same bytes.
The exemption rewrite (B-135) does that, and the harness probe (still exempt
afterwards, or every differential turns AMBIGUOUS) is what proves it changed
nothing.

**Quota, 2026-09-30:** Service Quotas reports `L-C48BCE79` "Service control
policy document size" as **10,240** bytes, not 5,120. The pressure this item
records is gone. `validate-policies.sh` still enforces 5,120, which is now a
self-imposed margin rather than the AWS limit. It is kept deliberately until
someone decides otherwise, because a policy near 10 KB is itself a review smell.

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
| `15-partner-model.md` | Account alias prefix `ad-` | `altdig-` — `ad-security-audit` is taken by another AWS customer (D-023) |
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

**UNBLOCKED, partly built** — 2026-09-22, on design 05's arrival.

design 05 supplies exactly what was missing, and the entry below was right
that posture is not values. What it now gives:

| | |
|---|---|
| mechanism | AWS Backup, driven from the Organization, pushed to the Members OU |
| destination | copies into a **separate backup account** |
| protection | **Vault Lock** — "a compromised member account cannot destroy its own backups" |
| control | vault deletion and lock modification under quorum (design 03) |
| RPO | 2 hours, and "standard daily snapshots do not meet this" |

`recovery/30-backup-policy.yaml` is built against it: a locked vault, a
**continuous** plan for the types that can meet a 2-hour RPO through
point-in-time recovery, and a separate 2-hourly snapshot plan for EBS and EC2,
which cannot. A single 2-hourly plan over everything would cost twelve times a
daily one on resources that gain nothing from it.

**Two scale-watch items fire on the day this deploys** — the restore test reads
the LOCAL recovery point rather than the cross-account copy this policy exists
to create ([W-003](scale-watch.md#w-003--restore-tests-read-the-local-copy-not-the-protected-one)),
and the recovery point's age is captured but never compared to the RPO
([W-004](scale-watch.md#w-004--rto-is-measured-rpo-is-captured-and-never-evaluated)).
Both are cheap and both touch this policy's output.

Two things in it are worth knowing before it is deployed. Vault Lock in
compliance mode is **irreversible by anyone including AWS**, so
`ChangeableForDays` is set rather than locking immediately. And the vault
carries an access policy denying deletion to every principal except
`PlatformQuorumExecutor`, which is design 05's quorum requirement expressed as
a policy rather than as a convention.

**Still open, and it is not code.** design 05 requires a *separate backup
account* and the organization does not have one — the estate is management,
audit, log-archive, tooling, forensics and two sandboxes. The copy destination
in the template names a vault in an account that must be vested first, and the
Organization-level backup policy that pushes plans to the Members OU is not
written because it has nowhere to copy to.

**Next:** vest the backup account, then the org policy. Both need a working
session; neither needs another design decision.

The original entry follows.

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

**STILL OPEN after design 05** — 2026-09-22, and worth saying explicitly
because 05's arrival cleared its neighbours.

05 confirms the five checks ("database connects, row counts plausible, volumes
mount, file checksums match") and confirms the monthly cadence. It says nothing
that removes the obstacle, which was never a missing specification: the worker
Lambda does not run inside the target VPC, and attaching it to one would break
P4 by requiring a function per mode.

The answer is unchanged — an executor launched INTO the target subnet at run
time — and it is still not built speculatively.

**Before it is built, one thing is owed from outside.** Checks 2-4 are
statements about somebody else's application, and the platform should not be
the one inventing them. [scale-watch W-006](scale-watch.md#w-006--the-validation-split-is-a-contract-question-not-just-a-build-one)
carries the split: AltDigital proves the infrastructure and the data came back,
the application team defines what proves the application is usable, the
platform runs both and combines the evidence.

The original entry follows.

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

**CLOSED 2026-09-22.** RDS exercised end to end against a real
`db.t4g.micro` in the exercise account, and armed by default.

The objection was always specific and never about the code being unwritten:
*"a `DeleteDBInstance` call that has never once run is exactly the code that
should not be armed by default."* It has now run.

**All three refusals were exercised first, and each left the instance
running:**

| case | outcome |
|---|---|
| `mode: recovery` with everything else correct | refused — teardown is test-mode only |
| RDS absent from the armed set | refused — reported as an orphan for a human |
| execution id does not match the resource's tag | refused — provenance failed |

Only the fourth case, armed and in test mode with matching provenance,
deleted.

**The half that matters more was also proven.** `DeleteAutomatedBackups` is
`False`, and after the instance was deleted its automated backup reported
status `retained`. "This platform's automation does not delete backups" is now
a verified claim rather than a documented intention — and those are different
things, which is the whole reason this entry existed.

Still a parameter and still narrowable per account. Arming remains a decision
somebody makes about a specific account; what changed is where that decision
starts from.

The original entry follows.

**UNAFFECTED by design 05** — 2026-09-22. Recorded because this entry sits
among the ones 05 unblocked and is not one of them.

05 does not discuss teardown arming, and would not: the reason RDS is off is
that a `DeleteDBInstance` call has never once run, and no design document
changes that. It is unarmed until it has been exercised against the sandbox
fixture, which is a testing task rather than a specification gap.

The original entry follows.

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

**CLOSED 2026-09-22** by prompt 13.4. `interactions.check_window_conflicts`
takes a `restore_test` window and reports every day on which it falls outside
the HOOP, with the consequence attached: the restore test is skipped or fails,
and a restore path that is never exercised is design 05's whole concern.

It is checked at CONFIGURATION time rather than at run time, which is what the
original entry asked for -- the arithmetic is over two declared schedules and
needs nothing to be running, so a conflict is answerable at vesting rather
than discovered by a missed test. `interactions.validate(strict=True)` raises
rather than recording a note.

The original entry follows.

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

---

## B-058 · The readout cannot ask Access Analyzer what a principal can reach

**Open** — 2026-09-19.

design 07's readout asks for "what else that principal can reach (**Access
Analyzer**, policy evaluation)". The policy half is implemented — the attached
and inline policies the principal carries, which is what an incident responder
reaches for first. Access Analyzer is not consulted.

The reason is that there is usually nothing to consult. An IAM Access Analyzer
is a per-account, per-region resource, and nothing in this platform creates
one. Querying an analyzer that does not exist returns an empty result, and an
empty "external access findings" section reads exactly like a clean result —
which is the failure mode this platform keeps running into (B-042, B-054). So
the readout states that the check was not made and why.

What Access Analyzer would add over the policy list: whether the principal can
be assumed from outside the account, and which of its resources are shared
externally. That is genuinely different information from "what policies does it
have", and it is the half that answers "how did they get in".

**Revisit when** — an analyzer is provisioned per account, which is most
naturally part of the member baseline rather than of containment. Design 04
(Security Controls and Evidence) is the document that would say so, and it is
absent.

---

## B-059 · Notification windows are per-partner only; the client level is not stored

**Open** — 2026-09-19.

design 07: "Notification windows are fields in the onboarding record at **two
levels** — per partner and per client. Both run simultaneously from the same
`discovery_time`, and an unset window at either level blocks go-live."

Only the partner level is stored. `scripts/registry.sh` writes `altdig_hours`
and `notify_hours` onto the PARTNER record and writes no window onto a CLIENT
record. The questionnaire field that would carry the client level —
`baa_window_partner_to_client_hours`, field 2.6 — is validated by
`scripts/derive-vesting.py` (it blocks vesting when PHI is true and the value
is null) and then not persisted anywhere the clock can read.

So today every client under a partner inherits that partner's window. A client
who negotiated a tighter one would be counted against the wrong deadline, and
the clock would report a comfortable margin that does not exist.

Three small pieces: `registry.sh client` gains a window argument,
`sync-notification-windows.sh` prefers the client value over the partner
default, and `clock.py` needs no change — it already reads whatever the map
gives it per account.

**Revisit when** — the first client negotiates a window different from their
partner's default, or at the first real vesting, whichever comes first.

---

## B-060 · The package did not contain the handlers the template named

**Fixed** — 2026-09-19.

`containment/10-containment.yaml` was deployed with three Lambda functions
whose code was a package containing only two of the four modules. The stack
deployed clean. `platform-incident-readout` and `platform-notification-clock`
would both have failed at invocation with "Unable to import module" — during an
incident, which is the only time either of them runs.

Two causes, both worth keeping:

**The module list was maintained by hand.** `publish-containment.sh` named
`contain.py` and `declare.py` in a `cp` line. Adding two modules to the
template required remembering to add them to the script, and nothing connected
the two. Fixed by deriving the check FROM the template: the publish script now
greps every `Handler: <module>.handler` out of the CloudFormation and refuses
to package if the corresponding file is not in the archive. The template is the
thing that declares what must exist, so the template is what the check reads.

**The publish script was piped to `head`.** The run that was supposed to upload
the corrected package printed its key and exited before the upload, because
`head` closed the pipe and the script took SIGPIPE. The key was then quoted
into a deploy, which failed with S3 `NoSuchKey` — an error that points at the
bucket rather than at the pipe.

CLAUDE.md already carries this trap, under "Windows / Git Bash traps":

> **Do not pipe a script into `tail`/`head` when the exit code matters.** The
> pipeline returns the *last* command's status, so a `set -e` abort inside the
> script reports success. Redirect to a file and read the file.

It is written about exit codes. The failure here was worse than a wrong exit
code: the script did not finish its work at all, and the truncated output still
looked like a successful run. Redirect to a file and grep the file.

---

## B-061 · `AWS_PROFILE=` sets an empty profile rather than unsetting it

**Fixed** — 2026-09-19, found writing `sync-notification-windows.sh`.

To run one AWS call with assumed-role credentials instead of the configured
profile, the obvious form is an inline prefix:

    AWS_ACCESS_KEY_ID=... AWS_PROFILE= aws dynamodb scan ...

That sets `AWS_PROFILE` to the empty string, which is not the same as unsetting
it. The CLI looks for a profile named `""` and fails:

    The config profile () could not be found

Which reads like a broken `~/.aws/config` and sends you to the wrong file
entirely. There is no shell syntax for unsetting a variable for the duration of
one command, so the fix is a subshell:

    RESULT="$( unset AWS_PROFILE
               export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
               aws dynamodb scan ... )"

Every other script in `scripts/` that assumes a role does this correctly, by
accident of structure: they `unset AWS_PROFILE` once at the top level after
assuming, because they then make several calls. This was the first place a
single call needed it inline.

Compounding it, the call had `2>/dev/null` on it and reported "Could not read
the registry table" — which points at the table name and at permissions, and
the cause was neither. The error is now captured and printed.

---

## B-062 · Auto-deployment has no ordering between stack sets

**Open** — 2026-09-19, found creating the exercise account.

A new account landing in a targeted OU receives every service-managed stack set
concurrently. Three of them failed on first deploy, and all three failures were
ordering:

| Stack set | Failure |
|---|---|
| `platform-kms-secrets` | `NoAvailableConfigurationRecorder` — a Config rule created before `platform-config` had created the recorder |
| `platform-instrumentation` | "The specified KMS key does not exist" — a log group created before `platform-log-data-protection` had created `alias/platform-logs` |
| `platform-canary-smoke` | "You do not have access to the bucket" — the canary's artifact bucket is in another account |

The first two are genuinely transient and a retry fixed both. That is the
problem: nothing retries.

**Correction, 2026-09-30: the third row was never an ordering failure.**
`platform-canary-smoke` carried `ArtifactsBucketName` as one StackSet-level
value: the Canary account's bucket, entered by hand at deploy. On a StackSet
a parameter has one value for every account, so Sandbox Exercise's canary
pointed at a bucket in another account. No retry could ever have fixed it.
The Exercise stack is `DELETE_COMPLETE` behind an `OUTDATED` instance, and
Exercise's own artifacts bucket exists.

`canaries/10-canary.yaml` now derives the bucket name per account, and the
parameter is gone. The live StackSet is left as it is. It is management-owned
and due to be recreated under Infra Tooling (F-2), where the corrected
template creates Exercise's canary fresh. Only Canary's instance needs the
retain-and-import path.

**Fixed 2026-10-02.** `platform-canary-smoke` was redeployed from the corrected
template. `deploy-stackset.sh` kept all 19 existing parameter values
(`UsePreviousValue`) and dropped the removed `ArtifactsBucketName`. Both
instances are `CURRENT`. Exercise's canary is `RUNNING` and writes to its own
account's bucket. `settle-baseline.sh` reports all 11 of Exercise's stack sets
`CURRENT`, so the estate's one known-red instance is gone. The stack instances sit at `OUTDATED` indefinitely,
the account looks provisioned, and the missing pieces are exactly the ones that
make it monitored — instrumentation is how alarms get created at all.

This matters far more at vesting than it did here. Prompt 12.5's verification
sweep is supposed to fail closed on a half-provisioned account, and it checks
outcomes rather than stack-instance status, so it would catch the consequence.
But a member account that silently comes up without instrumentation between
provisioning and the sweep is a window where the platform believes it is
watching something it is not.

Two candidate fixes, neither built: give the dependent stack sets an explicit
retry after the account settles, or make each one tolerate its dependency being
absent and converge on a later update. The second is more work and much more
robust, because it removes the ordering assumption rather than sequencing
around it.

**Revisit when** — prompt 12.x builds the provisioning saga, which is where
"an account is fully baselined" becomes a state somebody asserts rather than
assumes.

**Built 2026-10-01: the retry option.** The baseline StackSets now target
Members, so Arc8's first vesting would have hit this.
`scripts/settle-baseline.sh --account <id>` runs from Infra Tooling as
delegated admin, in three phases:
1. It waits until every StackSet targeting an OU on the account's path has an
   instance there and none is still running.
2. It retries each non-`CURRENT` instance once, in dependency order (config
   before kms-secrets, log-data-protection before instrumentation). The retry
   is `update-stack-instances` with no overrides, so nothing changes except
   finishing what failed.
3. It refuses with the list unless every instance is `CURRENT`.

`INOPERABLE` instances are not retried, because a re-run cannot repair them.

`vest-client.sh` step 6b runs it for each new account, and stops vesting on
failure.

Proved on real accounts:
- Canary: 11 stack sets, all `CURRENT`, nothing retried, exit 0.
- Exercise: retried `platform-canary-smoke` once, which failed again on its
  real fault (B-062 correction above), and refused with exit 1, naming the
  instance.

The "tolerate the dependency being absent" option stays open. It is the more
robust fix, and the trigger is a second dependency failure that one retry does
not cure.

---

## B-063 · Isolation is verified at the control plane, not by reachability

**Open** — 2026-09-19.

The containment exercise asserts isolation three ways: the isolation security
group is attached to every interface, the group has no ingress rules at all,
and the instance is still running. All three are control-plane facts.

What it does not do is try to reach the instance and fail. A route table, a
NACL, or a VPC endpoint could in principle leave a path open that the security
group alone does not describe, and the drill would pass.

Doing it properly needs a prober — something inside the VPC that attempts a
connection and reports the refusal — which is the same in-VPC executor problem
as B-044, and would be the same piece of work. The drill target is also a bare
instance with no listening service, so "cannot connect" would currently prove
very little without first putting something on it to connect to.

Worth stating plainly: the current check proves the isolation group is applied
and is itself restrictive. It does not prove unreachability.

**Revisit when** — B-044's in-VPC executor exists, since the two share a
mechanism.

---

## B-064 · `sync-account-tiers.sh` kept a hand-written list of platform accounts

**Fixed** — 2026-09-19, found creating the Forensics account.

The script derived tenant account tiers from deployed stack set parameters —
correctly, and with a good comment explaining why the stack set is more
truthful than the account name. It then added the platform's own accounts from
a list written out in the script:

    for name in altdig-security-audit altdig-security-logarchive altdig-infra-tooling

Creating the Forensics account left it absent from the map, and the router
treats an absent account as `prod`, which pages. The right answer arrived by
omission rather than by design, and it would not have been the right answer for
the exercise account, which is `dev` and was also briefly absent.

Now discovered from `/platform/org/account/`, which is where
`create-platform-account.sh` registers every platform account — so the thing
that creates the entries is the thing the list is read from. Accounts that
already took a tier from a deployed stack set are skipped, which keeps the
sandbox and exercise accounts at `dev` rather than being overwritten.

Same lesson as B-060, four days apart and in a different file: a list
maintained by hand alongside the thing that generates its entries will
eventually disagree with it, and the disagreement is silent.

---

## B-065 · V3 is confirmed, and now has evidence

**Closed** — 2026-09-19.

open-items.md V3: "Member account with full local admin cannot block the
containment role. Test: Phase 7 blast-radius verification."

design 07 calls the assumption load-bearing. It has now been tested, twice, by
the scheduled exercise against a role in the exercise account that IAM grants
the exact rights to disable containment:

    tag_the_containment_role                         blocked  AccessDenied
    attach_a_deny_policy_to_the_containment_role     blocked  AccessDenied

Both denied by `DenyPlatformRoleModification` in
`policies/scp/03-protect-platform-identity.json`. The test isolates its
variable deliberately: because IAM grants the actions, a denial can only have
come from the SCP.

Confirmed further by what the drill did NOT leave behind — the containment role
carries no tags afterwards, because the probe that would have applied one never
succeeded.

**This is now checked monthly** and its result is carried in the exercise
evidence. It is worth noting what that changes: V3 moves from an assumption
somebody wrote down to a control with a date on it, which is the difference an
auditor is asking about.

---

## B-066 · Three scripts now scan the registry and publish a slice of it

**Open** — 2026-09-19.

`sync-account-tiers.sh`, `sync-notification-windows.sh` and
`sync-tenant-facts.sh` all do the same thing: scan the registry table in
Tooling, project out a few fields, and write a per-account map into Audit as an
SSM parameter. Three scans, three parameters, three consumers.

The duplication is deliberate for now and should not be collapsed
reflexively. Each map has one consumer and one meaning — the router reads
tiers, the incident clock reads notification windows, reconciliation reads
change projects — and a single shared document would couple three components'
release cycles together. A change to what reconciliation needs would force a
redeploy of the routing layer.

What makes it worth revisiting is the operational half, not the code: three
scripts must each be re-run after vesting, and forgetting one produces a
different silent wrongness in each case. An account missing from the tier map
routes as prod and pages; missing from the windows map reports its notification
obligations as unknown; missing from the facts map reconciles against the
platform default.

**Revisit when** — a fourth appears, or vesting (prompt 12.x) automates the
post-vest steps, at which point "re-run these three" becomes one saga step and
the duplication stops costing anything.

---

## B-067 · Reconciliation would have skipped accounts absent from the tenant map

**Fixed** — 2026-09-19, before first deployment, found by running the sync
against an empty registry.

The first version of `recent_deployments()` derived the list of accounts to
reconcile from `/platform/change/tenant-facts`. With no tenants vested that map
is `{}`, so reconciliation would have queried nothing — while capture happily
recorded deployment events from the sandbox under
`deployment#754280127660`.

The general failure is worse than the empty-map case. Any account absent from
the facts map — newly vested, or one the sync had not been re-run for — would
have its deployments CAPTURED and then never compared. Evidence would
accumulate, nothing would examine it, and the account would read as clean
rather than as unexamined. That is the same silent-gap shape as B-062 and
B-042, arrived at from a third direction.

Fixed with a `by-record-type` GSI on the change table, keyed
`record_type` + `occurred_at`. Reconciliation now reads the deployments
themselves and covers everything that was captured; an account with no tenant
record still gets reconciled, against the platform default, with that fact
recorded in the finding rather than applied silently.

**The rule worth carrying:** derive the work list from the EVIDENCE, not from a
configuration map. A configuration map answers "who should exist"; the evidence
answers "what actually happened", and reconciliation is a control over the
second.

---

## B-068 · The Jira search endpoint is unverified against the live API

**Open** — 2026-09-19.

`change/src/jira.py` calls `/rest/api/3/search/jql`, which is Atlassian's
replacement for the retired `/rest/api/3/search`. The request shape, the
parameter names and the response envelope are written from documentation
rather than from a live call, because no API token exists yet.

Three things specifically unverified:

- whether `fields` is accepted as a comma-separated string or must be an array
- whether the response still returns `issues` at the top level
- whether pagination is `nextPageToken` rather than `startAt`, and whether the
  engine needs to follow it at the volumes involved

None of this is guesswork about behaviour — it is guesswork about an interface,
which is the kind that fails immediately and loudly rather than subtly. The
engine already treats any Jira error as `change_source_unavailable`, so a wrong
endpoint degrades to "no conclusion drawn" rather than to a false compliance
finding.

**Close when** — the service account token is in Secrets Manager and
reconciliation has compared a real deployment against a real change record.
That is the last unverified part of 8.2.

---

## B-069 · The change record shape, and the one field it is missing

**Open** — 2026-09-19. Recorded here so it is not lost between sessions; Jamie
will implement the scheme in Jira separately. This is the platform-side half:
what the code needs from it, and why.

Jamie's intended change record carries:

| Field | Purpose |
|---|---|
| business reason | what the change is meant to accomplish |
| technical review | who owns saying the change will accomplish it |
| business review | who confirmed it is requested and the reason is valid |
| change start time | when the change is approved to begin |
| change end time | when changes and testing are expected complete |
| rollout plan | the exact steps |
| rollback plan | how the previous state is restored |
| technical testing | how we know the change was implemented well |
| business testing | how we know the business outcome was achieved |

That is a stronger shape than design 08 assumes, and two of the fields make
existing code better rather than merely fitting it.

**THE MISSING FIELD: what the change touches.**

Nothing in the list identifies the target — account, environment, or resource.
Both prompts need it, and they fail differently without it:

- **8.2** currently attributes a change to an account by TIME proximity plus an
  optional `account-<id>` label, because no target field existed to match on.
  Time proximity is a heuristic; it will eventually match a deployment to an
  unrelated change in the same window and report a clean result.
- **8.3** cannot satisfy design 08's fifth quorum condition at all —
  *"The approval is for this specific resource and action"*. With no target
  field, an approval for deleting key A would verify successfully against the
  deletion of key B. That is not a weak control, it is a defeated one.

A label (`account-754280127660`) is enough and needs no scheme change, which is
why the engine already reads one. A custom field is better if the scheme is
being designed anyway.

**Start and end time make design 08's third outcome real.** "Deployment outside
an approved window" is currently approximated by matching against `created`
within a configurable number of hours, because there was no window to compare
to. With real start and end times it becomes the actual check design 08 asks
for, and `CHANGE_MATCH_WINDOW_HOURS` can stop being a guess.

**Two distinct review roles map onto quorum better than "any two approvers".**
design 03 requires two approvals with the requester excluded. A scheme that
names a TECHNICAL reviewer and a BUSINESS reviewer gets that structurally, and
gets something design 03 does not ask for and probably wants: the two approvals
come from different perspectives rather than from two people who both read the
same paragraph.

**Rollback plan is worth surfacing during an incident.** Not used by
reconciliation, but `customer_caused()` already correlates deployments with
incidents. When it finds a deployment minutes before an outage, the rollback
plan for that change is precisely what the responder needs next, and the
platform will already know which change record it was.

**Two kinds of record, one shape.** Worth separating before the scheme is
built:

- **Tenant change records** live in the tenant's project and are what 8.2
  reconciles deployments against. Approvers are the tenant's.
- **AltDigital quorum requests** live in the ops project (AIINFRA) and are what
  8.3 verifies before a key deletion, vault lock modification, account removal
  or CloudTrail disable. Approvers are {Jamie, Wayne, Art} per design 03.

The field list above suits both. The approver sets do not overlap, and 8.3
must not accept a tenant's approver as a quorum member.

**Close when** — the scheme exists in Jira and the engine is pointed at it.
Until then 8.2 works on the label convention and 8.3 treats a missing target
field as a refusal to execute rather than as a pass.

---

## B-070 · The Jira service account is a personal identity wearing another name

**Open** — 2026-09-19. Owner: Art (mailbox provisioning), Jamie (Jira account).

open-items.md B10 asks for "Service account identities for Jira, PagerDuty,
Truveon — **platform identities, not personal**."

What exists is `platform@altdigital.ai` as a mail ALIAS onto Jamie's mailbox.
That is not an Atlassian identity. Atlassian knows an account by the address it
was registered with; aliasing happens at the mail layer and is invisible to it.
So the credential the platform holds authenticates as Jamie's personal
Atlassian account, under a name that reads like a service account.

Proved immediately rather than theoretically: the first `jira-check.sh` run
returned HTTP 401 for `/rest/api/3/myself`, because the alias is not an
address Atlassian will authenticate.

**Why this is not cosmetic.** Change reconciliation is a SOC 2 control, and its
evidence trail runs through this credential:

- every Jira read in the audit log is attributed to a named individual rather
  than to the platform, which muddies exactly the separation design 08's
  reconciliation is meant to demonstrate
- deactivating or recovering that individual's Atlassian account stops change
  reconciliation, and the failure is silent by design — the engine reports
  `change_source_unavailable`, which is correct and looks like a quiet month
- 8.3's quorum control verifies that "neither approver is the requester". A
  credential that IS one of the three quorum members reading its own approvals
  is a conflict an auditor will ask about, even though the client is read-only

**What is actually required.** A real mailbox — not an alias — so that
Atlassian can send the account a verification email, security notices and the
token expiry warning. Then an Atlassian account registered to it, granted
product access to Jira, scoped read-only, and a fresh classic (unscoped) API
token minted from it.

Jamie cannot provision the mailbox; **Art can**.

**Interim, accepted deliberately.** The token is minted from a personal
Atlassian account so that Phase 8 can be verified end to end. Recorded as a
deviation rather than left to look finished.

**Close when** — Art provisions a shared mailbox, an Atlassian account is
registered to it, and `scripts/set-jira-token.sh` is re-run with it. Rotation
is a single command and Secrets Manager retains the previous version, so the
swap is low risk. Before the first tenant is vested.

---

## B-071 · The Jira token expires 2027-09-19, and the platform watches it itself

**Open** — 2026-09-19. Not action; a mechanism worth knowing about.

Atlassian caps API token lifetime at one year. This one dies on 2027-09-19.

When it does, change reconciliation degrades to `change_source_unavailable` on
every run. That is the correct behaviour and produces no false compliance
findings — and it is indistinguishable from a healthy platform in a quiet month
unless somebody is watching. The expiry notice goes to the Atlassian account's
email, which is outside the platform's control and, today, an alias (B-070).

So the platform watches its own credential rather than depending on an email
reaching a human. Every reconciliation run publishes
`AltDigital/Change JiraTokenDaysRemaining`, and
`platform-jira-token-expiring` fires below 30 days.

Worth contrasting with B-050, where the equivalent idea was impossible. A
CloudWatch alarm cannot span the gap between MONTHLY restore tests, because
`EvaluationPeriods x Period` is capped at one week. Reconciliation runs every
six hours, so the metric is dense enough for an ordinary threshold alarm. Same
problem, opposite answer, and the difference is cadence.

**On rotation** — re-run `scripts/set-jira-token.sh` and update
`PLATFORM_JIRA_TOKEN_EXPIRES` in `config/platform.env`. The date is not a
secret and is deliberately in plain config where it can be read.

---

## B-072 · AIINFRA is a JSM project, and JSM already has the change record

**Open** — 2026-09-19, found by the first successful `jira-check.sh` run.

The check reported AIINFRA's issue types:

    [System] Service request
    [System] Service request with approvals
    [System] Incident
    Task
    Sub-task

Two things follow.

**There is no `Change` issue type**, so `PLATFORM_JIRA_CHANGE_ISSUETYPE=Change`
matches nothing. The engine reports `change_source_unavailable` rather than
reporting every deployment as unapproved — the check working as intended — but
reconciliation cannot draw a conclusion until this is settled.

**AIINFRA is Jira Service Management**, and that changes the recommendation for
open item B9. The instinct was to design a change scheme by hand. JSM ships a
Change Management request type set whose fields line up with Jamie's list
almost item for item:

| Jamie's field | JSM equivalent |
|---|---|
| business reason | Change reason |
| change start time | Change start date |
| change end time | Change completion date |
| rollout plan | Implementation plan |
| rollback plan | Backout plan |
| technical testing | Test plan |
| technical review + business review | Approvers, via the approvals feature |
| business testing | no native field — custom |
| *(the missing target)* | Affected services, or a custom field / label |

Adopting it beats authoring one: the fields already exist, the workflow already
exists, and it is what an auditor expects to see in a JSM instance.

**The approvals feature matters more than the fields.** design 08 warns that
the pipeline must "validate rather than trusting the ticket's status field
alone", and JSM approvals are a first-class object rather than a status string:
`/rest/servicedeskapi/request/{id}/approval` returns each approver's
`accountId`, their decision, and when they made it.

That is exactly what prompt 8.3 needs to check design 08's five conditions —
both approvers authorised, neither is the requester, approvals postdate the
request. Reading a real approval object is a materially stronger control than
inferring approval from a workflow status, and it removes the guesswork about
what "approved" means as a status name.

**What this changes about the build.** 8.3 should read JSM approvals as its
primary evidence, with the status field as corroboration rather than as the
source of truth. 8.2's `approved` heuristic — a list of status names treated as
approved — becomes a fallback for non-JSM projects rather than the main path.

**Decide** — enable Change Management request types in AIINFRA (or a dedicated
change project), then set `PLATFORM_JIRA_CHANGE_ISSUETYPE` to whatever the
request type is actually called. Jamie is implementing the scheme in a separate
session; this is the input for it.

---

## B-073 · A dev sandbox finding raised a real PagerDuty incident

**Fixed** — 2026-09-19, found on the first successful end-to-end reconciliation.

The first real change-reconciliation finding — a deployment in the DEV sandbox
with no change record — routed to PagerDuty and raised an incident:

    routed : platform-change-no-change-record -> pagerduty
    why    : platform.change escalation, tier prod
    sent   : {"pagerduty": {"status": 202, ...}, "jira": {"sent": true}}

Account 754280127660 is `dev` in `/platform/routing/account-tiers`. The router
had it available and did not look.

**The cause.** `decide()`'s dossier branch read the tier off the dossier and
fell back to `"prod"` when absent:

    tier = norm.get("environment_tier") or "prod"

The response engine has always set `environment_tier`, so for two phases the
fallback was never exercised. The restore workflow set it too. Change
reconciliation does not — it knows the account but not the tier — and the
moment a third emitter appeared, the default fired.

**The fix is the one the code already used elsewhere.** The FINDING path in the
same function has always called `account_tier()` to resolve an account to a
tier from the shared map. The dossier path now does the same, falling back to
`prod` only for an account genuinely absent from the map — which still pages,
and is still the right way to fail.

**Why this is B-041's family.** Not identical: B-041 was a five-valued decision
reduced to a boolean. This is a tier lookup replaced by an optimistic default.
Both share the shape that matters — a routing decision made from an assumption
rather than from the map that exists to answer it, failing silently in the
direction of waking somebody.

**The general rule, now applied in both places:** if the platform has a map
that answers a question, no code path may answer it by assumption. The map's
own fallback is the only permitted default, because it is the one that
announces itself in the routing record.

**Note for the operator** — a real PagerDuty incident was raised by this and
carries dedup key `b87cad4b1cfa3716d69e8d97792f4869`. It is a true finding
about a genuine unapproved deployment, just at the wrong urgency.

---

## B-074 · A platform deployment was reported as the tenant's unapproved change

**Fixed** — 2026-09-19, found by Jamie asking whether the platform can file
change records.

The first real reconciliation finding said a deployment in account
754280127660 had no change record. True, but attributed to the wrong party. The
captured principal was:

    arn:aws:sts::754280127660:assumed-role/OrganizationAccountAccessRole/platform-deploy

That is ALTDIGITAL deploying a stack, not the tenant deploying an application.
The engine reconciled it against the tenant's change project and reported the
tenant as having failed to file a change they were never party to.

design 08 draws this line explicitly and the first version did not read it.
"The control point" describes AppDeployer and tenant deployments. A separate
section, "The platform's own pipeline", describes AltDigital's — "the most
privileged thing AltDigital owns. It can modify every member account" — with
its own approval model, currently Jamie as sole approver for baseline changes.
Two kinds of change, two sets of records, two approvers.

**Why it matters more than an attribution quibble.** Every StackSet rollout,
every baseline update and every platform stack deployment lands in a member
account. Once there are real tenants, each one would have produced an
unapproved-change finding against that tenant. Hundreds of false accusations,
in the control whose entire value is that the tenant trusts its findings.

**Fixed by classifying the deployer**, three ways:

| Principal | Class | Reconciled against |
|---|---|---|
| `PlatformAppDeployer-*` | tenant | the tenant's change project |
| `OrganizationAccountAccessRole`, `stacksets-exec-*`, `Platform*` | platform | AltDigital's ops project |
| anything else | **unknown** | the tenant's, and said loudly |

The third row is the one worth having. design 08's premise is that AppDeployer
is the ONLY path a tenant deploys through, so a deployment by neither
AppDeployer nor a platform role came from a credential nobody has accounted
for. The finding now says that rather than leading with the missing paperwork —
somebody deploying with local static credentials is a finding about the control
point itself, and strictly more interesting than a missing ticket.

**What this leaves open.** AltDigital's own deployments are now reconciled
against the ops project, where they will be reported as having no change
record — because the platform does not file change records for its own
deployments. That is design 08's "Interim: Jamie is sole approver for baseline
changes" showing up as a daily finding rather than as a process. It will need
either platform change records (12.3 drains `platform-jira-intents` and could
file them) or an explicit, recorded exclusion for platform-managed deployments.

Not decided here. Flagged because the alternative is a finding nobody can
action, and those are how a rotation learns to ignore a queue.

## B-075 · `schema.type: "string"` does not mean the field takes a string

**Fixed** — 2026-09-19, found by filing the first real change record.

The first filing attempt was rejected:

    HTTP 400
    customfield_10063: The field value is not valid Atlassian Document Format
    customfield_10060: The field value is not valid Atlassian Document Format
    customfield_10059: The field value is not valid Atlassian Document Format

Those are Test plan, Backout plan and Implementation plan. `filer.py` already
had an `_adf()` helper and already used it for `description`; it sent the three
plan fields as plain strings because the create metadata reports all four as:

    "schema": {"type": "string"}

**That is the trap.** `schema.type` does not distinguish a plain text field
from a rich text one. Both are `string`. The discriminator is `schema.custom`,
which the client was discarding:

    ...customfieldtypes:textarea      takes ADF
    ...customfieldtypes:textfield     takes a string

The error names custom field ids, not labels, and says nothing about which of
the three plausible causes it is — wrong format, wrong value, or field not on
the screen. Without the raw response body kept whole by `_post`, the message
would have been unactionable.

**Fixed by deriving the shape from the metadata** rather than from a list of
field names in the code. `jira.py` now returns `custom`; `filer.py`'s `put()`
wraps a string in ADF when the field's custom type is a textarea, or when it is
a rich text system field (`description`, `environment`), and otherwise passes
the value through untouched.

**Why derived and not a list of three names.** Within the hour, Jamie added a
fourth rich text field — Business Test Plan — to the change record. A
hand-maintained list would have been wrong immediately and would have failed
the same way, on the next filing, with the same opaque error. This is B-060 and
B-064's rule for the third time: derive from the source of truth, do not keep a
second copy of it. The unit test asserts the negative case too, because a
wrapper applied too broadly would corrupt the option and datetime fields rather
than fail loudly.

## B-076 · Business Test Plan exists but cannot be set when the record is created

**Fixed** — 2026-09-19 by Jamie adding the field to the change screens. No code
change was needed, which was the point: the filer sets whatever the metadata
says the record can carry. AMI-4 populated it on the first filing after the
screen change, and `fields_unavailable` went to empty.

Original finding follows.

Jamie added "Business Test Plan" to the change record. It does not appear in
the create metadata, so the platform cannot populate it when it files a record.

The field is real:

    customfield_10079 | Business Test Plan | textarea | NOT on create screen
    customfield_10063 | Test plan          | textarea | on create screen

**Two causes look identical from `createmeta` and need different fixes.** A
field missing from create metadata is either not on the site at all (the name
is wrong) or on the site but absent from this issue type's CREATE screen (a
screen configuration). createmeta reports both as simply absent, so
`find_field` was added — read-only, listing site fields and marking which are
creatable — rather than guessing and sending somebody to the wrong page.

**The fix is in Jira, not in code:** add Business Test Plan to the create
screen for `[System] Change` in AMI. Project settings → Screens → the Change
issue type's create screen.

**Until then nothing breaks.** `put()` sets a field only if the project exposes
it, so the record files without it and `fields_unavailable` names it in the
output. That tolerance is deliberate and load-bearing for tenant projects,
which will not all carry the same fields — but it does mean a field silently
going missing looks the same as one deliberately absent, which is why the
filer reports the list rather than only logging it.

The filer already accepts the value: `business_test_plan` in the event, or any
field name at all via `extra`. It starts being populated the moment the screen
carries it, with no code change.

## B-077 · The change records the platform filed could not carry an approval

**Fixed** — 2026-09-19, found while starting 8.3.

Asking AMI-1 for its approvals returned:

    404  Either you don't have access to this request type, or it doesn't exist
         i18nKey: sd.customerview.error.vpOriginMissing

Not a permissions problem. An issue created through `/rest/api/3/issue` is a
valid Change issue and is **not a JSM request**: it has no Request Type, which
JSM calls `vp-origin` and stores in `customfield_10010`. Every servicedeskapi
endpoint returns 404 for such an issue, the approval endpoint included. The
error names the missing thing — *vpOriginMissing* — which is the only reason
this took minutes rather than an afternoon.

**Why it is worse than cosmetic.** Quorum verification (8.3) reads approvals as
its primary evidence, deliberately, because design 08 says not to trust the
status field. A record that cannot carry an approval cannot be verified at all.
The platform was filing records that were structurally unverifiable by the
control being built to verify them.

**Fixed by raising records as requests.** Where the project is a service desk
with a matching request type — resolved by NAME, `Request a change`, because the
id differs per site — the filer posts to `/rest/servicedeskapi/request`. Where
it is not, the issue API is used and the output says `approvable: false` with
the reason, rather than leaving it to be discovered as a 404 during a quorum
check. Tenant projects will not all be JSM.

**Two things this dragged in.**

*The request form is not the create screen.* A request type exposes a narrower
field set than the issue type can carry, and `/rest/servicedeskapi/request`
rejects anything outside it. Asked, not assumed, via the request type's own
field endpoint. Labels are not on it.

*Labels are load-bearing.* Reconciliation reads `account-<id>` labels to decide
which deployments a change record covers. Losing them silently would mean the
record matches nothing and every deployment in that account is reported as
unapproved — a false accusation produced by a half-successful filing. So labels
are set by a follow-up edit and **a failure there is fatal**, naming the issue
key and the labels to add by hand. The edit happens before any approval exists,
so it cannot disturb design 08's condition 4.

*And the same shape trap in mirror image.* The issue API takes rich text as
ADF; the request API takes it as a plain string. Values are now held raw and
rendered per path. Building the payload once and sending it to whichever
endpoint answered is precisely how this would send ADF where it is rejected.

**The publish guard had to be narrowed, and it was over-broad rather than
wrong.** It banned the whole `servicedeskapi` namespace to stop the filer
approving anything. But creating a request and approving one both live there,
so the ban blocked the filer's actual job and forced the unverifiable records
above. It now matches the forbidden ACTS — approval and transition endpoints —
not the address space. Fourth instance this session of a guard matching a word
rather than a construct.

## B-078 · An approval exists only once the issue reaches the approval status

**Fixed** — 2026-09-19. The first diagnosis was wrong and is kept below,
because the way it was wrong is the finding.

**What this actually is.** The `Request a change` workflow DOES have an
approval step. It sits on the **Authorize** status, with the rule *one person
from each group named in the record's Approver groups field must approve*.
Jamie configured the group `AWS Core Infra Approvers`.

A newly raised request is created in **Review**, which is the status before
Authorize. JSM creates the approval record when the workflow ENTERS the
approval status, so a request sitting in Review has no approval record at all:

    AMI-3   status=Review   approvals=0
    AMI-4   status=Review   approvals=0

Identical, from the API, to a request whose approvers have all refused to act.

**Two things were needed, and only one was a code change.**

*Approver groups on the record.* The field is on the issue screens but NOT on
the request type's form, so `/rest/servicedeskapi/request` cannot set it at
creation. Those are two separate configurations in JSM, and it is easy to check
the first and conclude the second is fine — which is exactly what happened, and
the screen config was correct. The filer now applies the field in the
post-create edit, alongside labels. A record filed without a group cannot be
approved at all, and an unapproved record does not cover a deployment, so this
would have surfaced as our own build work being reported as unapproved change.

*The transition into Authorize is a human act, deliberately.* The platform
files the record and stops. It does not move its own change into the approval
stage, and `scripts/publish-change.sh` refuses to package a filer that names a
transition endpoint. The reason is narrow: if the workflow anywhere permits
Review to reach an approved status directly, a component that can transition
can approve itself, and the record it files proves nothing. Submitting for
approval is cheap for a human, and is the point at which somebody affirms the
change is real.

**Why `issue_state` was added.** Status is what makes `count: 0` readable.
Three different situations produce it —

    status is before the approval step  -> approval is not yet POSSIBLE
    status is the approval step, empty  -> approval is PENDING
    no approval step in the workflow    -> approval is IMPOSSIBLE here

— and reporting any of them as "not approved" is the fourth repetition of
B-042/B-054: an unavailable answer reported as a negative one. The third case
is a finding about the platform's own configuration rather than a denial, and
the verifier must say which it is.

**What is still true and still open:** one group with one member satisfies
Jira's rule and does not satisfy design 03, which requires two approvals from
distinct people with the requester excluded. The quorum verifier enforces that
itself rather than trusting the workflow, so quorum actions will correctly
refuse until Wayne and Art are in the group. Tracked as B-079 — closed
2026-09-26, see there.

### Original diagnosis, kept because it was wrong in an instructive way

The first reading of `{"readable": true, "count": 0}` was "the workflow has no
approval step". That was concluded from an empty list without asking what status
the issue was in — the same mistake the verifier is being built to avoid, made
while building it. An empty result has more than one cause, and the API does not
volunteer which one applies.

## B-079 · One approver is not two-party control

**Closed 2026-09-26.** Jamie added Wayne, Art and Tina to
`AWS Core Infra Approvers`, so the group can now supply two distinct approvers.
Jamie then changed the Authorize rule to require two approvers (same day), so
Jira's rule is no longer the weaker one on count. It still is on *who*: requester
exclusion is not known to be enforced by the workflow, and group membership is
managed in Jira rather than checked against the quorum list. The verifier remains the thing enforcing
design 03. Every change record, ordinary ones included, now needs two people to
approve it. Two things this does not cover on its own:

- Quorum execution reads `/platform/quorum/approvers` in SSM, not the Jira
  group. `config/quorum.env` must list the design 03 approvers' accountIds and
  `scripts/sync-quorum-approvers.sh` must have been run after they were added.
- Tina is a quorum approver: Jamie, 2026-09-26, "she's a peer partner in
  AltDigital, and should be treated as such". design 03 names only {Jamie,
  Wayne, Art}; D-022 records the departure. Her accountId goes in
  `config/quorum.env` as `QUORUM_APPROVER_4`, then re-run the sync.

The change-record note explaining the one-member gap is retired. Records filed
before this date keep it, because editing an approved record breaks design 08
condition 4.

Original entry:

`AWS Core Infra Approvers` had one member. The Authorize rule is one approval
per group, so a single approval from Jamie satisfies Jira.

design 03 requires **two approvals from {Jamie, Wayne, Art}, with requester and
approvers being distinct people**, for `kms:ScheduleKeyDeletion`, backup vault
deletion or lock modification, removal of a member account from the
Organization, and disabling CloudTrail or the Config recorder.

Jira's rule and AltDigital's rule are not the same rule, and today Jira's is the
weaker of the two. This is precisely the case design 08 anticipates when it says
the pipeline **validates rather than trusting the ticket's status field** — Jira
permissions are easy to get subtly wrong, and a workflow satisfied by one person
is exactly that.

So the verifier counts distinct approver accountIds against an authorised list
held in SSM, and enforces requester-exclusion itself. Until Wayne and Art are
added, quorum actions refuse to execute. That is correct behaviour rather than a
defect: the alternative is a two-party control that one person can satisfy.

Ordinary changes are unaffected — they need one approval and have one approver.


With B-077 fixed, approvals are readable. AMI-3, raised as a proper request:

    {"issue": "AMI-3", "readable": true, "count": 0, "raw": []}

Readable, and empty. The `Request a change` workflow in AMI has no approval
step, so no approval can ever be recorded against it.

**Why this blocks 8.3 rather than merely delaying a test.** design 03 puts four
actions behind two approvals from {Jamie, Wayne, Art} — `kms:ScheduleKeyDeletion`,
backup vault deletion or lock modification, account removal from the
Organization, disabling CloudTrail or the Config recorder. design 08 requires
the pipeline to verify five conditions against those approvals at execution
time. With no approval step, there are no approvals, so the pipeline correctly
refuses every request forever. The control is safe but inert.

**What needs configuring in Jira:** an approval step on the `Request a change`
workflow, with an approver set of at least two, and a minimum approval count of
two. Project settings → Workflows → the change workflow → the status that
precedes implementation.

**The distinction the verifier must preserve.** An empty approval list has two
causes:

    no approval step configured   -> approval is IMPOSSIBLE here. A finding
                                     about the platform's own configuration.
    step exists, nobody has acted -> approval is PENDING. A normal state.

Both look like `count: 0`. Reporting the first as "not approved" would be the
fourth repetition of B-042/B-054 — an unreadable or unconfigured source
reported as a negative result — and the consequence here is that a genuine
misconfiguration reads as an ordinary refusal, so nobody fixes it. The verifier
must say which, and an unapprovable request type is an exception record, not a
denial.

## B-080 · An approved change read as unapproved because of a status NAME

**Fixed** — 2026-09-19, found the moment the platform could read its own
project again.

Reconciliation decided whether a change record was approved by matching its
status name against a list in the code:

    approved, done, closed, resolved, in progress, implementing

AMI-4 was approved. Its status was **"Awaiting implementation"**, which is not
in that list, so the record did not count and the deployment it covered was
reported `no_change_record`.

**The consequence is a false accusation, not a missed detection.** An approved
change reading as unapproved means every deployment it covers becomes a finding
against the tenant who did nothing wrong. design 08's whole argument for
continuous reconciliation is that auditors accept evidence over claims — and
the evidence has to be right, because the control's value is that its findings
are trusted.

**Adding the string would have been the wrong fix.** It repairs this workflow
and breaks on the next tenant's, whose statuses this code does not choose and
cannot predict. The list was a guess about words other people pick.

**Fixed by reading the approval RECORD instead**, which is the actual evidence
and does not depend on anybody naming a status the way this code expects:

    JSM approval record        authoritative
    status name heuristic      fallback, and only when there is no record

The fallback still matters and is not dead code. Two cases reach it legitimately:
a record created through the issue API, which cannot carry a JSM approval at all
(AMI-1 and AMI-2 are both this), and a tenant project on plain Jira Software
with no approvals configured. An unreadable approvals endpoint falls back too,
rather than concluding "not approved" — the same rule as everywhere else here.

**Why this was not possible until now.** 8.2 originally used the status field
because approvals were *unreadable*: the records were not JSM requests (B-077),
so the endpoint 404'd. The comment in the code said design 08 only demands
record-level validation for quorum, and that reconciliation findings are
reviewed by a human rather than being irreversible. Both true, and both beside
the point once the better evidence became available.

Findings now carry `approval_evidence`, naming which source decided. A status
guess and a recorded approval are not equally good grounds for accusing a
tenant, and whoever reads the finding should be able to tell which one it
rests on.

## B-081 · Every approved change was reported stale the moment it was approved

**Fixed** — 2026-09-19, found in the same run.

design 08's fourth outcome is "Change record with no corresponding deployment
— Review: stale or abandoned." The implementation reported any approved record
with no matching deployment, with no notion of *when*.

AMI-4 was approved and immediately reported `change_without_deployment` while
the work it authorised was actively in progress.

**This fires on every change record, always.** There is necessarily a gap
between a change being approved and the deployment happening — that gap is the
entire point of approving changes in advance. So the outcome would be raised
for every record in the estate, every run, until each one deployed. A finding
that is always true is noise, and noise is how a control gets muted. B-043 is
the same failure mode from Phase 6 and this is its second appearance.

**Fixed by waiting for the window to close.** A record is stale only once
`created + CHANGE_MATCH_WINDOW_HOURS` has passed with nothing deployed — the
same window used to decide whether a deployment is covered, so the two halves
of the control agree about what "in time" means rather than each having an
opinion.

The regression test asserts the record NAME rather than the count. Asserting
the count alone passes whichever of the two fixtures was reported, which is
precisely the distinction being fixed — the first version of that test did
exactly that and would have passed against the bug.

## B-082 · Internal cache state was appearing in the audit record

**Fixed** — 2026-09-19, found by reading an emitted finding rather than the
code.

The approval lookup cached its answer on the change dict, which is the same
dict serialised into the emitted finding. Every `change_without_deployment`
record therefore carried:

    "_approved": [true, "no approval record readable, fell back to the status
                         name"]

A private tuple, with a leading underscore, in a record retained for audit.
Not wrong, and not something anybody wants to explain in an audit conversation.

Cached in a module-level dict keyed by issue key instead, cleared at the start
of each run. **The clear matters more than the move.** A warm Lambda container
lives for hours and this function runs every six hours, so a cache surviving
between invocations would keep reporting a since-approved change as unapproved
until the container happened to recycle — indistinguishable from the approval
not having worked. That is precisely the cached-auth-header failure in jira.py
that cost a diagnostic cycle earlier the same day, which is why it was worth
looking for.

The evidence string itself was kept, under `approval_evidence`, because which
source answered is genuinely useful to whoever assesses the finding.

## B-083 · Two of design 03's four quorum actions were never declared

**Fixed** — 2026-09-19, found while checking 8.3 would not build a second
alarm path.

design 07 says any attempt against a quorum-controlled action pages
immediately. `containment/10-containment.yaml` and `declare.py` implement that,
and the list of event names they watch was built from design 07's prose:

    Any ScheduleKeyDeletion, CloudTrail disable, or vault lock modification
    attempt.

That is a summary. The enumeration is in design 03, and it has **four** items:

| design 03 action | was declared |
|---|---|
| `kms:ScheduleKeyDeletion` | yes |
| Backup vault deletion or lock modification | yes |
| Disabling CloudTrail **or the Config recorder** | CloudTrail only |
| **Removal of a member account from the Organization** | no |

So removing an account from the Organization — which strips it of every SCP,
every guardrail and the whole platform baseline in one call — produced no
declaration and no page. Neither did stopping the Config recorder, which
silently ends configuration history for an account.

**The mechanism of the error is worth more than the fix.** Two documents
describe the same control at different resolutions, prose and enumeration, and
the implementation was written against the prose. Nothing was wrong in either
document. Where a design summarises elsewhere, the enumeration is the source
and the summary is a reminder that one exists.

Fixed by adding `RemoveAccountFromOrganization`, `StopConfigurationRecorder`
and `DeleteConfigurationRecorder` to both the EventBridge rule and the trigger
map, and by testing all four of design 03's actions by name rather than
testing that "the list works".

## B-084 · Every approved key deletion would have paged as an unexplained one

**Fixed** — 2026-09-19, found in the same read, before it could fire.

`_from_cloudtrail` declares a security incident for any quorum action and
titles it `<Action> attempted by <principal>`. With 8.3 built, the quorum
pipeline becomes a legitimate performer of exactly those actions — so the first
approved key deletion would have paged somebody at 3am with a title
indistinguishable from an attacker deleting a key.

**Not fixed by suppressing it.** design 03 is explicit:

    Any attempt against these actions — successful or denied — pages
    immediately.

An approved deletion is still a deletion, and these are rare enough that a
human should hear about every one. Suppressing the page for actions the
platform itself performed would also mean a compromised pipeline pages for
nothing, which is precisely backwards.

Fixed by making the page *legible*. execute.py sets the assumed-role session
name to `quorum-<issue-key>`, the executor role's trust policy requires that
shape, and CloudTrail therefore carries the authorising change record on the
principal ARN. The declaration reads it back:

    ScheduleKeyDeletion performed by the quorum pipeline under approved
    change AMI-9

versus, for anything else:

    ScheduleKeyDeletion attempted by arn:aws:sts::...:assumed-role/...

Same page, same urgency, different first move for whoever answers it. The
evidence comes from AWS rather than from the pipeline describing its own
behaviour, which is the only reason it is worth anything — a compromised
pipeline would describe itself favourably.

A session name that does not match `quorum-*` is NOT treated as authorised even
when the role name matches, because the trust policy forbids that combination
and seeing it would mean the trust policy had been changed.

## B-085 · This account's Lambda concurrency quota is 10, so nothing can reserve any

**Accepted constraint** — 2026-09-19. Not a defect; recorded because it will be
hit again.

`ReservedConcurrentExecutions: 1` on the quorum executor failed the stack:

    Specified ReservedConcurrentExecutions for function decreases account's
    UnreservedConcurrentExecution below its minimum value of [10]

The Audit account's Lambda concurrency limit is **10** — the new-account
default — and AWS requires at least 10 unreserved. So **no function in this
account can reserve any concurrency at all** until the quota is raised. That
applies to every future Lambda here, not just this one.

**The reservation turned out to be the wrong control anyway**, which is worth
more than the quota note. Reserving 1 would have prevented two destructive
actions running in PARALLEL. It would have done nothing about the case that
actually matters: the same approval being executed TWICE in sequence. Nothing
in the pipeline stopped a state machine execution being started again against
an already-actioned change record, and every replay would verify perfectly
because the approval is genuinely still there.

Replaced with a one-approval-one-execution lock: a deterministic row per
(issue, action, resource) written with a conditional put, so a second attempt
fails the condition and is refused. DynamoDB's conditional write is atomic, so
it also covers the parallel case the reservation would have handled — a
stronger control that does not depend on a service quota.

**Trigger to revisit:** if a quota increase is requested for another reason,
reconsider whether any function here wants a reservation. Nothing currently
does.

## B-086 · The reverse sweep swept nothing and reported a clean estate

**Fixed** — 2026-09-19, found on the first live run.

The first reverse reconciliation run returned:

    accounts_swept: 0   events: 0   outcomes: {}   findings: 0

Which is indistinguishable from "the estate is clean" — on the one control in
this codebase whose entire purpose is to prove a negative.

**Two causes, and both are the same mistake.**

*The account list was read from the wrong account.* `platform_accounts()` read
`/platform/org/account/`, which is the registry — and the registry lives in the
MANAGEMENT account. The sweep runs in Audit, where that path is empty. SSM
parameters are per-account, and nothing about reading an empty path looks like
an error.

*The member role was the wrong role.* `OrganizationAccountAccessRole` trusts
`arn:aws:iam::738815759702:root` — the management account only. A sweep running
from Audit gets AccessDenied against every account in the estate. Fixed with a
dedicated `PlatformQuorumReader` deployed alongside the executor, holding
`cloudtrail:LookupEvents` and nothing else. Separate from the executor
deliberately: the component that checks whether quorum actions were approved
must not be able to perform one.

**What makes this worth a numbered entry is not the two wrong paths.** It is
that the failure mode of both was a confident, quiet, wrong answer. The module
already had an `accounts_unreadable` counter written specifically to keep such
a gap visible — and the gap happened UPSTREAM of it, in deciding what to sweep
at all. A check placed one step too late is not a check.

Now: `platform_accounts()` raises rather than returning empty, the summary
always carries an explicit `swept` flag, and `unreadable_reasons` names the
cause. That last one was added after "6 unreadable" with no reason forced a
trip through the alert stream to diagnose the platform's own control.

Current state is honest: 2 accounts swept, 4 reported unreadable because the
reader role is only deployed to Audit and the sandbox. Rolling it out to the
rest is a StackSet away and is not pretended to have happened.

## B-087 · design 07's "any attempt pages immediately" was inert for every member account

**Fixed** — 2026-09-19. The most serious finding of the phase, and it was found
by accident.

design 03 and design 07 both say it plainly:

    Any attempt against these actions — successful or denied — pages
    immediately. A denied attempt is either confusion or something worse; both
    warrant knowing within minutes.

`containment/10-containment.yaml` implements it: `ProtectedActionDeclarationRule`
listens on the organization bus in Audit for `ScheduleKeyDeletion`, `StopLogging`,
`DeleteBackupVault` and the rest, and declares a security incident for each.

**Nothing put those events on that bus.** `baseline/70-event-forwarding.yaml`
forwards alarms, Config, deployments, dossiers, GuardDuty and Security Hub from
member accounts. It did not forward protected actions. The declaration rule
could only ever have fired for an action taken inside Audit itself — which is
not where tenant keys, vaults, trails or Config recorders live.

So the control that design 03 describes as absolute had never been able to fire
where it matters, and there was no signal of that anywhere: the rule existed,
was ENABLED, and matched nothing.

**How it was found.** A KMS key was deleted in the sandbox as part of testing
the quorum reverse reconciliation. The reverse sweep reported it as
`outside_pipeline`, correctly. The declaration path reported nothing — and the
only reason anybody looked was that the two were expected to agree.

That is the argument for building both directions, demonstrated rather than
asserted. design 08 says the reverse check is "what makes it a control rather
than a procedure"; here it was also what made a *different* control's failure
visible. A single detection path has nothing to disagree with.

**Fixed** with `ProtectedActionForwarding` — a separate rule from
`DeploymentForwarding`, not more event names added to it. They answer to
different designs, they page differently, and a shared pattern means every
future edit to one is an edit to the other: that is how a deployment list
quietly acquires a security event, or how a narrowing for cost quietly drops a
page.

Verified live: the same deletion re-attempted after the fix produced
`kms-key-deletion-attempt`, titled `ScheduleKeyDeletion attempted by
arn:...OrganizationAccountAccessRole/retest`, with `quorum_authorised_by: None`
— correctly not claiming an authorisation it did not have.

**Still open:** the forwarding StackSet is deployed to the sandbox OU only. The
other OUs need the same update before this control is live estate-wide.

## B-088 · Truveon's ingestion endpoint does not exist yet

**Deferred with a trigger** — 2026-09-20. Not blocking 9.1.

Prompt 9.1 says to treat Truveon as "an external endpoint that may be
unavailable". It turns out to be better than that: Truveon is an AltDigital
build on the same workstation, so **both ends of this contract are ours**.

That inverts the usual direction. Rather than discovering an API and adapting to
it, the contract is specified by what design 04's completeness controls
require — written down in
[docs/truveon-ingestion-contract.md](truveon-ingestion-contract.md) for the
Truveon project to implement.

**The two requirements that are easy to drop and expensive to drop:**

*Ingest must be idempotent on `(stream_id, sequence)`.* The sender retries, so a
Truveon that appended on every POST would accumulate duplicates — and the count
reconciliation would then report a mismatch on a stream that is perfectly
intact. The control would be wrong in the direction that costs an audit
conversation rather than the direction that is merely noisy.

*The 200 must mean PERSISTED, not received.* The sender marks its ledger row
delivered on that response, and a delivered row is excluded from gap detection
forever. An acknowledgement meaning "queued" lets Truveon lose a record the
platform has already stopped looking for — which is precisely the silent
failure the whole phase exists to prevent.

**Until it exists,** the sender is deployed with no endpoint configured. It does
NOT mark records delivered in that state: records stay pending, and the pipeline
reports itself as not delivering. A stub that acknowledged locally would make
the pipeline look healthy while sending nowhere, which is design 04's "clean
month" reproduced inside the thing built to prevent it.

**Trigger to revive:** whichever comes first —

- the Truveon project implements `POST /v1/evidence`, at which point set
  `TRUVEON_ENDPOINT` and the buffered backlog drains on its own, or
- a tenant contract requires evidence delivery, which makes it blocking rather
  than deferred.

Nothing needs re-architecting at that point. The endpoint and credential are
configuration, and the buffer means evidence produced in the meantime is
delivered rather than lost — the same property the design asks for against a
Truveon outage, exercised early by Truveon simply not being built yet.

### Update 2026-09-23 — the endpoint existed, and did not match

Truveon built `POST /api/ingest/evidence` on 2026-09-13 against its own
functional assumptions, and it differed from the contract above on path,
envelope, idempotency key and — substantively — auth: per-tenant keys, with the
tenant derived from the key, where the contract specified one shared token.
Neither side had told the other. The sender as it stood would have failed on
its first call.

**Reconciled by adopting Truveon's shape** (Jamie, 2026-09-23). The contract
is rewritten in place; its "Why this document changed" section carries the
reasoning, the short version being that the per-tenant heartbeat already
catches the quiet-expiry failure a shared token was meant to avoid, and a
leaked per-tenant key exposes one tenant rather than all of them.

**Done on this side:** sender speaks the real endpoint with a key per tenant
and names a tenant with no key; reconciliation compares count AND range (it
previously compared count only, despite its docstring) and routes an
unanswerable comparison to the digest rather than Jira; template carries
`TruveonSecretPrefix` and `PIPELINE_ACCOUNT_ID`; client accounts reach the
evidence tenant map (they previously never did — only platform accounts were
read); onboarding creates and links the tenant through
`scripts/truveon-tenant-handoff.sh`.

**LIVE 2026-09-23.** Truveon deployed through `f163af2` after migration 88.
AltDigital's own estate is linked to Truveon's `altdigital` tenant
(`prepare-platform`; key at `platform/truveon/ingest/altdigital-platform`) and
`platform-evidence` delivers to `https://app.truveon.ai`. The buffered backlog
(1,503 records) drained, and the first count reconciliation over a closed
window reported 3 streams, 0 mismatches.

The first live delivery found two Truveon bugs and one here, all fixed the same
day:

- Truveon's chain lock (`pg_advisory_xact_lock`) was taken under autocommit and
  released immediately, so ten concurrent Lambdas collided on
  `UNIQUE (chain_seq)` — 55 of ~250 submissions returned 500. No loss: the
  constraint stopped a fork and the sender retried.
- Two copies of one submission racing each other returned 500 instead of
  duplicate.
- Reconciliation's window ended at *now*, so records inside the in-flight grace
  counted as missing and every run would have raised a mismatch. It now ends
  where the grace begins.

**This item stays open for one thing:** `provisioning/src/verify.py`
`check_evidence` and verification sweep check 1 should call
`GET /api/ingest/evidence/receipt` from the Audit account. Until then both
report *undeterminable*. The first client tenant has the same dependency.

## B-089 · I invented event source names instead of reading them

**Fixed** — 2026-09-20, found on the first live collection run.

The evidence collector subscribed to `altdigital.change` and
`altdigital.quorum`. The codebase publishes `platform.change`,
`platform.security`, `platform.recovery`, `platform.response` and
`platform.exercise`. So reconciliation emitted five real findings, the
collector's rule matched none of them, and the ledger held one row — the
heartbeat, which the pipeline writes itself.

**It looked like it worked.** A heartbeat in the ledger, no errors anywhere,
every function returning success. Exactly the shape design 04 warns about:

> A silently failed pipeline that looks like a clean month is the failure mode
> that destroys an audit.

Caught only because the run was checked against what reconciliation reported
emitting — three findings out, one unrelated row in.

**Two separate mistakes, and the second is the worse one.**

*I guessed at a naming convention rather than reading the emitters.* This is
B-068's rule — every endpoint needs one real response before it is trusted —
applied to the platform's own event bus rather than to somebody else's API.
The bus is the one place it felt safe to assume, being ours.

*I introduced a second convention while doing it.* `quorum/src/reverse.py` and
all three evidence modules published `altdigital.*` into a codebase that had
used `platform.*` since Phase 2. Nothing failed, because the router's
`UnmatchedRule` catches by `anything-but` on detail-type and does not care
about source — B-055's fix absorbing a bug it was not written for. The quorum
findings did route. They were simply named differently from everything else,
and the next person to write a source-matched rule would have found half the
estate missing from it.

Both renamed to `platform.*`.

**What would have caught this earlier.** Nothing in the test suite: the unit
tests stub the bus, so the source string is never compared against a real
emitter. A test asserting that every source in `RECOGNISERS` appears in some
emitter's `put_events` call would have. Not built, because the honest version
of that check is "read the emitters", and the emitters are now read.

The collector already reports unrecognised events rather than dropping them
silently — that is what the `collected: false` branch is for. It did not help
here, because the events never reached the collector at all: the EventBridge
rule filtered them out one layer upstream. **A check inside a component cannot
see what a rule in front of it discarded.** Same shape as B-086, where the
completeness counter sat downstream of the decision about what to sweep.

## B-090 · Three cost reports nobody has asked the data for yet

**Deferred with triggers** — 2026-09-20, raised by Jamie while starting 9.2.

Prompt 9.2 builds one report: the **monthly tenant cost report** — raw AWS
cost, flat fee and percentage uplift shown separately. That is the billing
artefact, and it is in scope.

Three others were proposed, for three different readers. None is specified in
design 10 and none is blocking, but they are not equal in cost or in value.

| Report | Reader | Effort | Why |
|---|---|---|---|
| Cost by account by tier | Tina (CRO) | low | A Cost Explorer view once Cost Categories exist |
| ...by AWS service type | Wayne (CIO/CTO) | low | Same, plus the native service dimension |
| **Shared services cost** | Jamie / Art | **real** | Nothing produces this number today |

**The first two are nearly free and are not free.** Cost Categories give the
dimensions, so the *analysis* needs no engineering — somebody can open Cost
Explorer and group by them. What does not exist is a scheduled, formatted,
delivered artefact. That distinction is worth keeping straight: "the data
supports it" and "there is a report" are different claims, and conflating them
is how a commitment gets made on a call.

**The third one is different, and is the one to build first.** design 10:

> Log Archive, Audit, Forensics, Platform Tooling and Shared Services accounts
> generate real cost that nobody pays a flat fee for. [...] **Position:
> absorbed into the percentage uplift.**
>
> The weakness is that it decouples revenue from effort: a large, quiet
> workload pays a lot for little management; a small, messy one pays little
> while consuming the team.

Absorbing shared cost is a BILLING decision. It is not a reason to be unable to
see the number. Today nothing measures it, so the position "the uplift covers
it" is an assumption nobody can check — and design 10 names the exact way it
could be wrong.

The report worth having is therefore not "what did shared services cost", it is
**shared cost against uplift revenue, over time**. That turns a stated position
into a testable one, and it is the only one of the three that could change a
commercial decision rather than describe one.

**Triggers:**

- *Shared services cost:* before the first partner pricing conversation that
  relies on the uplift covering shared cost. Sooner if the estate grows past a
  handful of accounts, since the absorbed number scales with account count
  while revenue scales with workload.
- *Tina and Wayne's views:* when somebody actually needs one on a recurring
  basis. Until then Cost Explorer answers the question on demand, and a
  scheduled report nobody reads is worse than no report.

Note that the monthly tenant report built in 9.2 is per TENANT. None of these
three is, which is why none of them falls out of it for free.

## B-091 · The guardrail harness contains itself, and then fails its own tests

**Partly fixed** — 2026-09-20. Found while testing policy 04.

`scripts/test-guardrails.sh` proves the SCPs hold by attempting the things they
forbid: `kms:ScheduleKeyDeletion`, `cloudtrail:StopLogging`,
`backup:DeleteBackupVault`. Each attempt is denied, each check passes.

B-087 — fixed an hour earlier the same day — started forwarding protected-action
attempts from member accounts to the organization bus, so that design 07's
"any attempt pages immediately" could work at all. Containment now sees those
attempts and does exactly what it should:

    incident 20260920-53b0bc05aa
    trigger      kms-key-deletion-attempt
    principal    arn:aws:sts::754280127660:assumed-role/GuardrailTestAdmin/gr-test

It isolates the principal. `PlatformIncidentDenyAll` lands on the harness role
**mid-run**, and every check after the KMS one executes as a contained subject.
Sixteen such incidents had accumulated before anybody noticed.

**Nothing here is malfunctioning.** The harness performs actions indistinguishable
from an attack, because that is what a guardrail test IS. Containment reacting
is the control working. The SCPs denied what they should. The forwarding did
what it was fixed to do. Three correct components produce a broken outcome
because none of them knows the others exist.

**How it presented, which is the part worth remembering.** Policy 04's negative
control — "a correctly tagged resource must still be creatable" — failed, and
the harness reported:

> correctly tagged creation was BLOCKED — policy 04 is over-broad and would
> stop every tenant deployment

That diagnosis was confident, specific, actionable, and wrong. The real cause
was visible only in the raw error, which the harness discards:

    ...with an explicit deny in an IDENTITY-BASED policy

AWS says "service control policy" when an SCP denies. It said identity-based,
and the harness was not reading that word. A test that explains its own failures
is worth having; one that explains them wrongly costs more than one that stays
silent, because the explanation gets believed.

**Fixed for now** by having the harness clear any containment from its own
subject roles before it runs, and report loudly when it finds some — a contained
subject means the previous run left one behind, and that is worth knowing rather
than silently repairing.

**Not fixed, and the real question:** the harness should declare itself an
exercise so containment can mark the incident and skip isolation. Phase 7.4
built an exercise path for containment; the guardrail harness predates it and
does not use it. Until it does, every guardrail run writes real-looking
incidents into the security incident table, which is evidence pollution in the
store design 04 wants auditors reading.

**Trigger:** before the guardrail harness runs on any schedule, or before a
tenant account is vested — whichever first. A harness run by hand occasionally
is tolerable; one running nightly would bury real incidents under its own.

## B-092 · The pre-commit policy validator validated nothing and said all was well

**Fixed** — 2026-09-20, found in the output of an unrelated commit.

`scripts/validate-policies.sh` derives a policy's type from its directory:

```bash
case "${f}" in
  */policies/scp/*) ptype=SERVICE_CONTROL_POLICY ;;
```

Run with no arguments it globs absolute paths and works. The **pre-commit hook**
passes staged files, which are repo-relative — `policies/scp/04-require-tags.json`
— and `*/policies/` requires a path segment before `policies`. Every staged
policy fell through to the skip branch:

```
warn  04-require-tags.json: cannot infer policy type from path — skipped
  ok  all policy documents valid
```

Both lines are from the same run. The hook validated nothing and reported
success.

**How long it had been wrong is unknowable and probably always.** Policies
00–03 were committed before the hook existed or in commits that also ran the
full sweep, so nothing ever surfaced it. It took adding the first new policy in
months for the warning to appear next to a file somebody was looking at.

**The failure mode is the session's recurring one**, now in a fourth place:

| | reported | actually did |
|---|---|---|
| B-086 | clean estate | swept zero accounts |
| B-089 | pipeline healthy | collected zero events |
| B-091 | policy 04 over-broad | subject was contained |
| B-092 | all policies valid | validated zero policies |

Every one of them is a component that cannot distinguish "checked and fine"
from "did not check". The pattern is now frequent enough to be worth stating as
a rule rather than a list of incidents:

> **A check that can pass without doing anything must report what it examined,
> not only what it concluded.**

`validate-policies.sh` already prints a line per file, which is why this was
findable at all — the absence of `04` from a list of `ok` lines was visible.
The scripts that failed silently were the ones reporting only a verdict.

Fixed with `*policies/scp/*`. The skip branch stays: a genuinely unknown path
should still be refused rather than guessed at.

## B-093 · I wrote the same bug an hour after documenting it four times

**Fixed** — 2026-09-20, found on the first live run of the cost report.

`cost/src/report.py` read the pricing rates like this:

```python
except Exception as exc:
    raise RatesNotSet(f"could not read {RATES_PARAM}: ... Run
                        scripts/set-cost-rates.sh.")
```

Every failure became "nobody has set the pricing". The first live run produced:

    refused because: could not read /platform/cost/rates: TokenRetrievalError.
    design 10's $1,000 and 15-20% are INDICATIVE ... Run set-cost-rates.sh.

An expired SSO token, reported as a pricing problem, with confident and
specific instructions to go and fix the wrong thing.

**Written in the same session as B-086, B-089, B-091 and B-092**, all of which
are the same fault, and after the rule had been written down:

> A check that can pass without doing anything must report what it examined,
> not only what it concluded.

That rule turns out to be the narrow version. This case did not pass — it
failed, correctly, and stopped the report. The outcome was safe. What was wrong
was the DIAGNOSIS, and nothing about a safe outcome protects against that.

**The wider rule:**

> Where two causes lead to the same action, the action being right does not
> make the explanation right. Distinguish them at the point where they are
> still distinguishable.

`ParameterNotFound` and `AccessDenied` arrive as different error codes and are
collapsed by a bare `except Exception`. The information was present and thrown
away, which is the same move B-091 made when the harness discarded the word
"identity-based".

**Fixed** with two exception types — `RatesNotSet` and `RatesUnreadable` — and
a `blocked_by` field in the output, so the report says which of the two stopped
it. Both still refuse to produce a report.

**Worth noting about the test.** The original test stubbed
`RuntimeError("ParameterNotFound")`, an exception with no `.response`, so both
paths looked identical to it and it could never have caught this. A fixture
that does not have the shape of the real thing tests the fixture. The test now
raises a botocore-shaped error carrying a real error code, and asserts both
branches by name.

## B-094 · An empty billing report had three causes and reported one

**Fixed** — 2026-09-20, caught before it misled anybody, which is the only
reason it is worth a separate entry from B-093.

The first live run of the cost report returned `tenants: 0`. That is defensible
— every account in the estate is `kind=shared`, so there are no billable
tenants — but it is indistinguishable from two other things, and the report
said nothing about which:

| `groups_seen == 0` because | a problem? |
|---|---|
| nothing was spent in the period | no |
| the Cost Categories have not backfilled (AWS takes up to 24h) | no, wait |
| spend exists and categorisation returns nothing | **yes** |

The categories had been created minutes earlier, so the second was the obvious
suspect — and adding a warning for it was the first fix. It was wrong. Querying
the period ungrouped showed August cost **zero**: the estate did not exist yet.
The report would have warned about a backfill delay for a month in which there
was nothing to backfill.

**Separated by asking what the period cost without any grouping.** Ungrouped
zero means nothing was spent. Ungrouped non-zero with no groups means the
categorisation is not accounting for real money. A third branch covers the
ungrouped call itself failing, because "unknown" must not collapse into
"nothing" — that is the distinction the whole check exists to make.

Verified live against both: August reports no spend, September reports one cost
group, none billable, every account shared. Which is correct, and now says so.

**Why this one is filed as a near-miss rather than a defect.** It was noticed
by asking "how would this look if the thing I just built were broken?" rather
than by the report failing. That question is what the previous five entries
cost, and it is cheaper to ask than to learn again:

| | reported | actually |
|---|---|---|
| B-086 | clean estate | swept zero accounts |
| B-089 | pipeline healthy | collected zero events |
| B-091 | policy over-broad | subject was contained |
| B-092 | all policies valid | validated zero policies |
| B-093 | rates not set | credentials expired |
| B-094 | no tenants | *three different things* |

## B-095 · Environment variable keys need a platform-mediated view

**Deferred with a trigger** — 2026-09-20. See D-017.

design 11 grants developers environment variable KEYS in production and denies
VALUES. ECS returns both from `DescribeTaskDefinition` and offers no way to
separate them, so the call is denied and keys are not available.

**What would close it:** a read-only Lambda holding `ecs:DescribeTaskDefinition`,
returning container definitions with every `environment[].value` and every
`secrets[].valueFrom` stripped, invokable by the developer permission set.

Small, and not free. It is a new component with a credential developers do not
have, which makes it a thing to secure, test, and reason about during an
incident. The failure mode if it is wrong is that it returns the values it
exists to hide.

**Trigger:** a developer asks for it, or an incident review names missing env
var keys as a contributing factor. Not on the assumption that they will —
design 11's own argument for the wider production grant is about the failure
modes it names specifically (OOM kills, evicted tasks, non-zero exits), and env
var keys are not among them.

Worth noting the asymmetry that makes this deferrable: `ssm:DescribeParameters`
and `secretsmanager:ListSecrets` DO separate names from values cleanly, so
parameter names and secret names are already granted. ECS is the only one of
the three that needs mediating.

## B-096 · Every containment failed at the readout, and paged

**Fixed** — 2026-09-20, found by Jamie asking why each batch pages him.

`containment/10-containment.yaml` gives the state machine role:

```yaml
Action: 'lambda:InvokeFunction'
Resource: !GetAtt ContainmentFunction.Arn
```

The state machine invokes two functions. `DefinitionSubstitutions` lists both
`ContainArn` and `ReadoutArn`; the IAM policy lists one. So every containment
contained correctly, recorded its stages, and then died:

    User: .../PlatformContainmentStateMachine/... is not authorized to perform:
    lambda:InvokeFunction on resource: .../platform-incident-readout

The execution fails, `platform-containment-execution-failed` crosses its
threshold, and that alarm publishes **straight to the platform-alerts SNS
topic**, which has a PagerDuty subscription. It never goes near the router —
which is why the router's own logs showed zero PagerDuty routings while Jamie's
phone said otherwise.

**Present since Phase 7 and silent until 2026-09-20.** Nothing was triggering
containment. B-087 started forwarding protected-action attempts to the
organization bus, the guardrail harness attempts exactly those actions to prove
the SCPs deny them (B-091), and containment suddenly had work to do. Two
correct changes exposed a third thing that had been broken all along.

**What it cost beyond the paging.** Every incident record since Phase 7 has its
containment stages and no readout, so design 07's cross-account indicator
question is UNANSWERED on all of them. The failure message said so each time,
into an execution history nobody was reading.

**The class of bug.** A state machine's `DefinitionSubstitutions` and its role's
IAM policy are two lists of the same functions, in the same file, forty lines
apart, and nothing compares them. The quorum state machine happens to have both
entries — by attention, not by construction.

A publish-time guard could check that every ARN a state machine substitutes
appears in its role's invoke policy. Worth doing when a third state machine
exists; two is not yet a pattern, and B-060's lesson was about deriving the list
rather than maintaining a second copy, which is the same shape.

**Two paging paths, and only one goes through the router.** Worth stating
plainly because it is not obvious from the code: alarms wired directly to the
alerts topic bypass `alerting/src/router.py` entirely, so none of the tier
logic, the downgrade-only rule or B-073's account-tier check applies to them.
That is correct for platform-health alarms — the platform's own failures are
not a tenant's and have no tier — but it means "the router never pages for dev"
is not the same statement as "nothing pages for dev".

## B-097 · Two paths page, and only one of them is governed

**Deferred, to be revisited.** Raised by Jamie 2026-09-20 after B-096.

Diagnosing why each build batch paged him turned up something structural rather
than a defect: **there are two independent ways to reach PagerDuty, and the
platform's routing rules apply to only one of them.**

```
path 1   finding ──> platform-alerts bus ──> router ──> SNS ──> PagerDuty
path 2   CloudWatch alarm ─────────────────────────────> SNS ──> PagerDuty
```

Path 1 is `alerting/src/router.py`. Everything the platform has learned about
not waking people up lives there: design 06a's five routing classes, the
downgrade-only rule (an emitter may lower its own urgency, never raise it), and
B-073's account-tier check — the fix made after a dev-sandbox finding raised a
real incident.

Path 2 is a CloudWatch alarm with the alerts topic as an alarm action. It
reaches the same pager having consulted none of that.

**This is how B-096 hid.** The router's logs showed zero PagerDuty routings
while the phone said otherwise, and both were telling the truth.

### Why path 2 is not simply wrong

Platform-health alarms are AltDigital's own failures. They have no tenant, no
environment tier, and nothing to downgrade — a containment state machine that
cannot invoke its readout is broken at 3am regardless of whose account it was
contained in. Forcing those through a tenant-shaped router would mean inventing
a tier for them.

So the split is defensible. What is not defensible is that it is **invisible**:
nothing in the codebase says which alarms take which path, and "the router
never pages for dev" reads as "nothing pages for dev" until somebody's phone
disagrees.

### What to decide when this is picked up

1. **Which alarms legitimately bypass the router?** Probably: platform
   component health, DLQ depth, credential expiry. Probably not: anything
   carrying an account id, because that has a tier and the tier should decide.
2. **Should path 2 be enumerated somewhere readable**, the way
   `policies/scp/attachments.tsv` enumerates policy attachment? The current
   answer is recoverable only by reading every template's `AlarmActions`.
3. **Does the downgrade-only rule want a counterpart?** Path 2 alarms cannot
   declare a routing class at all. An alarm that wanted to be a digest line has
   no way to say so and must either page or be silent.

### Not urgent, and not nothing

Nothing is currently mis-paging: B-096 is fixed and the DLQ is drained. The
risk is the next platform alarm somebody adds, which will inherit path 2 by
default — because wiring an alarm action is the obvious thing to do and
routing through the bus is the thing you have to know about.

**Trigger:** the next time a platform-health alarm is added, or before the
on-call rota covers anybody but Jamie. A second person on the rota is the point
at which "I know which alarms mean what" stops being a workable answer.

## B-098 · The policy deploy script never deployed an RCP, and its header said it did

**Fixed** — 2026-09-20, found by writing the first RCP.

`scripts/deploy-policies.sh` opens with:

> Reads `policies/scp/*.json` and `policies/rcp/*.json`, and optionally attach
> them to OUs.

The glob was `find "${REPO_ROOT}/policies/scp"` only. RCPs were never read, never
created, never attached — and the script reported success, because it had
faithfully deployed everything it looked at.

`create-policy` also hardcoded `--type SERVICE_CONTROL_POLICY`, so an RCP that
did reach it would have been created as the wrong policy type: attached,
visible in the console, governing nothing.

**Third instance of the same shape today**, and the clearest statement of it:

| | claimed | did |
|---|---|---|
| B-092 | all policies valid | validated zero |
| B-098 | deploys scp and rcp | deployed scp |

> A script that reports on what it PROCESSED, rather than on what it was ASKED
> to process, cannot tell "nothing to do" from "did not look".

Fixed with both directories globbed and the type derived from the path, the
same way `validate-policies.sh` does it. The header is now true.

**What made it findable.** Nothing in the run said "0 RCPs". The gap surfaced
only because the RCP was expected to appear in the output and did not —
somebody comparing an intention against a result. That is the same thing that
caught B-089, and it is not a check, it is a habit.

## B-099 · design 11's discriminator table has two identical rows

**Open** — a documentation gap, not a defect. Found implementing prompt 10.3.

design 11's table for discriminating telemetry loss:

| Scenario | Status checks | Flow logs | SSM ping | CW telemetry |
|---|---|---|---|---|
| Network or IAM path broken | Pass | Traffic | Lost | Stops |
| **Both agents killed** | **Pass** | **Traffic** | **Lost** | **Stops** |

Identical across all four signals. The prompt says to implement the table
exactly and that other scenarios must not produce the alarm — which is
followed — but these two are not separable by the signals the table lists.

**Not a problem for the alarm, and a problem for the readout.** Both are a real
loss of visibility on an instance that is provably alive and passing traffic,
so raising a finding is correct either way. What would be wrong is naming one
cause. The finding says both and marks the cause undetermined.

**What would separate them,** if it turns out to matter: a broken IAM path
leaves the instance reachable by SSM's *hybrid* channel or shows
`AccessDenied` in its association history, whereas a killed agent leaves the
process absent with the role intact. Both need a signal the table does not
include, and neither is worth building before somebody has actually had the
ambiguity cost them time.

**Suggested for design 11:** either add a fifth column that separates them, or
merge the two rows and say the cause is undetermined at this level. The current
form implies a discrimination the signals cannot make, and somebody
implementing it will either notice — costing an hour — or not, and write a
detector that asserts a cause it cannot know.

## B-100 · The traffic signal read a log group that does not exist

**Fixed before deployment** — 2026-09-21, found while writing the stack.

`gap.py`'s first `_recent_traffic` read VPC flow logs from a CloudWatch log
group. On this platform flow logs go to **S3** —
`baseline/40-network.yaml`, `LogDestinationType: s3` — so there is no log group
to read.

Every instance would have marked `flow_logs` unreadable, `classify()` correctly
refuses to judge when a signal is unreadable, and therefore **no instance would
ever have matched a row**. The detector would have run on schedule, reported
`findings: 0`, and been inert.

The failure it exists to catch, performed by itself: a scan that examines
everything and concludes nothing, indistinguishable from a healthy estate.

**Fixed by using `NetworkPacketsIn` from `AWS/EC2`.** The substitution is
deliberate, not convenient. design 11 names flow logs, and the property its
table actually relies on is the one in the signal-dependency table above it —
flow logs **do not require the agent**. `NetworkPacketsIn` is hypervisor-published
and shares that property exactly, while being one CloudWatch call rather than
an Athena query over S3.

**Found by writing the deployment**, not by a test. The unit tests construct
`Signals` directly and never exercise `gather()`, so they passed throughout and
would have kept passing. What surfaced it was asking where the flow logs
actually are before wiring the IAM policy — a question the test never asks.

## B-101 · Inbound port 22 detection needs Athena over S3 flow logs

**Deferred with a trigger** — 2026-09-21.

design 11 and prompt 10.3 both ask for it:

> There is no SSH path in this architecture. Inbound on 22 appearing in flow
> logs is a finding in its own right, independent of the telemetry question,
> and is alarmed directly.

Not built. Flow logs land in S3, so this needs an Athena table over the flow
log bucket and a scheduled query — a Glue catalog, partition management, a
workgroup and a result bucket. Real work, and none of it shared with anything
built so far.

**What exists meanwhile, and why it is not a substitute.** `cfn-guard`'s
`SG_NO_OPEN_INGRESS_EXCEPT_443` refuses a security group opening any port but
443 to `0.0.0.0/0`, which prevents the common way the door gets opened. That is
a preventive control on the *configuration*. design 11 asks for a detective
control on the *traffic*, and the two answer different questions: the guard
catches somebody writing a template, and the flow log catches somebody
arriving through a path nobody templated.

Stated plainly rather than quietly treated as covered — a preventive control
described as if it were the detective one is exactly the substitution D-017
refused.

**Trigger:** when an Athena workgroup exists for any other purpose (cost
analysis over CUR is the likely first), since most of the cost here is the
first query rather than the tenth. Sooner if a tenant requires SSH-path
evidence for an audit.

## B-102 · Prompt 11.1 is mostly already built, and one part of it is not

**Noted** — 2026-09-21, before starting Phase 11.

Prompt 11.1 asks for five things. Three exist:

| Asked for | State |
|---|---|
| `CommercialReadOnly` permission set | Built — `identity/00-commercial-readonly.yaml` |
| Monthly per-tenant cost report (raw, flat fee, uplift, separately) | Built in 9.2 — `cost/src/report.py` |
| Delegated Cost Explorer access | Partly: Cost Categories exist in the payer; delegation does not |
| CUR export to S3 in a dedicated reporting account | **Not built** |
| Portfolio view by service model | **Not built** |

The cost report is word-for-word the same deliverable as prompt 9.2's last
line. Built once, and this records that so nobody builds it twice or assumes
the second mention means something different.

**What is genuinely outstanding:**

*CUR export to a dedicated reporting account.* design 12 puts commercial access
in its own account precisely so that billing visibility needs no member account
access at all — "this grant includes NO member account access of any kind". The
CUR is what makes that possible; without it, answering a cost question means
reaching somewhere it should not.

*Portfolio view by service model.* design 12's reporting table marks it
**AltDigital only** — all partners and clients, by service model, with margin
analysis. It is the report that would answer whether the percentage uplift
actually covers shared cost, which is the same question B-090 is about. The two
should be built together or the second will duplicate the first.

**Trigger:** before the first partner invoice, since the per-partner roll-up
design 12 lists is AltDigital's own invoicing basis and nothing produces it yet.

## B-103 · The CloudWatch agent is installed and never configured

**Open** — a baseline gap, found while deploying prompt 10.3. Two controls
depend on it.

`baseline/50-instrumentation.yaml` installs the agent:

```yaml
Name: AWS-ConfigureAWSPackage
Parameters:
  action: ['Install']
  name: ['AmazonCloudWatchAgent']
```

That puts the binary on disk. It does not write a configuration and does not
start it, so **the agent publishes nothing** — no `mem_used_percent`, no disk
metrics, no guest logs. Nothing else in the repository configures it; the
search for `AmazonCloudWatch-ManageAgent` and an agent config document returns
nothing.

### What depends on it

**Prompt 4.1's memory and disk alarms.** The prompt's own justification was
"memory and disk alarms are useless without it", and the template comment
directly above the association says an alarm on `mem_used_percent` with no
agent "sits in INSUFFICIENT_DATA indefinitely, which in a console full of green
looks near enough to healthy that nobody investigates". Installing the binary
does not change that — the metric still never arrives.

**Prompt 10.3's telemetry gap detector**, which uses `mem_used_percent` as its
sentinel precisely because it exists only when the agent runs. With no
configuration the sentinel is absent on every instance, and the detector would
have reported a killed agent on every healthy instance in the estate. The
loudest possible way to be useless.

### What was done about it in 10.3

Not worked around. The detector gained a row design 11's table does not have:
an instance that has **never** published the sentinel does not have a killed
agent, it has an agent that was never started. Absence only means something
stopped if something was previously going.

That is also what design 11 actually asked for — "absence is only detectable
against a declared expectation" — and the first implementation had the
expectation wrong. It used "is tagged `platform-managed`", which is a statement
about intent. The expectation that works is "has published before", which is a
statement about observed fact.

So the detector is correct either way, and reports the never-published case as
a configuration gap pointing here rather than as an incident.

### The fix

A second SSM association running `AmazonCloudWatch-ManageAgent` with
`action: configure` and a config document in Parameter Store, listing at
minimum `mem_used_percent` and `disk_used_percent`.

Deliberately not bolted on during 10.3. It belongs to the instrumentation layer,
it wants deciding alongside which guest metrics the baseline actually alarms on,
and doing it here would have buried a baseline change inside a detector commit.

**Trigger:** before any memory or disk alarm is created, since those are
inert until it exists — or before the telemetry detector is relied on to
distinguish a killed agent from one that never ran, since today it can only say
which by pointing at this entry.

## B-104 · A publish script was copied and half-renamed, and uploaded under the wrong name

**Fixed** — 2026-09-21, noticed in the script's own output.

`scripts/publish-commercial.sh` was built from `publish-telemetry.sh`, which was
built from `publish-quorum.sh`. The rename was applied to the script's head and
not its tail, so the commercial package uploaded as:

    s3://.../telemetry/telemetry-2bb45bee1d7e5170.zip

containing `route.py`. The trailing guidance was worse — two generations stale,
telling the reader to deploy an executor role and sync an approver list, neither
of which exists in this phase.

**Nothing broke**, which is the point. The upload succeeded, the key was
content-addressed and collided with nothing, and only the *name* was wrong.
Somebody later deploying the telemetry stack from a key list would have had a
one-in-three chance of shipping the commercial router into it.

**Caught by reading the output rather than by any check.** The script printed
"Commercial package" and then a telemetry key on the next line, which is only
visible to somebody looking. Third publish script in this codebase built by
copying the last one, and the first two carried their ancestry harmlessly.

**Worth a guard when there is a fourth**, not before: assert that the upload
key's prefix matches the script's own name. Two copies is coincidence; four is
a pattern, and a guard written now would be guessing at what the pattern is.

The stray object has been deleted from the artifact bucket.

## B-105 · The saga is not self-cleaning yet, and that is three of four

**Open** — a known gap in prompt 12.1's own requirements, stated rather than
implied.

The prompt names four properties. Three are built and tested:

| Property | State |
|---|---|
| Idempotent | every step checks before acting; re-runs are no-ops |
| Resumable | resumption reads the registry, not an execution history |
| Fails closed | uncertainty drives the record to `blocked`, never `provisioned` |
| **Self-cleaning** | **not built — the deprovisioning path is prompt 12.4** |

design 13: "failed provisioning is unwound by the deprovisioning path", and
"Deprovisioning — built at the same time". It was not built at the same time.

**What that means in practice today.** A saga that halts at step 5 having
created a Truveon tenant, a PagerDuty service and a Jira project leaves all
three, and the registry record is the only list of them. Unwinding is a human
reading that record and deleting three things by hand.

**Why that is tolerable now and will not stay so.** No tenant exists, so
nothing has been half-provisioned. The moment one is attempted, the unwind path
matters more than any further step implementation — because the failure it
handles is the likely outcome of a first run, not the exceptional one.

**What the saga already does to make it possible.** Every step writes its
identifiers back to the registry immediately rather than at the end, precisely
so that a half-finished provisioning leaves a complete list of what exists.
That was built for this, and it is the only reason a manual unwind is feasible
at all.

**Trigger:** before the first real tenant is attempted. Not before the
remaining step implementations — an unwind path matters more than another step,
because a step that fails without one leaves somebody deleting resources from
memory.

## B-106 · Seven of the saga's ten steps have no implementation

**Open, and deliberate** — the engine is prompt 12.1; the steps are 12.2
onward and later phases.

Steps 2 through 8 raise `StepNotImplemented`, which blocks. That is the correct
behaviour and not a placeholder: a step treated as a no-op would produce a
tenant that finished provisioning without a Truveon stream, a Jira project or a
verification sweep — the "looks supported" failure the saga exists to prevent.

| Step | Waiting on |
|---|---|
| 2 Truveon tenant | B-088, the ingestion API |
| 3 PagerDuty services | prompt 12.2 |
| 4 Jira project | prompt 12.3 |
| 5 AWS accounts + baseline | phase 3's vesting pipeline, which exists — needs wiring |
| 6 Instrumentation and schedules | phase 4 exists — needs per-tenant invocation |
| 7 Cost Categories and budgets | categories exist (9.2); budgets do not |
| 8 Verification sweep | design 13's own section, unbuilt |

Steps 5, 6 and 7 are the closest: the machinery exists and what is missing is
the per-tenant invocation rather than the capability. Step 8 is the one worth
doing early regardless of the others, because it is what would catch the
previous seven having gone wrong.

## B-107 · Exit refuses, because the data extraction path does not exist

**Open, and blocking exit by design** — 2026-09-21.

prompt 12.6 requires two things before a contractual exit removes anything:

> The data extraction path must be real and tested — application data, tenant
> logs from the archive, and the Truveon tenant if retained. Verify nothing of
> AltDigital's is entangled in the member account requiring surgical removal.

Neither is built. `deprovision.py` therefore **refuses exit** rather than
proceeding, and the refusal names both.

**Why refusing is the only option.** Exit removes accounts. An exit that
removed them and then discovered the extraction was not built is unrecoverable,
and design 10 makes exit a contractual commitment — so the failure would be a
breach rather than an inconvenience. `extract()` returning "nothing to extract"
would be the single most expensive no-op in this codebase, and it is exactly
the shape a placeholder takes.

The entanglement check is the quieter half and fails the same way. Returning an
empty list of concerns reads as "clean" and means "did not look"; a customer
taking over an account would inherit whatever was missed, including roles still
assumable from AltDigital's accounts.

**What works today.** `dormant` and `cleanup` are complete as designs and
blocked only by the provisioning steps they undo (B-106). Exit is blocked by
this entry as well, which is the right order: the two variants design 13 says
are "needed sooner than exit" are the two that are closer.

**Trigger:** before any exit conversation, and before the first tenant contract
is signed — design 10 lists a tested exit path as a commitment, so signing
without one commits to something that does not exist.

## B-108 · A permission probe answered a different question than the one asked

**Closed by re-probing** — 2026-09-21.

Prompt 12.3 says to verify current API capability before building, because this
area has been changing. So it was verified — against
`/rest/api/3/mypermissions` with no project scope. That returned:

```
ADMINISTER           False     <- cannot create projects
ADMINISTER_PROJECTS  True      <- so a component will work
```

which led to recommending, and the user selecting, a components-in-one-project
model. Scoped to the ops project, the same permission reads **False**, and
component creation returns `You cannot edit the configuration of this project`.

**The failure is not the missing permission, it is the shape of the check.**
`mypermissions` without `projectKey` answers "does this account hold this
permission anywhere", and the question being asked was "can it act on AMI".
Both answers are true statements; only one of them was relevant, and the
irrelevant one was affirmative.

This is the same family as B-086, B-092 and B-100, but a distinct variant worth
naming. Those reported a conclusion without having examined anything. This one
examined something real, and reported the answer to a question nobody asked.

> A check must be scoped to the thing the decision is about. A permission held
> somewhere is not a permission held here, and an affirmative answer to the
> wrong question is more expensive than no answer at all.

The cost was one design decision made on a false premise, caught before code
was written because the first thing built was a capability probe rather than a
feature. That ordering is worth keeping.

## B-109 · Tenant isolation in Jira is a naming convention, not a boundary

**Open, accepted for now** — 2026-09-21.

design 13 specifies "Project **or component**, inheriting the shared
configuration scheme". Neither is reachable by the platform credential
(B-108), so `jira_tenant.py` separates tenants with a label:
`tenant-<registry-id>`.

What that costs, stated plainly:

| | separate config | own field and filters | confidentiality boundary |
|---|---|---|---|
| project | yes | yes | **yes** |
| component | no | yes | no |
| label | no | no | no |

The label gives no confidentiality boundary whatsoever. Anyone who can browse
the ops project sees every tenant's onboarding tasks, obligations and owners.
Nothing in the code prevents this, and nothing in the code can — it is a
property of where the issues live.

**Why it is acceptable today.** The only people in the tool are the four
partners, all of whom are entitled to see every tenant. The isolation that
matters right now is operational — finding one tenant's work among another's —
and a label does that adequately.

### Corrected 2026-09-21: this entry conflated two populations

The original trigger read "the first time a person who is not an AltDigital
partner is given access to the ops project." That is too broad, and it treats
customers and agents as the same event when Jira does not.

| | how they reach Jira | what they see | isolated by |
|---|---|---|---|
| **Customer** (portal, unlicensed) | portal only, never browses | own requests, plus what is shared with their organization | **JSM Organization** |
| **Agent** (licensed) | browses the project | everything in the project | issue security level, or a separate project |

**Labels can never do this.** A label carries no ACL in Jira and there is no
mechanism to attach one. That is not a gap in this implementation — labels are
metadata with no security semantics at all, so "restrict by label" is not a
thing that can be built.

**What was found to work.** The platform credential CAN create JSM
organizations and attach them to the service desk — verified live, no admin
permission needed. An organization is *data*, not configuration, which is
exactly why it escapes the prompt 12.3 problem: creating one per tenant adds
no per-tenant workflow, screen, field or scheme. (It cannot *delete* them —
403 — which is a containment property rather than a limitation.)

**What was found not to work, and why it is a trap.** AMI does have an issue
security scheme attached, with two levels (`Sensitive Cases`, `Service Desk
Team`), and the credential holds `SET_ISSUE_SECURITY`. So per-issue visibility
is technically reachable. But a *per-tenant* level has to be created by an
admin inside the scheme, per tenant — which is per-tenant configuration drift,
the precise thing prompt 12.3 forbids automating. It does not avoid the trap,
it relocates it from workflows to security schemes.

**Revised trigger:** a person from a client who needs to work tickets as a
licensed **agent** in the ops project. Customers do not trigger this — they
never browse, and organizations bound them properly. Only the agent case forces
a project per tenant, and with it a credential holding site ADMINISTER.

## B-110 · Provisioning has no deployment, and its two dependencies live in different accounts

**RESOLVED** — 2026-09-21. See the resolution section below; the original
statement of the problem is kept because the reasoning that rejected the
cross-account option is still the reason the current arrangement looks odd.

`provisioning/` contains `saga.py`, `deprovision.py` and `jira_tenant.py` and
**no CloudFormation template**. Nothing is deployed; every test so far has run
locally against live backends.

Running it live surfaced the reason that matters. Step 4 needs two things:

| dependency | account | placed by |
|---|---|---|
| `platform-registry` table | tooling `751479507989` | `registry/10-registry-table.yaml` |
| `platform/jira/service-account` | audit `692869638531` | `scripts/set-jira-token.sh` |

Step 4 run with tooling credentials blocked with `could not read
platform/jira/service-account: ResourceNotFoundException`, which is the correct
behaviour — it wrote nothing and said why — but it means the saga cannot
execute end to end from either account as things stand.

**This is a placement decision, not a bug.** Three options, and one of them
turned out to be unavailable:

1. **Copy the credential into tooling.** `set-jira-token.sh --account` already
   takes the argument. Broadens the blast radius to a second account, and
   creates two copies to rotate — a rotation that updates one and not the
   other gives exactly the stale-but-passing failure this codebase keeps
   finding.
2. ~~**Cross-account read** of the audit secret from a tooling role.~~
   **Not possible as things stand.** `describe-secret` returns
   `KmsKeyId: None`, meaning the secret is encrypted under the AWS-managed
   `aws/secretsmanager` key, and AWS-managed keys cannot be shared across
   accounts. This option requires re-creating the secret under a
   customer-managed key first.
3. **Move the registry** to audit. Largest change, and it puts the platform's
   spine in the account whose job is evidence.

### Resolved 2026-09-21 — option 1, with the rotation objection engineered out

Authorised by the user. `set-jira-token.sh` now writes **every** consuming
account in a single run, and the list lives in the script rather than in a
flag:

    CONSUMER_KEYS="altdig-security-audit altdig-infra-tooling"

There is no supported way to rotate one and not the other. `--account` still
exists and now overrides the whole list rather than one entry, so narrowing to
a single account is something that has to be done on purpose.

Three properties worth keeping:

- **Every account resolves before anything is written.** An alias that does
  not resolve stops the run, rather than being discovered after the first
  account has already been rotated.
- **Verification is a read-back, not the absence of an error.** Each account's
  stored value is hashed and compared; twelve hex characters of sha256 lets
  two accounts be compared without either value being displayed.
- **A partial rotation names both halves.** "Rotation failed" would leave
  somebody guessing which half of the estate is stale, so the failure path
  names the accounts that took the new value *and* the ones that did not, and
  says not to revoke the old token until they agree.

A `--propagate` mode was added for the case in hand: the credential already
existed in audit and tooling was missing it. It fans out the value already
stored rather than prompting, because re-typing a token to copy it is how a
typo becomes a rotation nobody intended.

**Verified:** both accounts report fingerprint `e01ee0a9fce8`, and saga step 4
now runs end to end from tooling — registry record stamped with the tenant
label, onboarding key and organization id. It previously blocked on
`ResourceNotFoundException` for the secret.

Still worth Art ratifying as a security decision after the fact; the blast
radius is now tooling as well as audit.

## B-111 · Two JSM organizations left behind by capability probes

**Open, needs a Jira admin** — 2026-09-21.

Probing whether the platform credential could create JSM organizations (it
can — see B-109) left two behind:

| id | name | state |
|---|---|---|
| 1 | `zz-probe-org` | detached from the service desk |
| 2 | `tenant-zz-probe` | attached to service desk 2 |

The credential **cannot delete them**: `DELETE /rest/servicedeskapi/organization/{id}`
returns 403, *"You'll need additional permission to delete organizations"*.
That is the correct shape — provisioning creates a customer boundary and
cannot dissolve one — but it means cleanup is a human action.

Neither contains a customer, so neither exposes anything. They are untidy
rather than harmful, which is why the user has deferred the cleanup.

**Trigger:** before the first real tenant is onboarded, so that the
organization list reads as one entry per client with nothing else in it. An
organization list with test entries in it is the kind of thing that gets a
real customer attached to the wrong one.

## B-112 · The verification sweep cannot read into tenant accounts

**RESOLVED** — 2026-09-22. See the resolution below; the original statement
is kept because the reasoning that rejected the harness still explains why the
per-tenant check is narrower than design 13 asks for.

Three of design 13's nine verification checks need to read state *inside* a
tenant's own AWS accounts:

| check | what it must read |
|---|---|
| canary running and passing | the canary's last run result |
| backup policy and first recovery point | the backup vault |
| guardrail harness against these accounts | whatever the harness asserts |

`verify.py` runs in tooling, holds no role in a member account, and does not
assume one. All three report `undeterminable`, which blocks go-live — correct
behaviour, and useless behaviour, since it means the sweep can never clear for
any tenant no matter how healthy.

**What this needs.** A read-only role in the member baseline, assumable from
tooling by the verification function specifically, scoped to the handful of
describe calls these checks make. It is deliberately NOT the existing
`OrganizationAccountAccessRole`: that role is administrative, and a sweep that
runs on a schedule should not hold admin in every tenant account in the estate
to find out whether a canary passed.

**Trigger:** when the vesting pipeline (phase 3) first produces accounts. Until
then there is nothing to read into, and the checks are blocked by the absence
of accounts rather than by the absence of a role.

### Resolved 2026-09-22 — two reads served, and the third check replaced

Jamie agreed the narrowing the same day.

**Two of the three were genuine reads** and are now served by
`baseline/90-verification-reader.yaml`: `PlatformVerificationReader`, scoped to
six synthetics and backup actions, trusting only a `PlatformVerification*`
principal in tooling rather than the whole account. Deliberately not
`OrganizationAccountAccessRole` — a sweep on a schedule should not hold admin
in every tenant account to find out whether a canary passed.

`check_canary` and `check_backup` now read through it, and both distinguish the
three states that matter: cannot assume is **undeterminable**, never `fail`.
"I could not look" and "I looked and it is broken" send people to different
places.

Two specific traps are closed by the new code rather than by the role:

- **Zero canaries is a FAIL, not a pass.** An empty population produces zero
  failures, and a check reading that as health would clear a tenant with no
  monitoring at all.
- **A backup plan with no recovery point is a FAIL.** A plan that has never run
  is identical to a healthy one in every view showing configuration rather than
  outcome, which is exactly why design 13 asks for the first recovery point and
  not the plan.

`check_backup` was also **stale**: it reported that design 05 was absent and
that there was no backup policy to attach, which stopped being true when design
05 arrived and `recovery/30-backup-policy.yaml` was built and deployed. A stale
refusal is the same defect as a stale pass — the check was reporting what was
true when it was written, not the estate.

**The third check was the wrong check.** `scripts/test-guardrails.sh` proves
SCPs work by ATTEMPTING DENIED ACTIONS with an administrator: it creates IAM
roles, KMS keys, backup vaults and EBS volumes in the account under test, and
its own header requires management-account credentials. A read-only role cannot
run it; giving the sweep what it needs means a scheduled job holding admin
across the estate, and creating test IAM roles inside a client's account as a
condition of their go-live.

It is also redundant. SCPs attach to OUs. Running the harness for forty
accounts in one OU tests the same policy forty times.

So `check_guardrails` became an **inheritance assertion**: is this account under
the OU we published, and is anything beyond AWS's default `FullAWSAccess`
attached there? Expected comes from SSM, actual from Organizations — two
sources, so the comparison is not the registry agreeing with itself.
`org/30-verification-org-reader.yaml` carries the read.

**Proved live, not asserted.** The org reader was deployed to management
(`platform-verification-org-reader`), a temporary `PlatformVerificationProbe`
role was created in tooling, and the chain was exercised end to end:

| call | result |
|---|---|
| `ssm:GetParameter` on the published client OU | `ou-t7wy-cp0ss1x1` |
| `organizations:ListParents` on a real account | `ou-t7wy-9pmwmp9x` |
| `organizations:ListPoliciesForTarget` | three Platform SCPs + FullAWSAccess |
| `ssm:GetParameter` on `/platform/identity/*` | **AccessDenied** |
| `organizations:DescribePolicy` | **AccessDenied** |

Then `check_guardrails` itself was run against live Organizations and returned
`fail` with both sides of the comparison in `examined`. The probe role was
deleted and its absence confirmed.

This is the B-108 / B-113 / B-114 lesson applied rather than restated: test a
capability by USING it. Both denials above were checked as deliberately as the
grants.

**15 unit cases**, covering every refusal path including both undeterminable
branches for each read.

**Still open, and not this item's to fix.** The trust policies name
`PlatformVerification*` as a pattern because the verification function has no
deployment at all (B-110) — `provisioning/` contains no CloudFormation and the
saga has only ever run locally. Replace both with the function's exact role ARN
once that stack exists. A pattern that could have been an ARN is a standing
exception nobody revisits.

`baseline/90-verification-reader.yaml` is written and passes both gates; it
reaches accounts when the baseline StackSet next deploys, which for tenants is
their vesting.

## B-113 · Two registry record shapes, and the sweep read the wrong one

**Closed same day** — 2026-09-21.

`verify.py` was written treating registry records as raw DynamoDB attribute
maps (`{"state": {"S": "live"}}`), but `saga.record()` unwraps them to plain
values first. Every field read returned a string where a dict was expected, and
`.get("S")` on a string raised `AttributeError`.

**Two things caught it, and one of them is the design working.**

The sweep wraps each check in a try/except that reports a raising check as
`undeterminable` rather than letting it crash or counting it as a pass. So the
bug surfaced as "every check is undeterminable", which blocks go-live — the
failure was contained into the safe direction by the fail-closed rule rather
than by anything specific to this bug.

The test did not catch it, and that is the more useful lesson. It passed raw
table items straight into the check functions, so it exercised a shape the
production path never produces. A fixture that constructs its input differently
from the way the real caller constructs it tests a function that does not exist.
The tests now route through `saga.record()`, the same way the sweep does.

**A near miss worth naming separately.** The partner windows are stored as
DynamoDB **numbers**, not strings. `saga.record`'s unwrap reads
`v.get("S") or v.get("N")`, so copying that pattern was what made the live
check work — a version that handled only `S` would have read both windows as
unset and FAILED a correctly configured partner. The failure would have looked
like a compliance finding rather than a parsing bug, which is the expensive
kind of wrong: somebody would have gone looking at the contract.

## B-114 · A capability probe asked for a name the API does not use

**Closed same day** — 2026-09-21.

`pagerduty-check.sh` tested whether business services were available by looking
for `business_services` in the account's `/abilities` list. It was not there,
so the check reported `business_services=False`.

The account has **four business services**. `business_services` is simply not
an ability name — `GET /abilities/business_services` returns 404, as does
`service_dependencies`. The lookup asked a question PagerDuty does not answer,
and the negative answer was indistinguishable from a real one.

Had it been believed, the conclusion would have been that
`pagerduty/modules/tenant` could not create its business service, and design
12's client-conversation class would have been redesigned around a limitation
that does not exist.

This is the third of this shape in two days:

| | asked | actually answered |
|---|---|---|
| B-108 | can this credential administer AMI | can it administer *anything* |
| B-113 | what does this registry field say | what does this *attribute map* say |
| B-114 | does the account have business services | is `business_services` an ability name |

> Test a capability by USING it, not by looking for its name in a list. The
> weakest call that exercises the endpoint — here, listing — proves reachability
> and scope, creates nothing, and cannot answer a different question than the
> one asked.

Fixed: the check now lists business services and reports how many exist, and
`die`s if the endpoint is unusable, because the tenant module would otherwise
fail part way through provisioning a client.

## B-115 · The commercial PagerDuty resources were committed as "deployed" and never applied

**CLOSED** — 2026-09-21, and it took two passes. The first fix addressed the
obviously-suspect link and left a chain that still carried no event; see both
sections below, because the second pass is the more useful lesson.

Commit `f1cf3e7`, *"The client-conversation destination, deployed and proven"*,
added five PagerDuty resources for design 12's client-conversation event class:
a technical service, a business service, an escalation policy, a service
dependency and an Events API v2 integration.

None of them exist. `terraform state list` does not contain them, and the
PagerDuty account has no matching service or business service — only
`AltDigital Platform`, `AltDigital Platform – Low Urgency`, and unrelated
services belonging to other AltDigital work.

**The commit body does not actually claim otherwise, and that is the
instructive part.** Everything it verifies is AWS-side router behaviour: the
router refusing to emit with no notifier, routing awareness-only, withholding
notifiable detail, passing a cost anomaly through, declining an unrelated
event. All true, all verified, none of it touching PagerDuty. The word
"deployed" in the title covered both halves; the verification covered one.

**Consequence.** Design 12's client-conversation events have a router that
correctly decides where to send them and no destination to send them to. The
AWS side would emit, and the events would go nowhere — the failure being
invisible precisely because the sending half works.

### Partly closed 2026-09-21, and the chain is longer than first stated

Applied by the user. All five resources now exist: service `POD5UN1`
(`AltDigital Client Conversations`), business service `PS31XHG`
(`Client Conversations`), escalation policy `PPP1MJI`, the service dependency
and the Events API v2 integration `PC4MAUO`. The apply also proved write
access on the API key, which `pagerduty-check.sh` cannot.

`sync-pagerduty-secrets.sh` now writes the Events API v2 key to
`platform/pagerduty/commercial-routing-key` — a separate secret from the
platform one rather than another field inside it, because the blast radii
differ. Anything holding the platform key can page the on-call rotation;
anything holding this one can raise a low-urgency notification, and a consumer
needing only the second should not get the first as a side effect.

### Closed 2026-09-21 — the whole chain now carries an event

`commercial/src/notify.py` is the missing link, and tracing the path to find it
also turned up B-116, a live runaway that had been running for at least a day.

A Lambda rather than an EventBridge API Destination, because PagerDuty's Events
API v2 takes the routing key in the JSON BODY and has no header auth. A
destination would need the key baked into the rule at deploy time — the exact
wart `sync-pagerduty-secrets.sh` already warns about for the SNS subscription,
where rotating the key silently leaves a stale endpoint. Reading the secret at
invocation makes rotation one command with nothing downstream to remember.

`notify.py` re-checks the awareness allow-list before the payload leaves AWS,
importing `AWARENESS_FIELDS` from `route.py` rather than copying it. One
definition, two enforcement points — and the second is the only place the data
crosses an organisational boundary, which is the last moment the platform can
refuse.

**Verified live, end to end:** a cost-anomaly emitted onto `platform-alerts`
reached PagerDuty as incident #115 on the Client Conversations service at LOW
urgency — notifying, not paging — carrying its figures intact, because for a
cost anomaly the number is the point. Resolved after the test.

The path as it stood when this was opened:

| link | state |
|---|---|
| EventBridge rules match declared commercial events and incident dossiers | built |
| `route.py` decides awareness-only, withholds notifiable detail, refuses without a notifier | built and verified |
| `route.py` emits `client-conversation` back onto the alert bus | built |
| a rule matching `client-conversation` and forwarding to PagerDuty | built 2026-09-21 |
| PagerDuty service to receive it | built 2026-09-21 |
| routing key in Secrets Manager | built 2026-09-21 |

Nothing in the repository matches `DetailType: client-conversation` — the
router emits into the bus and no rule listens. So the destination now exists
and is still unreachable.

**What the missing link needs.** The platform alarm path reaches PagerDuty by
SNS subscription to the CloudWatch integration URL, which works because the
payload is an SNS-wrapped alarm the vendor integration parses. This payload is
arbitrary JSON going to Events API v2, so it needs shaping: an EventBridge API
Destination and Connection holding the routing key, and a rule targeting it.

**The lesson, restated more precisely than the original entry had it.** The
first version of this finding said the PagerDuty half was never applied. That
was true and incomplete: two of six links were missing, and applying fixed one
of them. Checking the link that was obviously suspect, finding it broken, and
stopping there would have left a chain that still does not carry an event —
and the next person would have read "B-115 closed" and believed it.

> A title that says "deployed" needs a verification line that names the thing
> deployed. "Verified live" covering one of two halves reads as covering both,
> and the half nobody mentions is the half nobody checks.

## B-116 · The commercial router fed itself, 27,000 times an hour

**Found and stopped 2026-09-21.** Mine, introduced in `f1cf3e7`.

`route.py` emits its routed event back onto the alert bus carrying
`commercial_event` so downstream consumers can see the class.
`DeclaredCommercialRule` matches on exactly that field existing. So the
router's output matched the rule that produced it:

    rule -> function -> emit -> rule -> function -> emit -> ...

**It was running.** 190,672 invocations in 24 hours, a sustained ~475 per
minute, every one of them re-routing the same `security-incident` class. It
stopped the instant the fix deployed: 475/min at 16:48, 203 at 16:49 as the
changeset landed, 1 at 16:51 — the deliberate end-to-end test.

**Why nobody noticed.** Nothing consumed `client-conversation` (B-115), so the
loop produced no pages, no tickets and no customer-visible effect. It spent
roughly a dollar a day across Lambda, EventBridge PutEvents and log ingestion,
which is inside the noise on a bill nobody was yet reading per-service. A loop
whose only symptom is cost is a loop that runs until somebody looks.

**The near miss is the point.** While nothing listened, this was invisible
waste. The moment the notifier existed it became **~27,000 PagerDuty incidents
per hour** on the Client Conversations service. The only reason that did not
happen is that the loop fix and the notifier went out in the same deploy — and
that was luck in ordering, not judgement: the loop was found while tracing the
chain for B-115, not by looking for it.

**Why the original verification missed it.** `f1cf3e7` tested `route.py` by
invoking the Lambda **directly**, which exercises the function and never
exercises the rule that triggers it. Every assertion in that commit is true.
None of them could have caught this, because the cycle only exists on the bus.

> Testing a handler by invoking it proves the handler. It cannot prove what
> triggers the handler, and a self-feeding rule is invisible to every test
> that starts after the trigger.

**Fixed in two places, deliberately.** The rule now excludes
`source: platform.commercial`, which stops the loop before it reaches Lambda;
and `classify()` declines any event whose `record_type` is already
`client-conversation`. The rule is the cheaper stop but lives in a template
somebody could widen, and a self-feeding router should not be reintroducible by
editing infrastructure.

**Open follow-up:** no alarm would have caught this. An invocation-rate alarm
on the platform's own Lambdas — or a PutEvents-rate alarm on the alert bus —
would have. design 06a has no entry for either. Tracked here rather than
silently accepted.

## B-117 · Non-production tiers escalate to the production rotation

**Open, owned by Jamie and Wayne** — 2026-09-21.

D-018 creates PagerDuty services for every tier. Each technical service needs
an escalation policy, and non-production ones currently fall back to
`AltDigital Platform Escalation`, which is the production rotation.

**Mitigated, not solved.** Non-production services carry **low** urgency, so
they notify rather than page: a dev alert reaches responders through their own
notification rules without forcing a phone call. Nobody is woken by a dev
environment today.

But the target is wrong even where the effect is currently tolerable. The
people who should hear about a client's dev tier are the developers working in
it and whoever supports their infrastructure, Jamie for now, not the platform
on-call rotation. As the estate grows, "low urgency to the wrong people" stops
being harmless and becomes a channel nobody reads.

**What it needs.** Probably a schedule and policy per audience rather than per
tenant: an infrastructure-support policy covering non-production across all
clients, possibly a per-client developer policy where the client's own people
are in PagerDuty. That is a rotation design question, not a code question.

**Where it lands.** `tier_escalation_policy_ids` on `pagerduty/modules/tenant`,
keyed by tier name. Answering this is a data change in `tenants.json` plus
whatever policies get created at the root config. No restructuring.

**Trigger:** before the first client with a non-production account goes live,
or before any client's own developers are given PagerDuty access, whichever
comes first.

### Follow-up closed 2026-09-21 — runaway detection built

`alerting/30-runaway.yaml`, deployed to Audit as `platform-runaway`. Three
account-wide alarms at 500 per five-minute period, two evaluation periods:
Lambda `Invocations`, EventBridge `PutEventsEntriesCount`, EventBridge
`TriggeredRules`.

**Account-wide, with no dimensions, and that is the point.** The obvious
design is an invocation alarm per function, and it is wrong because it has to
be remembered — the gap that let this run for a day was precisely that nobody
had added one. A control whose coverage depends on being extended by hand has
holes shaped like whatever was built most recently. Account-wide covers every
function that exists now and every function added later by somebody who has
never read the template.

For the EventBridge metrics it was not even a choice. Measured on the live
account, the `EventBusName` dimension publishes **no data** for
`PutEventsEntriesCount`, `TriggeredRules` or `MatchedEvents` — only the
account-level metric carries the signal. A per-bus alarm would have been
permanently inert, which is the B-100 shape and would have been indispensable
right up until it was needed.

**Thresholds measured, not guessed**, either side of the fix:

| metric | normal / 5min | during the loop / 5min |
|---|---|---|
| Lambda `Invocations` | 1–6 | ~5,000 |
| `TriggeredRules` | low single digits | ~4,967 |
| `PutEventsEntriesCount` | low single digits | ~2,604 |

Three orders of magnitude apart, so 500 is not a fine judgement: ~100x above
normal, 5–10x below the observed runaway.

**Verified retrospectively against the real incident:** every one of the 36
five-minute periods in the loop window exceeded the threshold — 100%. The
alarm would have fired within ten minutes of the loop starting and stayed in
ALARM until it was fixed.

`docs/runbooks/runaway.md` carries the diagnosis and the stop procedure. The
stop is deliberately manual: CLAUDE.md forbids automated termination, and a
containment routine that disabled rules on a rate signal would eventually
disable the platform during a legitimate burst, at the worst possible moment.

**Known limitation, accepted rather than solved.** The threshold is fixed and
the estate will grow into it. Anomaly detection was considered and rejected —
it needs weeks of history, and a band that widens on its own would have
widened around B-116 given long enough. Adapting to a runaway is exactly the
wrong behaviour here. Revisit trigger is tenant count: when routine operation
reaches a quarter of the threshold, raise it and re-measure.

## B-118 · cron() schedules are not cadence-checked

**CLOSED 2026-09-22.** `cadence.py` now enumerates cron firings rather than
refusing them. AWS cron is always UTC and its fields are small sets, so
counting days in a bounded window and checking each against the expression is
exact and cheap -- no library, and no month-length approximation that is wrong
for February.

The window is probed outward until it contains at least `MIN_INTERVALS`
firings, giving a monthly cron months and a daily cron days. That is the same
property `interval * MIN_INTERVALS` gives a `rate()` rule, and it is what keeps
"did not run" distinguishable from "not due yet".

**Second pass, same day.** The first version refused `L`, `W` and `#`, and
deploying it showed why that was not good enough: the estate's ONE cron rule is
`cron(0 14 ? * 3#2 *)` -- the second Tuesday of the month -- so the checker
evaluated nine rules of ten and reported the tenth as unevaluated forever. The
gap said so, which is the property that was defended, and it was still a gap
covering exactly the case that existed.

`#` and `L` are now computed exactly, because enumeration makes them arithmetic
rather than approximation: `3#2` asks which occurrence of its weekday a date
is, and `L` asks whether the next day is in another month. Only `W` -- "nearest
weekday", which does not cross a month boundary and moves a Saturday the 1st
forward rather than back -- is still refused, and no rule in the estate uses it.

Deploying also found a second defect. A 100-day window at hourly granularity is
2400 datapoints and CloudWatch rejects anything over 1440. The clamp went into
`triggered_series` rather than the cron call site, because the limit is a
property of the API and not of one caller.

**Verified live:** 10 of 10 scheduled rules evaluated, none unevaluated. The
monthly rule reports `no_metric` -- its stack was created on 2026-09-19 and the
second Tuesday of September was the 8th, so it has legitimately never fired and
next fires on 13 October. That is the "absence of evidence, not evidence of
absence" case the verdict exists for.

**Option 1 below was rejected on reflection.** Converting the containment
exercise to `rate(30 days)` would trade a predictable monthly date for one
that drifts through the month and eventually lands on a weekend -- changing the
platform to suit the checker, for an exercise people plan around.

The original entry follows.

**Open, narrow** — 2026-09-21.

`alerting/src/cadence.py` derives each rule's expected firing rate from its own
`ScheduleExpression`. It handles `rate(N unit)` exactly and **refuses** to
evaluate `cron()`, reporting `not_evaluated` with the reason.

Expanding an arbitrary cron expression needs a real cron library. The runtime
has none, and an approximation would be wrong in the direction that destroys
the control: a healthy weekly job would read as six days of silence, and a
control that cries wolf is a control somebody mutes.

**Currently affects one rule:** `platform-containment-exercise-monthly`. Nine
of the estate's ten scheduled rules are `rate()` and are checked.

**Options, in order of preference:**

1. **Convert the rule to `rate(30 days)`** if monthly-ish is good enough. One
   line, no new code, no dependency. A containment exercise does not need to
   land on the first of the month.
2. Vendor a small cron expander into the package. More code in the path of a
   control whose value is that it is simple.
3. Leave it. The rule is reported as unevaluated on every sweep, so the gap is
   visible rather than silent — which is the property that makes leaving it
   tolerable.

**Trigger:** when a second cron rule appears. One unevaluated rule is a
footnote; two is a pattern, and the third will arrive without anybody noticing
the list has grown.

## B-119 · The router picks a PagerDuty key by tier, and knows nothing about tenants or layers

**RESOLVED** — 2026-09-22. Rewritten and then closed the same day. The original entry was wrong in a way that
would have cost somebody a day, so what it said is kept here rather than
deleted.

**What it said, and why it was wrong.** It said each tenant's routing key had
to be distributed into the tenant's own AWS account, because "the SNS
subscription carrying them to PagerDuty is created by the baseline in that
account".

The baseline creates no PagerDuty subscription. Member alarms publish to
`AlertTopicArn` and `LowUrgencyTopicArn`, both of which are cross-account SNS
topics **in Audit**; `70-event-forwarding.yaml` sends everything else to the
central bus **in Audit**; and every consumer of a routing key in this
repository lives in `alerting/` or `commercial/`, which deploy to Audit.
Verified live: the `platform-alerting` stack exists in 692869638531 and does
not exist in tooling. No member account ever talks to PagerDuty.

So the keys are already in the account they belong in, the "interim home" was
the permanent one, and the trigger the entry was waiting on would never have
fired. Anyone picking this up would have built key distribution into tenant
accounts that nothing needs.

**The actual gap**, which is in Audit and is one function. `alerting/src/router.py`:

    def routing_key(tier):
        name = PD_SECRET_NAME_LOW if tier in ("dev", "test") ... else PD_SECRET_NAME

Two secrets, both AltDigital's own services, and the word `tenant` does not
appear in the file. Every tenant's alarm would page AltDigital. The per-tenant
keys `sync-pagerduty-tenants.sh` writes under
`platform/pagerduty/tenant/<client>/<tier>/<object>` have no consumer at all —
the services built in 12.2 exist in PagerDuty and nothing routes to them.

**What it has to become.** Key selection on **(tenant, tier, layer)**:

| dimension | where it comes from |
|---|---|
| tenant | the alarm's account id, through the tenant fact map in SSM |
| tier | the alarm's own `platform-tier` tag, as today |
| layer | the alarm's `platform-layer` tag — B-127 |

The layer dimension is Jamie's requirement of 2026-09-22: AltDigital owns the
AWS infrastructure and a separate team owns the application, and they page
differently even when both teams work for the same company. Arc8 is exactly
that case and is the first tenant.

**The SNS fallback stays pointed at AltDigital, deliberately.** The
subscription in `alerting/10-alert-topic.yaml` resolves one key at deploy time
and cannot be made tenant-aware without a topic per tenant. It does not need
to be: it is the fallback for a broken router, and a broken router is
AltDigital's incident. Waking a client's on-call because our routing layer
failed is the wrong page, and the fallback firing at all is itself the signal.

**Trigger:** before the first tenant account's baseline deploys. Until then
every alarm in the estate is AltDigital's own and the tier-only selection is
correct for the population it runs over.

### Resolved 2026-09-22 — the router selects on (tenant, tier, layer)

`routing_key(tier)` became `routing_key(tier, layer, account_id)`, and the
three dimensions come from three different places so none can be quietly
re-derived inside the router:

| dimension | source |
|---|---|
| tenant | the account id, through `/platform/change/tenant-facts` |
| tier | the alarm's `platform-tier`, off the description trailer |
| layer | the alarm's `platform-layer`, ditto — B-127 |

`platform_key(tier)` keeps the old two-secret behaviour under a name that says
what it is: AltDigital's own service, which is the right destination for
AltDigital's own accounts and the fallback for everything that cannot resolve.

`sync-tenant-facts.sh` now publishes each account's `app`, because the tenant
services are keyed on (tier, object) and the object for an application-layer
alarm is that app. Without it the router could not name the tenant's own
service.

**Every failure falls back to AltDigital WITH A NOTE, and never raises.** A
routing lookup that fails during an incident must not turn a page into no page;
the wrong page is recoverable in a way silence is not. Each fallback prints a
`routing-key-fallback` record, because a fallback nobody can see becomes
permanent — and this one looks exactly like working until a tenant asks why
they never hear from us.

**The SNS fallback still points at AltDigital**, unchanged and deliberately.
`alerting/10-alert-topic.yaml` resolves one key at deploy time and cannot be
tenant-aware without a topic per tenant. It does not need to be: it is the
fallback for a broken router, and a broken router is AltDigital's incident.

13 cases, including every fallback branch and an alarm predating the field.

## B-120 · Nothing schedules the PagerDuty drift check

**Open** — 2026-09-21.

`scripts/pagerduty-drift.sh` is built and works: `terraform plan
-detailed-exitcode` against the live account, reporting rather than
correcting. It found real drift on its first run.

Nothing runs it. There is no CI in this repository (see B-110's correction),
so it executes when somebody types it — which means a console edit is detected
whenever the next person happens to look, not when it happens.

prompt 12.2 asks for drift detection as a property of the system, not as a
command that exists.

**Options:**

1. **A scheduled job on a workstation.** Cheapest and least reliable; it
   detects nothing while the machine is off.
2. **CI.** Would also serve the AWS baseline, `cfn-lint`, `cfn-guard` and the
   test suites, all of which are equally manual today. The largest piece of
   work and the one with the broadest payoff.
3. **A Lambda that checks existence only** — services carrying the platform
   provenance marker against what the registry says should exist. Schedulable
   today, and materially weaker: it catches a deleted service, not an
   escalation policy quietly retargeted. It would also be a second
   implementation of "what should exist", which is exactly what using
   `terraform plan` avoids.

Option 2 is the honest answer and the one to plan for. Option 3 is a
consolation prize that adds a second source of truth to a design built to have
one.

**Trigger:** before the first client tenant is live in PagerDuty. Until then
the only services are the platform's own and a drift window measured in days
is tolerable; once a client's paging depends on it, it is not.

## B-121 · Tier semantics are enumerated in templates, not derived from the tier table

**Open** — 2026-09-21.

`config/tiers.yaml` is now the single source of truth for what an environment
tier means, and `scripts/lib/tiers.py` resolves it. CloudFormation cannot read
it, so templates still enumerate tier names:

```yaml
MetaAlarmsLowUrgency: !Or
  - !Equals [!Ref EnvironmentTier, dev]
  - !Equals [!Ref EnvironmentTier, test]
  - !Equals [!Ref EnvironmentTier, uat]
```

That list is a second copy of `resilience: non_production`, and the first copy
of it was already wrong — `uat` was missing, so a UAT environment would have
paged the on-call rotation against design 14's matrix. Nothing reported it;
the alarm worked, it simply woke the wrong people.

**Proposed fix.** Generate a `Mappings` block from `config/tiers.yaml` at
publish time, the way `publish-router.sh` bakes 06a's event patterns in with
`extract_event_specs.py`. Templates then condition on
`!FindInMap [TierSettings, !Ref EnvironmentTier, pagerduty_routing]` and the
enumeration disappears.

The precedent is deliberate: that script exists because the router and the
instrumentation Lambda must not disagree about 06a, and the same argument
applies here.

**Interim mitigation.** The conformance test asserts the resolved matrix
matches design 14 row for row, so the TABLE cannot drift from the design
silently. What it cannot catch is a template that fails to consult the table —
which is exactly what happened here.

**Trigger:** before the first UAT environment is vested, or at the third
template that enumerates tier names, whichever comes first. Two is a
coincidence; three is a convention nobody decided on.

## B-122 · Data integrity across a HOOP cycle needs a per-tenant query set

**Open** — 2026-09-21.

design 14 requires, monthly: *"Row counts and checksums consistent before and
after"* a full turndown/turnup cycle.

`scheduling/src/cycle.py` reports this as **undeterminable** rather than
implementing it. Checking integrity requires knowing which tables matter in an
application the platform did not build and cannot introspect safely, and a
generic implementation would check whatever happened to be easy to count —
then report consistency, which is worse than reporting nothing.

**What it needs.** A per-tenant query set supplied at onboarding: a handful of
statements whose results should be stable across a stop and start. That is
questionnaire territory rather than code, and the customer's engineers are the
only people who know which tables answer the question.

**Why it is not urgent.** A HOOP stops compute and leaves storage untouched;
there is no mechanism by which a correct turndown loses rows. The check exists
to catch the *incorrect* turndown — a terminated instance, a volume detached —
and `validate_turndown` already catches both of those directly and
immediately, rather than monthly and by inference.

**Trigger:** the first tenant running a database the platform did not
provision, or the first monthly cold-cycle exercise, whichever comes first.

## B-123 · Three of design 14's eight scheduling interactions are not wired

**Open** — 2026-09-21.

`scheduling/src/interactions.py` carries a `WIRING` table recording where each
of design 14's eight interactions is implemented. Three are not, and the table
says so rather than leaving a reader to conclude from silence.

### canaries — not wired

design 14: *"Paused during down, resumed and used as the turnup validation."*

Pausing needs `synthetics:UpdateCanary` in the tenant account; resume-as-
validation needs to READ the canary result, which is the same cross-account
gap as B-112 and the reason `cycle.functional_validation` reports
undeterminable.

**Cost of leaving it:** a canary running against a scheduled-down environment
fails every run, all night, every night. That trains people to ignore canary
alerts — which is worse than the canary not existing, because the canary is
the only check that says the service *works* rather than that it is running.

### change reconciliation — not wired

design 14: *"A deployment attempted against a stopped environment is a
legitimate failure, not a finding."*

`change/src/reconcile.py` would need to read the HOOP state and classify a
deployment failure during scheduled down as expected. Today it reports a
failed-or-unapproved deployment, which is a finding about a person who did
nothing wrong.

**Cost of leaving it:** the finding is wrong, and being wrong about somebody's
deployment is how a compliance control loses the room.

### alarm actions — partial

design 14: *"Suppressed during scheduled down, or every turndown pages."*

The state is published and the gap detector reads it, but alarm *actions* are
not disabled on turndown. The exposure is narrower than it looks: most alarms
on a stopped instance go `INSUFFICIENT_DATA` and do not fire.

The exception is any alarm with `TreatMissingData: breaching` — which now
includes `platform-cadence-sweep-stalled`, deliberately. That one lives in
Audit and is not affected by a tenant turndown, but the pattern will recur the
moment a tenant-side absence alarm is written.

**Trigger for all three:** the first tenant with a HOOP. None of them is
reachable before that — there is no tenant canary, no tenant deployment and no
tenant alarm to suppress — which is why they are recorded rather than built
against an environment that cannot exercise them.

## B-124 · Three sources give three different restore-duration thresholds

**CLOSED 2026-09-22.** Jamie confirmed 50%/80%. The reasoning that decided it:
a breach alarm at 100% of RTO fires when the commitment has already been
missed, with nothing left to do about the restore in progress; at 80% there is
still budget to escalate. `docs/deviations.md` D-019 records the departure from
design 05's indicative 75%/100%, and open-items B3 now reads as decided.

The original entry follows.

**Open, needs Jamie** — 2026-09-22.

| Source | Warning | Breach |
|---|---|---|
| `recovery/` (worker, workflow, schedule) | 50% of RTO | 80% of RTO |
| `design/05-resilience.md` | ~75% "indicative", **"not decided"** | at or near 100% |
| `design/open-items.md` B3 | — | "values not set" |

The 50/80 values were decided by Jamie on 2026-09-18 and written through three
files with their reasoning. open-items B3 was never updated to record that, and
design 05 — supplied later — restates the question as open with a different
indicative answer.

**Nothing has been changed.** Picking either silently would be worse than the
disagreement: the code carries a justification for 50/80, and 05 carries the
authority of the design package.

**The engineering argument favours keeping 50/80,** and it is about what the
threshold is *for*. A breach alarm at 100% of RTO fires when the commitment has
already been missed — it is a lagging indicator, and there is nothing left to
do about the restore in progress. At 80% there is still budget to escalate,
add capacity or start a parallel path. The same logic applies to the warning:
at 50% the trend is visible with half the budget intact; at 75% the
conversation is about an incident rather than about capacity.

That is a recommendation, not a decision. 05's point that the values are
per-application stands either way — both are parameters, because the RTO they
are a percentage of is per tenant.

**Resolution:** confirm 50/80 or adopt 75/100, then update whichever two of the
three sources disagree. Leaving all three is the only option that is certainly
wrong.

## B-125 · A deny keyed on a role ARN is only as strong as control over role creation

**CLOSED 2026-09-22**, and the investigation changed what the finding was.

The proposed fix was "write an SCP denying `iam:CreateRole` for
`PlatformQuorumExecutor`". That SCP **already existed** —
`03-protect-platform-identity.json`, statement `DenyPlatformRoleNameSquatting`,
denying creation of any `role/Platform*`. It was attached to the Security OU,
where the backup account lives. It should have stopped the attack and did not.

**Because it exempts `OrganizationAccountAccessRole`, which is what the attack
used.** The exemption is necessary and not a mistake: the platform deploys
through that role, and without the exemption no `Platform*` role could be
created at all. So the real shape of the gap was never "the control is
missing" — it was that the control's scope and the threat model had drifted
apart:

| principal | vault policy | SCP | holds |
|---|---|---|---|
| compromised tenant principal | denies | denies | **yes** |
| operator with `OrganizationAccountAccessRole` | defeated by minting the role | exempt | **no** |

design 05 puts vault deletion under quorum (design 03) precisely because a
trusted operator is not supposed to be sufficient. That is the whole content of
"quorum": one person cannot do it alone. An exemption that lets one person mint
the quorum role does not weaken the vault policy — it removes the quorum.

**The fix is one narrow statement**, added beside the one it qualifies:
`DenyQuorumExecutorCreationEvenByOperators` denies create-and-modify on
`role/PlatformQuorumExecutor` exactly, exempting **only** `stacksets-exec-*`.
`OrganizationAccountAccessRole` is deliberately absent from that list, unlike
every other statement in the policy.

Scoped to the one role rather than widened across `Platform*`, because the
broad exemption is load-bearing everywhere else — removing it would break
deployment for a problem that exists for exactly one role name.

**Verified by re-running the attack.** `iam:CreateRole` for
`PlatformQuorumExecutor` through `OrganizationAccountAccessRole` now returns
`explicit deny in a service control policy`, and creating an unrelated
`Platform*` role through the same principal still succeeds — so the platform's
deployment path is intact.

**A consequence worth knowing.** The backup account now has no principal able
to delete the copy vault, because `PlatformQuorumExecutor` does not exist there
and can no longer be created by hand. The recovery path is to deploy the quorum
stack to that account via StackSets, which creates the role properly. That is
not an obstacle — it is the control doing its job: replacing that vault is now
a deliberate, auditable act rather than something one person does in a
terminal.

**The KMS concern in the original entry stands and is now also covered**, since
the same statement prevents minting the role that the key policy's `ArnNotLike`
names.

The original entry follows.

**Open** — 2026-09-22. Found by locking myself out of a vault I had just made.

`recovery/40-backup-vault.yaml` and `30-backup-policy.yaml` both express design
05's quorum requirement as a vault access policy:

```yaml
Effect: Deny
Action: [backup:DeleteBackupVault, ...]
Condition:
  ArnNotLike:
    aws:PrincipalArn: arn:aws:iam::<account>:role/PlatformQuorumExecutor
```

That is genuinely effective against the threat it names — a compromised member
account principal cannot delete its own backups, and it stopped
`OrganizationAccountAccessRole` cold when the vault needed replacing.

**It is defeated by creating a role with that name.** Which is exactly what was
done to recover: `iam:CreateRole` named `PlatformQuorumExecutor`, assume it,
delete the vault. No quorum was involved, and nothing about the vault policy
was bypassed — the policy was satisfied.

**The control is therefore not the vault policy.** It is the vault policy plus
whatever prevents arbitrary role creation in that account, and the second half
is currently nothing. A member account with local admin can mint the role.

**What closes it,** in rough order of strength:

1. **An SCP denying `iam:CreateRole` and `iam:PutRolePolicy` for a role named
   `PlatformQuorumExecutor`** in every account except where the platform
   creates it. This is the direct fix and belongs with the other SCPs in
   `policies/scp/`.
2. Key the deny on a role's unique **RoleId** (`aws:userId`) rather than its
   ARN. A RoleId cannot be reproduced by recreating a same-named role — but it
   changes when the role is legitimately recreated, so the vault policy would
   need updating on every quorum-stack redeploy.
3. Vault Lock itself, which is not defeated by this and is the reason the
   exposure is narrower than it looks: once the lock hardens, *no* principal
   including root can shorten retention or delete a non-empty vault.

**Why it is not urgent but must not be forgotten.** Vault Lock is the real
protection for recovery points; this policy is defence in depth over the vault
object. But the same `ArnNotLike` idiom appears in the KMS key policy, and
deleting the KEY destroys every copy encrypted under it — which Vault Lock does
not prevent.

**Trigger:** before the first tenant's backups land in the copy vault.

## B-126 · Partner cost isolation needs a generated report, not a permission set

**Open** — 2026-09-22. Replaces the unbuildable half of prompt 14.5; see D-020.

Cost Explorer authorises the call and not the data (proven — D-020 carries the
evidence), so a partner business user either sees the whole estate's spend or
none of it. Isolation has to happen before the data reaches them.

**What to build.** A partner-scoped run of `cost/src/report.py` that produces
one partner's figures as a document, delivered to them. The partner receives
the output, not the query interface — the same shape as design 12's commercial
router, which sends a payload rather than granting a query.

**What makes it testable**, which the permission-set version never could be:
a generated document either contains another partner's account ids or it does
not, and that is assertable in a unit test rather than by attempting an access
and hoping the denial holds.

**Related:** B-090 records the three per-audience cost reports Jamie asked for
(cost by account by tier; by tier by object type; shared-services cost). This
is a fourth audience with a harder requirement — the others are internal, and
this one is the only report where including a row by mistake is a commercial
incident rather than noise.

**Trigger:** before the first partner business user is given any commercial
access. There is currently nobody it would be wrong to show the portfolio to,
which is the only reason this is not urgent.

## B-127 · Nothing records which team owns a resource

**RESOLVED** — 2026-09-22. Decided with Jamie the same day.

Within one tenant account, two teams own resources. The baseline puts the VPC,
KMS keys, log groups, Config recorder and backup vault into the account —
AltDigital's. The application's RDS, ALB and ECS services sit in the same
account and belong to whoever owns the application. For Arc8 that is Wayne's
dev team; for a later client it is the client's own developers. Same account,
same tier, different people to wake.

Nothing distinguishes them. No tag, no registry field, no alarm dimension.

**The tag.** `platform-layer`, values `infrastructure` and `application`.

Named for the layer rather than the owner because a required free-text `Owner`
tag already exists on every resource and means a *person*. Two tags called
owner meaning different things is the failure this codebase keeps writing
comments about.

Entity-blind, in the same sense as B12: the tag never names a team. It says
which layer the resource is, and the tenant's registry record says which
PagerDuty service that layer maps to. So the identical tag value means
"Wayne's team" for Arc8 and "the client's developers" for the next tenant,
with no code change.

**Hard rule, Jamie 2026-09-22: everything the platform builds carries
`platform-layer: infrastructure`.** Recorded in CLAUDE.md, because it binds
every template written from here on and not just this item.

**A tag is a claim, not provenance.** The router sees an alarm, not a
CloudTrail event, so at alarm time a tag is all it can read — and a dev team
that tags its resource `infrastructure` pages Jamie at 3am with nothing
noticing.

Jamie's own discriminator is better and is already half-built: which Jira
project authorised the change. `reconcile.py` reads `jira_change_project` from
the tenant fact map today. So the fast path is the tag and a slower control
checks it — a resource whose `platform-layer` disagrees with the project that
created it is a reconciliation finding. Same shape as everywhere else here.

**The four pieces.**

1. `policies/scp/04` requires `platform-layer` at creation with the value
   constrained to the two-word vocabulary, alongside the existing `Owner` and
   `Application` requirements. Note 04 already exempts `role/Platform*`,
   `OrganizationAccountAccessRole*` and `stacksets-exec-*` — which is how
   platform-deployed resources will satisfy the hard rule without the SCP
   fighting the baseline.
2. The instrumentation Lambda stamps it on each alarm it creates, beside the
   `platform-tier` tag it already writes.
3. `router.py` selects the routing key on it — B-119.
4. The tenant Terraform module gains a reserved `infra` object per tier, so
   `tenant-<id>-<tier>-infra` exists with AltDigital's escalation policy
   alongside the application's service. No structural change to the module:
   services are already keyed on (tier, object).

Application-layer alarms page the application team's service and notify
AltDigital's infrastructure service at low urgency, same dedup key so the two
correlate in the console. Jamie: "probably us too, just in case."

### Resolved 2026-09-22 — all four pieces

1. **SCP 04** carries `PlatformLayerMustBePresentAndOneOfTwo`. One statement,
   not the two `DataClassification` uses, because `StringNotEquals` without
   `IfExists` evaluates true when the key is absent — so one statement denies
   both a missing tag and a wrong value.
2. **`PLATFORM_LAYER_TAG_VALID`** in `policies/guard/baseline.guard`, covering
   what the SCP's exemption of `Platform*`, `OrganizationAccountAccessRole*`
   and `stacksets-exec-*` necessarily leaves open — which is every template the
   platform deploys. Proved against a deliberate typo.
3. **The instrumentation Lambda** attributes each alarm from the Config
   configuration item's own tags, so there is no extra call and no second place
   the answer could differ from the resource.
4. **The tenant Terraform module** gains a reserved `infra` object in every
   tier, added in the module rather than in `render-pagerduty-tenants.sh`: the
   renderer derives objects from each account's `app` field, so a changed
   renderer or an account without one could produce a tier with no
   infrastructure service, and the failure would be silent. Its escalation
   policy is AltDigital's regardless of tier, because what decides who answers
   is which team owns the resource and not which environment it sits in. Its
   *urgency* still follows the tier — the layer decides who is woken, not how
   loudly.

**The direction of the unknown-value default, which I got wrong first.** The
router's layer branch was written as

    INFRA_OBJECT if (not layer or layer == "infrastructure") else None

which sends every value it does not recognise — a typo, a future third layer, a
truncated trailer — to the APPLICATION team. That is the one direction that
wakes a client at 3am for something that may well be ours. It is now

    None if layer == "application" else INFRA_OBJECT

so only an exact, recognised `application` reaches the application team. The
unit case that covered this originally asserted the wrong behaviour and passed,
which is worse than no case at all: it documented the defect as intended.

**What this enforces today, and what it does not.** `attachments.tsv` puts SCP
04 on Sandbox only, deliberately — it is the only policy in the set that denies
ordinary creation rather than tampering with a control, and the manifest is
explicit that widening it to Members is a conversation rather than a
deployment. So in a tenant account an application team can create untagged
resources freely. Those alarms route to AltDigital by the default, which is the
safe direction, but the split only becomes real for a tenant once 04 is widened
— a conversation with Arc8 about their pipeline tagging resources, not a
decision this item can make.

B-129 carries the provenance cross-check the tag cannot provide by itself.

**Drift, found 2026-09-29.** The layer-tag statement
(`PlatformLayerMustBePresentAndOneOfTwo`) is in the file but NOT in the live
`PlatformRequireTags` on Sandbox (`deploy-policies.sh --drift`). It was committed
in 2661274 and never deployed, so the text above describes a control that is
not running. Deploying it will fail the harness's tagged-volume control, which
carries no `platform-layer` tag, so the harness change and the deploy go
together, in the exemption rewrite (B-135).

## B-128 · A daily report of resources missing the layer tag

**Deferred with a trigger** — 2026-09-22. Jamie asked for it and deferred the
design the same day: "that report design is for a different day."

An operational report, mailed daily, of objects and services carrying no
`platform-layer` tag.

**Why it is not optional once B-127 lands.** An untagged resource does not fail
loudly. Its alarms fall to whatever the router's default is, which means either
the wrong team is paged or nobody is — and both look exactly like a quiet
night. The SCP catches creation, but it exempts the platform's own roles and
cannot see resources that predate it, so enforcement at creation is not the
same as coverage across the estate.

This is the same declared-expectation principle the cadence checker uses:
absence is only detectable against a declaration, and the tag is the
declaration.

**Design deliberately not attempted here.** Jamie deferred it, and the audience
matters more than the mechanism — who reads it, whether it is per-partner, and
whether it belongs with the three unbuilt cost reports in B-090, which are also
"generate a document and send it" and would otherwise be built twice.

**Trigger:** when anything other than the baseline first creates a resource in
a tenant account — that is, the first `AppDeployer` deployment. Before that
every resource in the estate is platform-built and carries the tag by the hard
rule, so the report would have nothing to say and would teach people to ignore
it.

## B-129 · The layer tag is a claim, and nothing checks it against provenance

**Open** — 2026-09-22. Split out of B-127, which built everything except this.

`platform-layer` is read at alarm time, so it has to be a tag: a CloudTrail
lookup on the path that pages people is slow, fragile, and runs while an
incident is starting. But a tag is something the creator asserts about itself.
An application team can tag a resource `infrastructure` and page Jamie at 3am,
and nothing would notice.

Jamie's own discriminator is better and is already half-built: which Jira
project authorised the change. `change/src/reconcile.py` reads
`jira_change_project` from the tenant fact map today. Resources created through
the Core App Infra project are AltDigital's; resources created through the
client-specific project belong to that team.

**What to build.** A reconciliation finding when a resource's `platform-layer`
disagrees with the project that created it. The fast path stays the tag; a
slower control checks the claim against the record, which is the shape used
everywhere else here — the guardrail harness against the SCPs, the registry
against Organizations, the cost categories against the account list.

**Why it is not urgent.** It cannot fire until an application team is creating
tagged resources in a tenant account, which needs both a tenant and SCP 04
widened to Members. Until then every resource in the estate is platform-built
and the claim and the provenance are the same thing.

**Trigger:** when SCP 04 is widened to the Members OU. That is the moment the
tag starts being asserted by somebody other than us.

## B-130 · Onboarding is an operator script, and the saga it mirrors is not deployed

**Open** — 2026-09-23. Owner: Jamie.

`scripts/onboard-client.sh` drives design 13's sequence from a workstation.
`provisioning/src/saga.py` implements the same sequence as a Lambda. Only the
first of the two can actually run: `provisioning/src/` has no CloudFormation
template and no publish script, so there is no `platform-provisioning`
function to invoke, and `scripts/jira-check.sh` is the only thing that reaches
that code path — through a function that does not exist either.

**Why two implementations is the wrong end state.** They will drift. The
script is what gets used, so it is what gets fixed, and the saga becomes a
description of how onboarding used to work. That is worse than having only
one, because the saga is the artefact an auditor would be shown.

**Why the script exists anyway.** Half of this work cannot live in a Lambda.
PagerDuty is applied by Terraform from a working copy, and
`render-pagerduty-tenants.sh` writes a file into the repository — design 13
requires PagerDuty to be versioned and drift-detected like the AWS baseline,
and a Lambda calling the API would put every tenant service outside Terraform
state. So the operator half is not a stopgap; it is load-bearing, and the
question is which steps belong on each side rather than which one wins.

**What to do about it.** Not a rewrite. Publish the saga, reduce it to the
steps that genuinely belong in a Lambda — registry, Truveon, verification —
and have the script own the staged-apply steps and call the function for the
rest. The step numbering in `onboard-client.sh --list` already carries the
design 13 mapping both would share.

**Trigger:** the second real tenant. One tenant can be onboarded by hand with
the script and the drift costs nothing; two means the sequence is being
repeated from memory, which is the point at which the two copies start to
disagree about what happened.

## B-131 · `console_access` is collected, recorded, and enforces nothing

**Open** — 2026-09-23. Owner: Jamie.

Questionnaire 3.7 asks whether staff need direct AWS console access as opposed
to pipeline-only. `vesting/roster.schema.json` carries the answer per person
and `scripts/derive-roster.py` warns about it. Nothing acts on it: a person
marked `console_access: false` is put in exactly the same Identity Center
group, bound to exactly the same permission set, as everyone else in that
environment.

**Why it is a warning and not silence.** The alternative was to drop the field
until it could be enforced. That is worse — the question is already in a
client-facing questionnaire, so a client has answered it and reasonably
believes it means something. A recorded answer that visibly does nothing is
honest; a question whose answer is discarded is not.

**Why enforcing it is not trivial.** There is no clean AWS control for "API
yes, console no". The nearest real mechanism is an SCP or permission-set
policy keyed on the federated session, and the console and the CLI both arrive
as the same federated principal. What would actually express the intent is not
issuing the grant at all and having the person work through the pipeline — in
which case the honest implementation is to leave them off the roster for that
environment rather than to invent a half-grant.

**Trigger:** the first client who asks what `console_access: false` did. That
conversation decides whether it becomes a real control or is removed from the
questionnaire — and either is better than the current state.

## B-132 · The saga's PagerDuty check cannot see that the tier services are missing

**Open** — 2026-09-23. Owner: Jamie. Found while building
`scripts/onboard-client.sh`.

PagerDuty has to be applied **twice** per tenant, and only one of the two
passes is visible to `provisioning/src/saga.py`.

`pagerduty/modules/tenant/main.tf` builds its tier business services and every
technical service from `var.tiers`, and `scripts/render-pagerduty-tenants.sh`
derives tiers from ACCOUNT records in the registry. Before vesting there are
no accounts, so an apply at design 13's step 3 creates exactly one thing: the
client-conversation business service, which is not keyed on tiers. The tier
services, the per-`app` technical services, the reserved `infra` service and
**all of the routing keys** come from a second apply after the accounts exist.

**The gap.** The saga's step 3 check keys on `pagerduty_business_service_id`,
which the first pass sets. A saga run would therefore mark step 3 complete and
never perform the second pass — leaving a tenant with alarms, no routing keys,
and a provisioning record that says PagerDuty is done. That is precisely the
failure design 13 put PagerDuty before the accounts to prevent, reintroduced
by the check that was supposed to confirm it had been prevented.

**Why the check looked right.** The comment on it reasons that every tenant has
exactly one conversations business service regardless of which tiers it runs,
so its presence is a reliable "this tenant has a PagerDuty footprint" — and a
paging service id would be absent for a client with no accounts yet, which is
a legitimate state. All true. The error is that a footprint is not the same as
a paging surface, and step 3 is responsible for the second.

**Fix.** The check needs to compare against the tenant's ACCOUNT records: a
tenant with N vested environments needs a routing key for each. `counts` is
already an output of the tenant module for roughly this purpose.
`scripts/onboard-client.sh` works around it by running the render twice as
separate numbered steps, which makes the second pass visible to an operator
but does nothing for the saga.

**Trigger:** before the saga is deployed (B-130). While onboarding runs from
the script the workaround holds; the moment the Lambda can advance a tenant on
its own, this becomes a tenant that reports provisioned and cannot page.

## B-133 · Truveon has no read access to raw logs, deliberately

**Deferred with a trigger** — Jamie, 2026-09-23.

The lightweight form of "all of a tenant's logs into Truveon" has three parts:
close the collection gaps, attest arrival (`log-custody`, B-134), and give
Truveon read access to the raw logs in place. The third is not built.

**Why not now.** Truveon's AWS account (852064978666, organization
`o-gnh5qyx6rs`) is outside this platform's organization (`o-61yvddd7d0`).
Read access means a cross-organization trust into the Log Archive account and a
written exception to every organization-scoped condition that protects it. That
is attack surface, and today nothing in Truveon would use it — it has no code
that reads raw logs. An unused trust path into the most sensitive account in
the estate is the wrong trade.

**Trigger to revive:** Truveon has code that reads raw logs (an investigation
view, an auditor's raw-record request), or a tenant contract requires Truveon
to present raw records. Then: a role in the Log Archive account assumable by a
named Truveon role, `s3:GetObject` scoped per tenant to
`AWSLogs/<org>/<account>/` for that tenant's accounts, plus Athena, and the
organization-policy exception recorded in `docs/deviations.md`.

## B-134 · Log custody: hourly evidence that each account's raw logs arrived

**Built 2026-09-23** — `evidence/src/custody.py`, category `log-custody`.

One evidence record per account, per source (CloudTrail, Config), per hour:
object count, bytes, first and last file, a digest of the listing, and for
CloudTrail whether every region's hourly digest file arrived. A missing digest
is a finding; a quiet hour is not. Runs in the Audit account with
`s3:ListBucket` on the two archive buckets and nothing else.

**What it does not cover yet:** VPC flow logs (B-012 — none are collected),
Route 53 Resolver query logs and CloudWatch Logs (not centralised). Each joins
as a source when its gap is closed; that is the next piece of this work.

**Worth doing when convenient:** declare a 60-minute cadence for each member
account's `log-custody` stream in Truveon's manifest apply, as is done for the
pipeline heartbeat. Truveon would then notice a stopped custody job per account
on its own. Not done now because it is a Truveon-side change and the platform
already detects the same thing through sequence gaps.

---

## B-135 · Three role-name shapes match an SCP exemption, and anyone can create them

**Observed** — 2026-09-29, `scripts/test-guardrails.sh` against Sandbox Canary.
Every SCP exempts principals by name. SCP 03's squatting deny protects one
shape, an unpathed `role/Platform*`. The harness subject (AdministratorAccess,
matching no exemption) created each of these with the developer boundary
attached, which is what a developer can do:

| Role | 01 | 04 | 06 | 07 |
|---|---|---|---|---|
| `/xrole/PlatformSquatPath` | held | **exempt** | **exempt** | **exempt** |
| `stacksets-exec-squat` | **exempt** | **exempt** | **exempt** | **exempt** |
| `OrganizationAccountAccessRoleSquat` | held | **exempt** | **exempt** | **exempt** |

Why each one gets through:
- 04–07 write the exemption as `arn:aws:*:*:*:*role/Platform*`. The leading
  wildcard absorbs a path that ends in `role/`.
- 04–07 exempt `OrganizationAccountAccessRole*` with a trailing wildcard.
- Nothing anywhere denies creating `stacksets-exec-*`.

The developer boundary denies none of the 04, 06 or 07 actions, so for a
bounded squatter the SCP is the only control, and the name defeats it. The 01
column holds for the pathed role only because SCPs see the path (B-001
correction).

Against the rest of 01, the boundary is what holds. It repeats most of 01's
denies, but not all of them: `access-analyzer:DeleteAnalyzer`, the Macie and
Inspector disables and `logs:PutRetentionPolicy` are in 01 only.
`stacksets-exec-squat` is exempt from 01 outright, so against those actions
nothing stops it.

**Fix** — the exemption rewrite:
- explicit per-SCP lists, as `arn:aws:iam::*:role/<Name>` with no leading
  wildcard;
- exact `OrganizationAccountAccessRole`, with no trailing wildcard;
- 03's squatting deny extended to `stacksets-exec-*`, `OrganizationAccountAccessRole*`
  and pathed roles, exempting the StackSets service-linked role
  (`/aws-service-role/member.org.stacksets.cloudformation.amazonaws.com/`),
  which is what creates the real `stacksets-exec-*` roles.

The pathed-role deny must also exempt `/aws-service-role/` and
`/aws-reserved/sso.amazonaws.com/`. Design 11 needs no pathed roles.

**Blocks Arc8** — these accounts would be the first developer-accessible
accounts in Members. Close before vesting.

**Built 2026-09-30, Sandbox only.** Exemptions are now named principals, each
list derived by `scripts/derive-scp-exemptions.py` from the templates, and each
entry justified against what the role actually does:
- 01–03 are new policy objects (`*-v2.json`) swapped onto Sandbox
  attach-then-detach. The originals are untouched on Members, Security and
  Infrastructure until 15.4 does the same swap there.
- 04–07 were edited in place. They are attached to Sandbox only.

Other changes:
- The patterns are exact `arn:aws:iam::*:role/<Name>`. The dead
  `assumed-role` variants are gone (B-001 correction).
- 03's squatting and modification denies now also cover `stacksets-exec-*`,
  `OrganizationAccountAccessRole*` and `TenantAppDeployer-*`.
- 01's log-group denies are scoped to platform log groups
  (`/aws/platform/*`, `/aws/lambda/platform-*`,
  `/aws/vendedlogs/states/platform-*`). A tenant's own application log groups
  are the tenant's, and the unscoped version would have broken every tenant
  stack that sets `RetentionInDays` once the app deployer lost its exemption.
- `PlatformDeployer` (15.3) is reserved in every list except
  `DenyQuorumExecutorCreationEvenByOperators`, which still exempts
  `stacksets-exec-*` only (B-125).

**No blanket pathed-role deny.** With exact patterns, a path cannot match any
exemption: `role/Platform*` does not match `role/x/PlatformY`. A pathed-role
deny would add nothing, and it would refuse the `/service-role/` roles that
AWS tooling creates. The harness keeps the pathed case and asserts that it is
exempt from nothing.

**Harness, Sandbox Canary, 2026-09-30:**
- all three squatting creations are refused, or permitted and inert in the
  pathed case;
- every exemption check on the pathed role holds;
- nothing that passed before regressed.

**Remaining** — 15.4 swaps 01–03 v2 onto the other OUs, then retires the
originals.

---

## B-136 · The tenant app deployer is exempt from the guardrails meant for it

**Observed** — 2026-09-29, same run. `change/00-appdeployer.yaml` names the
tenant CI role `PlatformAppDeployer-<app>`. The `Platform` prefix was chosen so
that SCP 03 would protect the role from tampering. It also puts the role inside
every SCP's `Platform*` exemption. A harness clone carrying
`PlatformAppDeployer-demo`'s live boundary and `Deploy` policy:
- listed Lambda functions in eu-west-1: exempt from 06;
- reached ECS validation on an untagged `CreateService`: exempt from 04.

That role serves a pipeline AltDigital does not control. `Deploy` grants
`lambda:*`, `ecs:*`, `elasticloadbalancing:*` and more on `*` with no region
condition, so the SCP was the only region control it had.

**Fix** — rename it to `TenantAppDeployer-<app>` so that it is exempt from
nothing, and keep it tamper-protected by adding the new pattern to 03 and to
the boundary's `NeverThePlatformIdentities`. Scope its compute to the approved
regions with `aws:RequestedRegion`.
`change/src/reconcile.py` matches the literal `PlatformAppDeployer` and must
change in the same commit, or every tenant deploy reconciles as an
unaccounted-for credential. Changing `RoleName` replaces the role, which
changes the ARN the tenant pipeline assumes.

**Blocks Arc8** — Arc8's pipeline would be the first real user of the role.

**Correction, 2026-09-30.** The ECS half of the evidence above is void. ECS
answers "TaskDefinition not found" before it authorizes. The non-exempt
harness subject, which SCP 04 certainly applies to, got the same answer. So
the check could not distinguish an exemption from a denial. The 06 half
(Lambda in eu-west-1) stands. The 04 exemption was real, but the proof for it
is the policy text, not that run. The harness now tests 04 with an untagged
`lambda:CreateFunction`, having first shown that Lambda authorizes before it
validates.

**Built 2026-09-30:**
- `change/00-appdeployer.yaml` names the role `TenantAppDeployer-<app>` (D-026),
  scopes its compute to the `AllowedRegions` parameter, and tags it
  `platform-layer: infrastructure`.
- SCP 03 v2 and the developer boundary protect `TenantAppDeployer-*` by name.
- `change/src/reconcile.py` recognises both names, anchored at the role-name
  boundary (`scripts/test-reconcile.py`).
- The harness clone takes the new name and is refused by 04 and 06.

**Redeployed 2026-09-30** — `platform-appdeployer-demo` in Sandbox Canary now
creates `TenantAppDeployer-demo`, with the operator's confirmation that nothing
used the old role. CloudFormation replaced the role and modified its boundary
and policy in place. The OIDC provider was untouched. Verified:
- the boundary is `PlatformAppDeployerBoundary-demo`;
- the compute statement carries the `aws:RequestedRegion` condition for the
  three approved regions;
- the role is tagged `platform-layer: infrastructure`.

**OIDC proven 2026-10-01.** A GitHub Actions run from
`AltDigitalOfficial/msp-demo-service`, branch `release`, assumed
`TenantAppDeployer-demo` under the RCP. CloudTrail recorded
`AssumeRoleWithWebIdentity` SUCCESS at 01:26:57Z, session `GitHubActions`.

Two things surfaced on the way:
- The trusted repository `altdigital/demo-service` never existed. The org is
  `AltDigitalOfficial`.
- GitHub sends the owner and repository IDs in the subject claim. B-142 covers
  what that means for onboarding.

Running the real event through the reconciler also found a bug that predates
the rename. A web-identity caller has no `arn`, so the assumption classified
as `deployer: unknown`. With no change record, it would have been reported as
"credentials nobody has accounted for", the opposite of what it was. Fixed in
`classify_event`: the assumption is classified by the role assumed. Tested
with the real event's shape.

The deployed reconciliation Lambda needs republishing (`publish-change.sh`)
before Arc8's first deploy.

---

## B-137 · The harness probe scheduled its own KMS fixture for deletion — CLOSED

**Observed** — 2026-09-29. The Canary fixture `alias/guardrail-harness-fixture`
is `PendingDeletion`, due on 2026-10-20. CloudTrail shows `ScheduleKeyDeletion`
by session `gr-probe` on 2026-09-20 at 20:53:27 and 21:01:25 (MDT), which is
`PlatformHarnessProbe`, the exempt differential principal.

**Cause** — `check()` in `yes` mode re-runs a denied call as the probe to
prove that the SCP made the difference. For the KMS check the resource is
real, so the probe's call succeeded and did what the check exists to prevent.
The same mode was in use for three other real-resource checks. For those, the
probe path never ran, only because the services named the SCP in their
denials:
- the developer-boundary `CreatePolicyVersion` check, whose probe run would
  have published an `Allow *` default version and stripped every deny;
- the snapshot share, whose probe run would have shared a harness snapshot
  with an external account id;
- the platform-tag change.

The Canary boundary is intact: v1 only, and every `CreatePolicyVersion` in
CloudTrail is the subject's.

**Fixed 2026-09-29** — all four checks now use `real` mode. A real resource
plus AdministratorAccess means a denial can only be an SCP, so no probe run is
needed. `check()`'s documentation now says `yes` must never target a real
resource.

**Fixture restored 2026-09-30** — `kms:CancelKeyDeletion` then `kms:EnableKey`,
by the operator's decision. State `Enabled`, no deletion date. The harness
still warns if it ever finds the fixture in any other state. B-137 closed.

---

## B-138 · Nothing limits which identity providers an account may trust

**Observed** — 2026-09-29, while fixing the RCP (D-003 correction). With
`sts:AssumeRoleWithSAML` and `sts:AssumeRoleWithWebIdentity` out of the RCP,
federation into a role is limited only by the role's trust policy and by which
SAML and OIDC providers exist in the account. A local administrator can
register a provider they control, such as their own OIDC issuer, create a role
that trusts it, and assume that role from outside the Organization. That is
the same exfiltration path design 11 names for cross-account trust. The RCP
never really closed it, because it also denied every legitimate federated
sign-in.

**Fix** — an SCP, since SCPs see the request and this is a request-side
control. Deny `iam:CreateOpenIDConnectProvider`,
`iam:UpdateOpenIDConnectProviderThumbprint`,
`iam:AddClientIDToOpenIDConnectProvider`, `iam:CreateSAMLProvider` and
`iam:UpdateSAMLProvider` to every principal except the platform's deployment
principals. Those create the GitHub provider the app deployer trusts and, via
Identity Center, the SAML provider. The deny belongs with 07 (bypasses) and
is written against the explicit exemption lists from B-135, not against
`Platform*`. Harness case: the subject creates an OIDC provider and is denied.

**Blocks Arc8** — with B-135. Arc8's accounts are the first where someone
other than AltDigital holds IAM rights.

**Built 2026-10-01, Sandbox.** SCP 07 statement `NoUnapprovedIdentityProviders`
denies these to everyone except the deployment principals
(`OrganizationAccountAccessRole`, `stacksets-exec-*`, `PlatformDeployer`) and
service-linked roles:
- `iam:CreateOpenIDConnectProvider`
- `iam:UpdateOpenIDConnectProviderThumbprint`
- `iam:AddClientIDToOpenIDConnectProvider`
- `iam:CreateSAMLProvider`
- `iam:UpdateSAMLProvider`

There are only two legitimate creators. `change/00-appdeployer.yaml` creates
the GitHub provider, and is always deployed by a deployment principal.
Identity Center creates `AWSSSO_…_DO_NOT_DELETE` through its service-linked
role, which no SCP constrains.

Deletes are deliberately not covered. Deleting a provider removes trust
rather than adding it.

Harness: the subject's `CreateOpenIDConnectProvider` was denied with the SCP
named (31 passed, 0 failed). The case runs in `no` mode, because a
differential call that succeeded would create a real provider. If the
guardrail is ever absent, the case deletes what it created.

**Remaining** — reaches Members with 07 in 15.4b.

---

## B-139 · Account-level security settings were set nowhere

**Observed** — 2026-09-29, from Security Hub in Sandbox Exercise (us-east-2),
and confirmed in all three allowed regions of both Sandbox accounts by
`scripts/apply-account-settings.sh --dry-run`. None of these was set, and
nothing in the repository set them:

| Setting | Finding |
|---|---|
| EBS encryption by default | EC2.7 |
| EBS snapshot public sharing blocked | EC2.182 |
| SSM document public sharing disabled | SSM.7 (critical) |
| Default security groups empty (every VPC, default VPCs included) | EC2.2, CIS 4.3 (high) |
| Account-level S3 Block Public Access | S3.1 |
| IAM password policy | IAM.7, CIS 1.5–1.11 |
| A role for AWS Support incidents | CIS 1.20 |

Only three of these have CloudFormation resources, so the whole list lives in
one script rather than being split between a template and a script.

**Built 2026-09-29** — `scripts/apply-account-settings.sh`. It is idempotent,
supports `--dry-run`, and has a read-only `--verify` that exits non-zero on
any gap. It refuses, rather than breaking something:
- S3 Block Public Access, if any bucket is public;
- emptying a default security group, if any network interface uses it.

Every rule it removes is printed first, so the change can be reversed.

It is called from:
- `vest-client.sh`, step 6a, which verifies and stops vesting on a gap;
- `create-platform-account.sh`.

Applied and verified on Sandbox Exercise and Sandbox Canary. There were no
refusals: no interface used a default group, and no bucket was public.

**Not yet applied** — the Security and Infrastructure OU accounts (Log
Archive, Audit, Forensics, Backup, Infra Tooling). Same script, but each is a
change outside Sandbox, so each needs explicit confirmation.

**Open** — the security alternate contact (Account.1). For member accounts it
is set from the management account with `aws account put-alternate-contact
--account-id`, which needs Organizations trusted access for AWS Account
Management. That is a management-account change. The contact values live in
the gitignored `config/contacts.env`.

**Also open** — nothing re-checks these after vesting. `--verify` is the
check, and the verification sweep should run it per account. Security Hub
reports the same drift in the meantime, via EC2.7, S3.1 and the rest.

---

## B-140 · The Organization's account quota is 10, and Arc8 needs 11

**Observed** — 2026-09-30. Service Quotas, AWS Organizations,
`L-E619E033` "Maximum number of accounts" = **10**. There are 8 active
accounts: management plus seven platform accounts. The Arc8 example
questionnaire vests dev, test and prod, which makes 11. No increase has ever
been requested.

**Why it matters** — `vest-client.sh` creates accounts one at a time. At the
cap it would have created two, failed on the third, and left two accounts that
can never be deleted, with vesting half done.

**Guarded 2026-09-30** — `vest-client.sh` step 3a reads the quota, counts
active accounts plus the accounts this vesting would create, and refuses to
start if they do not fit. A dry run warns instead of refusing.

**Requested 2026-09-30** — Jamie approved 30, sized for Arc8's 3–4 accounts,
the FinOps account (15.9), Avergent, and the tenants after it. Service Quotas
request `f56961bfd3a04b9ea693ca52f0381f40wNyLviE5`, raised from the management
account in us-east-1, status PENDING, no support case yet. Check with:

```
aws service-quotas list-requested-service-quota-change-history-by-quota \
  --service-code organizations --quota-code L-E619E033 --region us-east-1 \
  --profile msp-mgmt
```

At the time of the request: 8 active accounts, and both Sandbox accounts in
use (the canary smoke test and the guardrail harness), so none could be
retired. Closing one would not have helped: a closed account stays SUSPENDED
for 90 days and still counts, and the root SCP denies closing.

**Blocks Arc8 until approved.**

---

## B-141 · Nothing patches, so nothing can wake an environment to patch it

**Observed** — 2026-09-30, OEight dry run. Arc8's developers work India hours
and want non-production maintenance after their day ends, when the
scheduler has switched dev and test off. The decision (Jamie, 2026-09-30) is
to **wake, patch, shut down**. Looking for the patching to wake for found none:
no SSM maintenance window, no patch baseline and no `AWS-RunPatchBaseline`
association in any template. Design 09's Patch Manager section was never built.

**Three layers, in order:**

| Layer | What | Notes |
|---|---|---|
| 1. Patching | Patch baseline per OS family; an SSM maintenance window per account, using the prod window for prod and the non-prod window for the rest | Scan only under service model A (evidence, no change). Scan and install under B and C. |
| 2. Wake for maintenance | A third scheduler state, `maintenance`, beside `up` and `down`: turn up from the manifest, patch, validate, turn down, write a fresh manifest | Alarms stay suppressed and canaries paused; the digest shows it as maintenance, not use |
| 3. The check | For a scheduled environment, a non-prod window outside the HOOP becomes a derived "wake required", not an error | Only once layer 2 exists |

Most of layer 2 is already built in `scheduling/src/hoop.py`: the
manifest-driven turnup, the full-set turnup validation and the state
publisher. What is new is the state, the trigger at the window's start, and
failure handling.

**Failure handling, agreed 2026-09-30** — every branch notifies somebody:
- the turnup fails: an immediate ticket, the same as a scheduled turnup failure;
- the patch fails: shut down anyway, and a ticket naming the instances;
- the window runs out mid-patch: **leave it running**, page nobody, and raise
  a ticket. Never stop an instance halfway through a patch.

**What was done instead, for now** — `derive-vesting.py` reports a non-prod
window outside the HOOP as a **warning** citing this entry, not an error.
That is safe only because nothing patches: there is no job to fail silently.

**Whoever builds layer 1 must build layer 2 with it, or turn the warning back
into an error in the same change.** Building patching alone brings back
exactly the silent failure the check was written for: the window opens, the
instances are stopped, and it closes having patched nothing.

**Build when** — Arc8's developers recommend a compute model and it includes
EC2, or any tenant runs EC2 instances in a scheduled environment. On Fargate
or Lambda there is no OS to patch, and only RDS would be affected. RDS has
its own maintenance window, and AWS restarts a stopped database after seven
days.

---

## B-142 · The deploy role's trusted OIDC subject must be the exact claim, and the questionnaire cannot produce it

**Observed** — 2026-10-01, proving GitHub OIDC federation into
`TenantAppDeployer-demo` (B-136). The role trusted
`repo:AltDigitalOfficial/msp-demo-service:ref:refs/heads/release`. GitHub sent
`repo:AltDigitalOfficial@188894052/msp-demo-service@1398892324:ref:refs/heads/release`.

That is GitHub's subject format with the owner and repository IDs appended. It
depends on the organisation or repository settings, not on anything the
platform controls. The `StringLike` condition did not match, and STS refused:
"Not authorized to perform sts:AssumeRoleWithWebIdentity". That message is
identical to an RCP or SCP denial.

CloudTrail's `userIdentity.userName` on the failed event carried the exact
claim, which is how this was diagnosed in minutes rather than hours. Pinned to
the exact claim, the run succeeded.

The RCP was not involved. With this, the D-003 correction (federation removed
from the RCP) is proven for OIDC as well as SAML.

**Why it matters** — the subject condition is the whole control on the deploy
role (`change/00-appdeployer.yaml` says so). It has two ways to go wrong:
- too loose, and any repository on GitHub can deploy;
- built from names when GitHub sends IDs, and the tenant's first deploy fails
  with an error that points at the wrong layer.

Questionnaire 8.1 collects only `cicd_tooling`. There is no field for the
repository, the branch or environment that may deploy, or the subject format.

**The ID form is the better one to trust.** A renamed repository, or a deleted
one whose name someone else re-creates, cannot inherit the trust, because the
IDs do not carry over. So where GitHub emits IDs, pin to them, and never widen
the pattern to accept both forms.

**Fix** — at onboarding, obtain the exact claim rather than composing it:
1. Collect the repository (owner/name) and the deploying branch or environment
   in the questionnaire.
2. Read the subject format from GitHub:
   `GET /repos/{owner}/{repo}/actions/oidc/customization/sub` returns whether
   the repository uses a custom claim template. The owner and repository IDs
   come from `GET /repos/{owner}/{repo}`.
3. Or, more simply and as done here, have the tenant run the one-step
   `oidc-proof` workflow once, and read the claim from the denied event in
   CloudTrail before pinning it.
4. Make one successful `AssumeRoleWithWebIdentity` a vesting completion check.
   Until a tenant's pipeline has assumed its role once, the control point
   design 08 depends on is unproven.

**Related** — the Phase 15 prompts name the GitHub organisation `altdigital`.
It is `AltDigitalOfficial`, and the subject match is case-sensitive. 15.3's
pipeline trust must use the real name, and the ID form if the organisation
emits it.

**Blocks Arc8** — Arc8's first deploy would hit it.

---

## B-143 · The developer boundary was a hand-deployed stack in one account, and vesting did not check for it

**Observed** — 2026-09-30. `PlatformDeveloperBoundary` existed only in Sandbox
Canary, as a plain stack (`platform-developer-boundary`) deployed through
`deploy-to-account.sh`. Sandbox Exercise had none, which is why the harness
could not run there. No StackSet carried it, and `vest-client.sh`'s
baseline-reachability check listed four StackSets without it.

A vested Arc8 account would therefore have come up with no boundary:
- Before SCP 05 reached Members, developers could create roles with no
  boundary at all.
- After it, SCP 05 would refuse every role a developer or the tenant pipeline
  tried to create, because 05 requires exactly that boundary. That fails safe,
  but it is an outage.

**Built 2026-10-01** — `platform-developer-boundary` is a service-managed
StackSet, operated from Infra Tooling as delegated admin, targeting Sandbox.
Canary's existing stack was IMPORTED rather than recreated
(`import-stacks-to-stack-set`), so the policy was never deleted and no role
lost its boundary. Exercise got a fresh instance. Both accounts now carry the
current boundary, including `TenantAppDeployer-*` and the
`OrganizationAccountAccessRole*` variants (B-135, B-136).
`vest-client.sh` lists the StackSet in its baseline check, and the management
account can read it there with `--call-as SELF`.

**Import, as proven here** — worth having written down, because D-012 assumed
it was hard:
1. Create the StackSet from the template the stack is RUNNING, not the repo's
   newer one. An import that would change anything is refused.
2. Match the stack's STACK-LEVEL TAGS on the StackSet. `cfn_deploy` adds
   `platform-managed=true` to every stack it deploys, and those tags reach the
   resources. The first import failed, safely, because the StackSet had no tags
   and the import would have removed the policy's.
3. If an import fails, the StackSet keeps an `INOPERABLE` record. Remove it
   with `delete-stack-instances --retain-stacks`. Without `--retain-stacks`
   that call deletes the real stack.
4. Then update the StackSet to the repo template as normal.
   `deploy-stackset.sh` skips creating instances when the OU is already a
   target, so an account in that OU without an instance (Exercise here) needs
   `create-stack-instances` with an account filter.

**Remaining** — the StackSet targets Sandbox only. It joins the other baseline
StackSets when 15.4b widens them to Members.

---

## B-144 · A publish deployed another session's uncommitted code as a live control — guarded

**Happened** — 2026-10-01. `publish-change.sh` packaged `change/src` from the
working tree while another session had uncommitted, in-progress edits to
`reconcile.py` and `jira.py`. The package was deployed to the Audit account's
`platform-change` stack as the live change-reconciliation control. Its hash
(`change-832dfb0e…`) matched neither the commit (`…29d35ec6…`) nor the later
working tree (`…1bb6eb0c…`), so the deployed code was a state that never
existed in history.

**Contained** — about 20 minutes later the stack was redeployed on the
previous package (`change-9a9c7e7bde555b37.zip`), the known-good state from
before. The reconciler logged no errors in that window. The fix that prompted
the publish (B-136, WebIdentity classification) is therefore NOT live yet. It
goes out once the other session's reconciler work is committed.

**Guarded** — `require_committed` in `lib/common.sh`. Every `publish-*.sh`
calls it, naming the directories it packages:
- each script's `*/src`;
- `design/06a-alarm-specification.yaml` for instrumentation, response and
  router;
- `scripts/lib/extract_event_specs.py` for router.

It refuses on any modified or untracked file there, and it fails loudly if git
itself cannot run. There is no override flag: a package deployed as a platform
control should be a commit someone can name. Proved both ways:
- `change/src`, dirty, is refused and its files are named;
- `telemetry/src`, clean, passes the guard.

**Lesson** — parallel sessions share one working tree. Anything that ships
files from disk, not from a commit, ships whatever another session is halfway
through. Staging explicitly protects commits; it does nothing for publishes.

---

## B-145 · publish-telemetry.sh has refused to publish since 2026-09-21 — CLOSED

**Observed** — 2026-10-01, testing B-144's guard. `publish-telemetry.sh`'s own
self-check fails on committed code: "gap.py has lost a control: 3 scenario(s)
alarm; design 11's table has exactly two." The last change to `telemetry/src`
was a8d6dbf, 2026-09-21. Either that commit added a third scenario without
updating the check, or the third scenario contradicts design 11.

**Why it matters** — the telemetry-gap package cannot be republished. Whatever
is deployed is frozen at its last successful publish, and nothing reports it,
because only a publish runs the check.

**Next** — read design 11's scenario table against `SCENARIOS` in
`telemetry/src/gap.py`. Then fix whichever is wrong. Do not loosen the check
to make it pass.

**Fixed 2026-10-02: the check was wrong, not the code.** The third alarming
row is `instance-stopped-off-schedule`, added on purpose by a8d6dbf for design
14 / prompt 13.4. It splits design 11's "stopped" row by HOOP state, so that a
stop the platform did not perform is reported instead of looking like a
turndown. The check counted design 11's two rows only. It now compares the
exact SET of alarming scenarios by name, each with its source. That is
stricter than the count, which a swap of one row for another would have
passed. `publish-telemetry.sh --dry-run` passes.

---

## B-146 · The incident readout cannot assume the responder role it is written to use — CLOSED

**Observed** — 2026-10-01T19:07Z, during the post-swap watch for 15.4a.
`PlatformIncidentReadout` (Audit) was refused `sts:AssumeRole` on
`PlatformSecurityResponder` in Sandbox Canary, 8 times. An incident had been
opened by the guardrail harness run.

**Cause** — the responder's trust policy names `PlatformContainment` only
(`containment/00-responder-role.yaml`, `Principal: AWS: !Ref
ContainmentRoleArn`). The readout's own policy grants
`sts:AssumeRole` on the responder (`containment/10-containment.yaml`,
`AssumeTheResponderRole`), but the target never trusted it. So the readout
has never been able to read a member account.

This is not the SCP swap:
- the message carries no "explicit deny" clause, which means no policy
  ALLOWED the call;
- `PlatformContainment`, in the same Audit account under the same v2 SCPs,
  assumed the same role 16 times in that hour without a failure.

The 24-hour baseline missed it only because no incident had been opened in
that window.

**Why it matters** — design 07's readout is how an incident becomes something
a human can act on. A readout that cannot reach the account produces a readout
with nothing in it, and is quiet about why. The failure is only in CloudTrail.

**Fix — a decision, not a one-liner.** Either the responder trusts the readout
too, or the readout gets its own read-only role in members accounts. The
second keeps the responder (which can isolate and revoke) trusted by exactly
one principal. That is the reason to prefer it, and it is a containment design
choice. Do not just widen the responder's trust to make this pass.

**Closed 2026-10-02 — the separate role, by the operator's decision.**
`PlatformIncidentReader` (`containment/00-responder-role.yaml`, condition
`HasReadout`) trusts exactly `PlatformIncidentReadout`. It holds
`cloudtrail:LookupEvents` and the four IAM policy-list calls, and nothing that
writes. It needs no SCP exemption. The responder's trust is unchanged:
containment only.
- It was rolled out through `platform-security-responder` to both Sandbox
  accounts and both Arc8 accounts, all `CURRENT`.
- The readout and its role policy point at the reader (620f97b). Containment
  was redeployed in Audit on `containment-40c253597dcb006d`.

That redeploy also shipped d752a46 (the clock can record a notification as
made) and fd11f14 (B12's two-person rule). Both were decided 2026-09-22 and
had never been deployed, because the four functions share one package.
Rollback is `containment-988c583636b3913a`.

Proof: a guardrail harness run on Canary opened an incident.
- Declaration ran 4 times, containment 24, the readout 4, the clock 2, with
  zero errors.
- CloudTrail shows `PlatformIncidentReadout` assuming `PlatformIncidentReader`
  6 times, all SUCCESS: `readout-trail` ×3 and `readout-reach` ×3. It was
  refused 8 times out of 8 before.

---

## B-147 · Every account carries the same Security Hub findings, most of them the platform's own

**Observed** — 2026-10-01, reading Security Hub from Audit after the first
tenant vesting (Arc8 dev `624606539840`, test `904883674541`). Sandbox
Exercise `801093629821` and both new accounts show near-identical failed
findings: 2 CRITICAL, 2 HIGH, 15–17 LOW, 39–55 MEDIUM. A tenant's security
view therefore opens on findings the PLATFORM causes, which buries anything the
tenant causes and trains people to ignore the critical count.

**For phase 15.6** (AWS Config everywhere, Security Hub central configuration),
which owns the templates and the central configuration involved:

| Finding | Count / where | Cause | Proposed fix |
|---|---|---|---|
| **Config.1** (FSBP Config.1 and CIS 2.5) | CRITICAL ×2, every account; Sandbox since 2026-09-19 | `baseline/10-config-recorder.yaml` gives the recorder a custom role (`ConfigRecorderRole`); Config.1 requires the service-linked role `AWSServiceRoleForConfig` | Record with the service-linked role. Confirm nothing depends on the custom role's extra permissions first |
| **CIS 3.1–3.14** (CloudWatch.1–.14) | LOW ×13–15, every account | Each expects a CloudWatch Logs metric filter and alarm on the account's own trail. The organization trail does not deliver to per-account CloudWatch Logs | Confirm the central event rules (06a CloudTrail event alarms) cover each control, record the mapping, then disable these controls for member accounts in Security Hub central configuration |
| **CloudTrail.1** (and CIS 2.1) | HIGH ×2, Arc8 accounts | Last updated 21:37 during turnup, before the baseline settled. Periodic check | Re-check after its next evaluation before acting; if it persists, the organization trail's multi-Region / read-and-write settings are the place to look |
| **IAM.1** | HIGH ×2, Sandbox Exercise only, since 2026-10-01 01:41 | Probably guardrail-harness fixtures (full-admin policy) from overnight testing | Confirm with whoever ran the harness; fixtures should be cleaned up or the finding suppressed in Sandbox with a reason |
| **Mediums** | 39–55 per account | Mostly baseline design: CloudFormation.3/.4 on StackSet stacks, recommended VPC endpoints (EC2.10/.55–.60), flow logs (EC2.6, B-012), KMS.2, DynamoDB.6, EC2.172, SSM.6, Account.1 (B-139's open item) | Triage once: fix, or suppress per control with a recorded reason |

**Why it matters for onboarding without Jamie** — a partner opening their
tenant's security view should see their own findings. Today every new account
opens at CRITICAL because of how the platform itself is built.

**Build when** — phase 15.6, which is already changing the Config recorder and
building Security Hub central configuration.

**Update 2026-10-02 (the session that built the boundary StackSet and ran
the harness):**
- **IAM.1 is not harness fixtures.** Every IAM.1 finding is on
  `policy/PlatformDeveloperBoundary`. In Exercise it appeared at 01:41Z, when
  the boundary StackSet created the policy there (B-143). In Canary it
  appeared at 19:08Z, when the harness attached it to bounded roles. Its
  `CeilingNotGrant` statement is `Allow * on *` by design: a permissions
  boundary caps a role and grants nothing. Every tenant account will raise
  IAM.1 once a developer creates a bounded role. Fix: a Security Hub
  suppression scoped to that one policy ARN, with this reason recorded. Do not
  rewrite the boundary to dodge the check.
- **CloudTrail.1 on Arc8 is most likely a turnup artefact.** The last
  evaluation was 2026-10-01T21:36–21:37Z, during turnup, and this is a
  periodic control (about 24h). The Sandbox accounts, under the same
  organization trail, do not fail it. Re-check after the next evaluation, as
  above.
- **IAM.1 suppressed, 2026-10-02.** `security/30-securityhub-suppressions.yaml`
  is stack `platform-securityhub-suppressions` in Audit. It holds automation
  rule `platform-suppress-iam1-developer-boundary`, matching control `IAM.1`
  AND a resource containing `:policy/PlatformDeveloperBoundary`. The rule sets
  `SUPPRESSED` and writes the reason into each finding's note.
  - The 8 existing findings, which predate the rule, were given the same
    status and note by `batch-update-findings`. No other IAM.1 finding
    existed.
  - The rule's tags are applied by `securityhub tag-resource`, because the
    CloudFormation schema for this type rejects hyphenated tag keys.
  - This template is where further suppressions go, one rule per cause.

---

## B-149 · Developers could rewrite or switch off the platform's monitoring

**Observed** — 2026-10-01, defining the Arc8 developer and read-only roles.
design 11 Layer 1 lists "platform-managed alarms and SSM associations" among
the things no principal in a member account may touch. Neither the SCPs nor
the developer boundary carried them. `DeveloperDev` is PowerUserAccess, so in
a dev account a developer could:

- `lambda:UpdateFunctionCode` on `platform-response`, which then runs their
  code as `PlatformResponseAutomation`. That is a control removed *and* a
  privilege escalation.
- disable `platform-alarm-drift-on-delete`, then delete `platform-auto-*`
  alarms, and the drift sweep that would have recreated them never fires.
- `ssm:PutParameter /platform/response/enabled false`, which is the response
  kill switch.
- delete the `platform-install-cloudwatch-agent` association, the
  `platform-remediation-history` records, or a `StackSet-platform-*` stack.
- change the baseline route tables, security groups and DNS firewall, which
  is egress escaping its declared profile.

None of this needs malice. A developer cleaning up "unused" Lambdas in their
own dev account does the first one.

**Built 2026-10-01:**
- `policies/scp/08-protect-platform-monitoring.json` covers alarms, EventBridge
  rules, Lambda functions, `/platform/*` parameters, `platform-*` tables and
  `platform-*` stacks, all keyed on the `platform-` name prefix, which every
  baseline monitoring resource carries. Each statement exempts the
  deployment principals plus only the platform role that writes to that
  resource (`PlatformInstrumentation`, `PlatformHoopScheduler`,
  `PlatformResponseAutomation`).
- `policies/scp/07-deny-bypass.json` adds `ProtectBaselineNetwork` and
  `ProtectResolverTags`, keyed on `platform-managed=true`. 07 rather than 08,
  for two reasons: design 11's bypass table is what 07 implements, and one
  SCP holding all of it came to 5,841 bytes against a 5,120 limit.
- `baseline/80-developer-boundary.yaml` mirrors 08, so a role a developer
  creates cannot do what the developer cannot.
- `DeveloperDev` denies claiming or stripping `platform-layer: infrastructure`
  (B-127). This lives in the permission set rather than an SCP, so it needs no
  list of platform-role exemptions.

**State Manager associations — NOT accepted, must be narrowed before Members.**
As committed, SCP 08 and the boundary deny `DeleteAssociation` and
`UpdateAssociation` on EVERY association, because association ARNs carry an
id rather than a name. Jamie questioned it on 2026-10-01, and the cost turned
out larger than it was first written:

- The SCP binds every principal in the account, **including the tenant
  pipeline**. Any CloudFormation, CDK or Terraform stack containing an
  association can create it but can never update or delete it. A schedule
  change fails the stack update, and a dev teardown ends in `DELETE_FAILED`.
- Configuration management through State Manager (Ansible, package installs,
  config pushes) cannot be iterated. Each fix becomes a new association and
  leaves the broken one behind.
- Quick Setup, Quick Setup patching, Distributor and Default Host Management
  all create associations nobody could then modify.
- Close to zero impact on Arc8 if it is ECS/Fargate. The pain lands on the
  first EC2 tenant, or on any pipeline that includes an association.

**The premise was half wrong.** The IAM authorization data (iann0036
iam-dataset, which mirrors the Service Authorization Reference, read
2026-10-01; not yet confirmed live) lists `aws:ResourceTag/${TagKey}` for the
`association` resource on both `DeleteAssociation` and `UpdateAssociation`.
So a tag-scoped deny is possible. The obstacle is different:
`platform-install-cloudwatch-agent` in `baseline/50-instrumentation.yaml`
carries no tags. It *targets* `tag:platform-managed`, but is not tagged
itself. It is also unconfirmed whether `AWS::SSM::Association` takes a `Tags`
property in CloudFormation.

**Next, in Sandbox, before any Members deploy:**
1. Tag the platform association `platform-managed=true`, in the template if
   CloudFormation supports it, otherwise post-deploy with
   `ssm add-tags-to-resource --resource-type Association`.
2. Narrow the deny in SCP 08 and the boundary to
   `aws:ResourceTag/platform-managed = true`.
3. Deny `ssm:AddTagsToResource` / `RemoveTagsFromResource` carrying the
   `platform-managed` key, the pattern SCP 03 uses for EC2. Otherwise untag
   and then delete is the bypass.
4. Make the harness prove both directions: the test admin cannot delete the
   platform association, and can delete one it created.

If the tag condition does not evaluate in practice, drop the prevention and
alarm instead on CloudTrail `DeleteAssociation`/`UpdateAssociation` against
the platform association's id. That is weaker, and it breaks nobody's
pipeline.

**Known costs, accepted:**
- **The `platform-` prefix is reserved.** A developer stack or function named
  `platform-*` is refused, just as SCP 03 reserves `Platform*` roles.
- **`lambda:InvokeFunction` is not denied.** Invoking `platform-response`
  with a crafted event could make it isolate a resource in the caller's own
  account. Blocking that safely needs the full list of cross-account callers,
  which this change did not establish. *Trigger:* the next change to
  containment's invocation path.
- **`ec2:ModifySecurityGroupRules`** authorises against the security group
  rule as well as the group. Rules do not inherit the group's tags, so this
  one action may not be caught. Authorize and Revoke are.

**Status 2026-10-03, 00:01Z — DONE on Sandbox and Members.** SCP 08 is
attached to Members (9 of the OU's 10 SCPs). Between 15:15Z and 23:52Z on
10-02, under 08 on Sandbox, `PlatformInstrumentation` created alarms and
`PlatformHoopScheduler` wrote `/platform/hoop/*` 37 times, with zero denials
for either. That is the evidence the 15:40Z watch lacked. The platform agent
association in Arc8 dev `624606539840` and test `904883674541` was tagged
before the attach. No denials in Arc8 in the first minutes after it. The
permission sets (TenantReadOnly, DeveloperDev denies) were deployed by Jamie
the same day.

The harness has not been run in an Arc8 account. It creates roles and trips
containment, which pages under the tenant's PagerDuty services; run it with
`--no-kms-fixture` in a planned window if a tenant-side proof is wanted. The
same policy object is proven on Sandbox.

Unrelated, seen during the watch: `PlatformTelemetryReader` is refused
`ssm:GetParameter` on `/platform/hoop/state` in Sandbox Canary from 23:44Z,
33 times, "no identity-based policy allows". An IAM gap in the telemetry
reader role, not an SCP. Belongs with the B-145 telemetry work.

**Status 2026-10-02, 15:40Z — Sandbox done, Members and permission sets not.**

| Step | State |
|---|---|
| SCP 08 created (`p-3co5wuz8`), attached to Sandbox | live 15:20Z |
| `platform-developer-boundary` StackSet updated | live, all four instances CURRENT. Note the StackSet already targets Arc8 dev and test, so this reached them too: update-stack-set cannot be limited to one OU through `deploy-stackset.sh` |
| SCP 07 updated (`p-lilqdrua`) | live 15:31Z, in Sandbox **and Members** (one object) |
| Harness, Sandbox Canary | 42 passed, 0 failed, 3 ambiguous. Every 08 check passes, including both association directions. Ambiguous: 07/ProtectResolverTags (Canary has no DNS firewall), and the two long-standing 03 checks |
| Exempted roles, real ARNs | every exempted role present in Canary, Arc8 dev and Arc8 test matches its pattern; none carries an IAM path. `PlatformHoopScheduler` is absent from Arc8 dev and test, so nothing writes HOOP state there yet |
| CloudTrail, 15:15Z onward | zero denials outside the harness in all four accounts, **and also zero platform-role writes**, so the silence proves nothing yet. Re-run after the hourly drift schedule and some instrumentation activity |
| Arc8 association tags, 08 to Members | **not done** |
| `platform-developer-access` stack (TenantReadOnly, DeveloperDev denies) | **not done** |

The session was stopped twice by its permission classifier: once deleting a
Canary alarm to exercise the drift path (not one of the approved commands),
and once reading the permission-set stack. Both are left to the operator.

**Status 2026-10-02 (earlier).** The association deny is narrowed (`ca30634`), and the
harness carries 08 and 07 assertions. The platform association in Sandbox
Canary `754280127660` and Sandbox Exercise `801093629821` was tagged
`platform-managed=true` and `platform-layer=infrastructure` directly, through
OrganizationAccountAccessRole. Nothing else is live. The Claude Code session
doing this was stopped at the first SCP apply by its own permission
classifier, correctly, so every remaining step is an operator command:

```
# Sandbox
scripts/deploy-policies.sh --only 08-protect-platform-monitoring.json --attach
scripts/deploy-stackset.sh --name platform-developer-boundary \
  --template baseline/80-developer-boundary.yaml --ou sandbox
scripts/test-guardrails.sh --account 754280127660
#   expect the 07/ProtectBaselineNetwork and 07/ProtectResolverTags checks
#   to FAIL here: 07's new statements are not deployed yet
scripts/deploy-policies.sh --only 07-deny-bypass.json --confirm-production
#   07 is ONE object attached to Sandbox and Members: this is live in Arc8 at once
scripts/test-guardrails.sh --account 754280127660      # now clean

# Watch an hour, then: CloudTrail in Canary, errorCode AccessDenied, from any
# Platform* role. The exemption lists were derived, not observed.

# Members
#   tag the platform association in Arc8 dev 624606539840 and test 904883674541
#   (same add-tags-to-resource call as Sandbox), then:
#   attachments.tsv: 08 -> sandbox,members
scripts/deploy-policies.sh --only 08-protect-platform-monitoring.json --attach --confirm-production
#   and widen the boundary StackSet to Members when 15.4b does (B-143)

# Permission sets (management account, stack platform-developer-access)
#   identity/10-developer-access.yaml: TenantReadOnly and the DeveloperDev
#   layer-tag denies
```

`baseline/50-instrumentation.yaml` now tags the association. It deploys with
the next instrumentation publish rather than separately, because
update-stack-set redeploys every instance.

**Original plan, kept for the reasoning.** 08 is attached to Sandbox only,
and the 07 and boundary changes are not deployed. Arc8 dev and test are
vested, so Members needs this before the Arc8 developers get access:
1. `scripts/deploy-policies.sh`, Sandbox. Then `scripts/test-guardrails.sh`
   with an assertion added that `GuardrailTestAdmin` cannot
   `UpdateFunctionCode` on `platform-response` or `PutParameter`
   `/platform/response/enabled`.
2. Re-run the instrumentation, HOOP and response paths in Sandbox and watch for
   AccessDenied from the exempted roles. Each exemption list was derived by
   reading the templates, not by watching a live run.
3. Add `members` to 08 in `attachments.tsv`, deploy 07 and 08 with
   `--confirm-production`, and update the `platform-developer-boundary`
   StackSet.

---

## B-148 · The router has never recognised evidence-completeness or telemetry-gap events, and Arc8's turnup multiplied them by thirteen

**Observed** — 2026-10-02, reading the router's log after a reconciliation
run. `platform-router` sends `evidence-completeness` and `telemetry-gap`
events to the review queue with reason "unrecognised event shape". Hourly
counts:

| Period | Count per hour |
|---|---|
| Before Arc8 (09-30 and 10-01, same hours) | about 30 |
| 10-01, 21:00Z hour (Arc8 joined 21:32Z) | 664 |
| 10-01, 22:00Z hour | 1,394 |
| Since | about 400 |

So the router has never had a shape for these two event types: they have
always fallen through to review. A new account multiplies them, presumably
completeness gaps for accounts that have not emitted yet. 248c29a ("turnup
noise") addressed part of turnup noise, not this.

**Why it matters** — nothing pages. But `platform-review-queue` has no
consumer and keeps messages for 14 days. A real item sent to review is now
one message in thousands, so the review queue as a control is drowned. Every
new tenant makes it worse.

**Fix** — give the router a shape for both event types, and route them by
tier like everything else, or to the digest. The emitter still never
escalates (CLAUDE.md). Then confirm the per-hour rate drops back to
near-zero for steady-state accounts.

---

## B-150 · A canary that never runs after creation is silent

**Context** — 2026-10-02. The canary failure alarm (`canaries/10-canary.yaml`)
moved from `TreatMissingData: breaching` to `missing` plus
`InsufficientDataActions`, because the old setting fired within 90 seconds of
any (re)creation, before the canary had run once. Verified against the real
PagerDuty integration: OK -> INSUFFICIENT_DATA raises an incident, and back to
OK resolves it. A canary that **stops** reporting is still heard.

**The gap that leaves** — a canary that **never** runs after creation. Its alarm
starts in INSUFFICIENT_DATA and never transitions, so nothing notifies. Causes
would be a canary created stopped, a broken runtime, or a role that cannot
write its artifacts.

**Proposed fix** — the verification sweep (or a scheduled check in Audit)
asserts that every `platform-canary-*-failed` alarm older than 30 minutes has
left INSUFFICIENT_DATA at least once, and raises a finding naming the canary if
not. "Has this alarm ever had data" is answerable from the alarm's state
history, so it needs no new metric.

**Build when** — before the first tenant canary (Arc8's, once it has an
endpoint), or when the verification sweep is automated, whichever is first.
Today only the Sandbox smoke canary exists, and it has run for weeks.

**Cleanup from the test** — the throwaway alarm
`platform-TEST-insufficient-data-delivery` in Sandbox Exercise
(`801093629821`) has its actions disabled and can be deleted.

---

## B-151 · Every account had a Lambda concurrency limit of 10

**Found** — 2026-10-02, by the router throttle alarm added under D-028, which
went to ALARM within minutes of being deployed. The router in Audit had been
throttled every hour since about 22:55 the night before (5 to 69 throttles an
hour, against 400–1,270 invocations). Throttles are not errors, so
`platform-router-errors` never saw them. The router DLQ was empty: every
throttled event succeeded on retry, so nothing was lost -- but the margin was
retries.

**Cause** — the Lambda "Concurrent executions" quota (`L-B99A9384`) was **10** in
us-east-2 in every account in the organization, management, platform,
Sandbox and both Arc8 accounts. That is AWS's low default for new accounts. In
Audit, the router, reconciliation, evidence pipeline and cadence sweep share
those ten. In a tenant account the platform's instrumentation and response
functions share them with whatever the tenant deploys.

**Done 2026-10-02** — increases to 1000 requested in all ten accounts, from
inside each account, all PENDING:

| Account | Request id |
|---|---|
| management `738815759702` | `164c5421e91c405a991bf2e3bb917e33jWIiVIyN` |
| Log Archive `868150784436` | `f28f2452579f476a86130223e3415dd3pE0VNybe` |
| Infra Tooling `751479507989` | `051c1a323efa4bedaa35165872ac1185E7qQ66qO` |
| Audit `692869638531` | `09207acc7e75495c9b57a1c66687fb8aUOiD7eok` |
| Security Forensics `947247529132` | `ab692418df9447eeac926d99531088fe9l0dpV0T` |
| Arc8 test `904883674541` | `b39647e2fea043e7884f45b9fd41c6ceyUbQfahJ` |
| Arc8 dev `624606539840` | `272346d521564df88745bbb947cfe2ebMMO15bDF` |
| Security Backup `061976161709` | `502fa5fc46f140b191322e550f7be116H0ebOeVW` |
| Sandbox Canary `754280127660` | `f61ea4662a28499fbdabc0e102c0ee441pS7fezw` |
| Sandbox Exercise `801093629821` | `a91ff80bfaf64414842eeec4b592ba92ww360K3K` |

`vest-client.sh` now requests the same increase for every account it creates,
straight after creation.

**Still open** —
- **Confirm the approvals** and that `platform-router-throttled` returns to OK.
- **A standing check.** Nothing verifies the quota after the request. The
  verification sweep should report any account below 1000 in the home region.
- **Other regions.** us-east-1 and us-west-2 are allowed regions and still at
  their defaults. Request when the platform first runs Lambda there.

---

## B-152 · A secret written by hand in prod is a change nothing reconciles

**Open** — 2026-10-02. Created with the `SecretsCustodian` permission set
(identity/10-developer-access.yaml).

A custodian's `PutSecretValue` in prod replaces a credential the application
is using. That is a production change, and an outage if the value is wrong.
CloudTrail records it and names the custodian, but `change/src/reconcile.py`'s
`DEPLOYMENT_EVENTS` does not include it, so there is no change-record match,
no finding and no evidence row.

**Fix** — add `secretsmanager:PutSecretValue`, `UpdateSecret`,
`RotateSecret` and `ssm:PutParameter` against the application's prefixes to
the captured deployment events, matched by account and time like any other
deployment. Platform-prefixed names stay excluded: those are AltDigital's and
reconcile against the ops project.

**Trigger** — the first custodian is put in the group for a tenant that has
prod. Until then nobody holds the set where it matters.

## B-153 · The deploy role's encryption denies are written and not proven

**Closed 2026-10-02.** Proven in Sandbox Canary by
`scripts/test-deployer-conditions.sh`, 23 passed and 0 failed, against
`TenantAppDeployer-demo` redeployed from the current template with
`AccountTier=prod`. The real role trusts GitHub OIDC only, so the subject was
a clone carrying its boundary and its `Deploy` policy, removed afterwards.

**The question it asked:** how each encryption key resolves when a request
omits the flag. Answered:

| Create | flag omitted | false | true |
|---|---|---|---|
| RDS instance | refused | refused | authorized |
| Aurora cluster | refused | n/a | authorized |
| Aurora member instance | authorized (storage is the cluster's) | n/a | n/a |
| ElastiCache replication group | refused | refused | authorized |
| EFS | refused | n/a | authorized |
| Secret | no key: refused | n/a | alias and ARN both authorized |

So an omitted flag reads as unencrypted, and no compliant request was falsely
refused. `secretsmanager:KmsKeyArn` is populated when the key is named by
alias, so the same condition in `SecretsCustodian` behaves the same way.
Also proven: SNS email and https subscriptions refused, sqs authorized; uat/prod
deletes of an RDS instance, a DynamoDB table and a real secret refused; the
account public access block refused.

**What it found, both in ElastiCache and both fixed in the template:**
1. A compliant `CreateReplicationGroup` was refused by the role's OWN policy:
   with no parameter group named, ElastiCache authorizes against
   `parametergroup:*`, which `demo-*` and `default*` did not match. The
   supporting resources (parameter, subnet and user groups) are now
   account-wide; the caches stay name-scoped.
2. Then refused by the boundary: the create is evaluated against each of
   those resources, and only the replication group carries
   `elasticache:AtRestEncryptionEnabled`. `BoolIfExists` against
   `Resource: '*'` met the parameter group with no key and denied. The deny
   is now scoped to `replicationgroup:*`.

Every RDS create would have hit neither, so the pattern is per-service:
**re-run the script after any change to a conditioned statement.**

The script's first run also aborted midway (an unmatched grep under
pipefail) and left the clone and one EFS behind. It now cleans up on exit,
and the leftovers were removed by the rerun.

## B-154 · Nothing stops a dev developer touching a platform secret, if one ever lands there

**Latent** — 2026-10-02. Found while scoping `SecretsCustodian`, which denies
`secret:platform/*` by name.

`DeveloperDev` is PowerUserAccess. SCP 08 protects `/platform/` *parameters*,
but no SCP covers Secrets Manager. A `platform/...` secret in a dev account
could be read, overwritten or deleted by any developer. The developer boundary
does not help: it bounds the roles developers create, not developers
themselves.

**Why it is latent, not live** — no platform secret is held in a member
account. The PagerDuty routing key lives once, in Audit, on purpose
(`scripts/sync-pagerduty-secrets.sh` explains why), and `list-secrets` in Arc8
dev on 2026-10-02 returned nothing at all.

**Fix, when triggered** — add `secretsmanager:*` on
`arn:aws:secretsmanager:*:*:secret:platform/*` to SCP 08, beside
`ProtectPlatformParameters`, with the writing role exempt, plus a harness case.

**Trigger** — the first design that places a `platform/` secret in a member
account. Whoever writes that template adds the SCP statement in the same
change.

---

## B-155 · The digest is sending, and SES's sandbox will refuse the dev manager

**Built 2026-10-03.** `platform-digest` (Infra Tooling) runs the 10-01 code,
which sends to the client's `dev_manager` and falls back to `tech_contact`.
It was the 09-18 package until today. `DigestSender=platform@altdigital.ai`,
so it is in sending mode for the first time, and `DigestCc` is Jamie.

**The CC is Jamie, for now (Jamie, 2026-10-02).** It has two jobs: keep tabs on
Arc8 during turnup, and vet the digest's usability together with Wayne.
Design 06 intends the CC to be an AltDigital address, so a bounce or a departed
contact is visible to us. A person serves that today; a person stops being
the right answer once there is more than one tenant. *Trigger:* the second
tenant vests, or Jamie says the digest is settled. Then move `DigestCc` to a
shared ops mailbox.

**What still stops Wayne receiving it.** SES in Tooling is in the sandbox
(B-028: `ProductionAccessEnabled false`, 200 a day). The sandbox delivers
only to verified identities. `altdigital.ai` is verified, so the CC can
arrive. The dev manager's OEight address is not, and SES rejects a message if
any recipient is unverified. So the first scheduled run (Mon 2026-10-05,
13:00Z) fails for Arc8, CC included, and is recorded as `failed`. Two ways
out:
- verify the one address: AWS emails a link, and Wayne clicks it. Fast, but it
  does not scale to a client's staff (B-028's own objection);
- the B-028 production-access request, which is the real fix. Submitted
  2026-10-03, review pending.

**Interim, 2026-10-03:** SES email identities created in Tooling for
Jamie's and Wayne's OEight addresses (tagged `purpose=digest-sandbox-recipient`),
so the digest can be delivered before production access clears. Each is
verified only once its owner clicks AWS's link. *Remove them* when production
access is granted. Verified recipient identities are a sandbox workaround,
and keeping them would hide which recipients the account can reach on its own.

**Nobody is subscribed to `platform-digest-bounces`.** The template creates
the topic and the SES event destination, but nothing listens. A bounce is
published to a topic with zero subscribers, which is design 06's "rotted
contact, invisible" case one hop later. It should route through the central
router like every other platform event, not to an inbox. *Trigger:* before
SES leaves the sandbox, because that is when sends to unverified client
addresses, and therefore real bounces, begin.

**Unconfirmed:** whether `platform@altdigital.ai` is a mailbox anyone reads.
Replies to the digest go there.
