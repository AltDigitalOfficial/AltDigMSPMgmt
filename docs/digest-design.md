# Digest design: two audiences, one gatherer

**Status:** Stage A built 2026-10-14 (B-171): gatherer, facts bucket, manager
render with time zones, layer-tag, HIPAA and telemetry lines, and the else
paths. Stage B, everything from the org trail, waits on `security/50`. The
platform digest (B-172) is designed and not built.
**Rulings (Jamie, 2026-10-14):** two separate emails; which signals go to which
email (§3); design all three increments before building; build the manager
digest. [ADR-003](adr/ADR-003-two-digests-two-audiences.md).
**Ratified (Jamie, 2026-10-14):** where the facts come from and where they are
gathered (§6), including the new Log Archive role, Audit bucket and Tooling
read grant; personal data as §9 sets out; AltDigital humans itemised (§7.2).
[ADR-004](adr/ADR-004-digest-facts-gathered-in-audit-from-org-trail.md).
**Work:** B-171 (manager digest), B-172 (platform digest).

---

## 1. Why

The digest for Arc8 dev on 2026-10-10 said two resources were running with no
monitoring, and gave nothing a reader could act on:

```
  - arn:aws:resource-explorer-2:us-east-2:...:index/f4f4... (AWS::ResourceExplorer2::Index) — unrecognised-resource-type
```

Finding out what they were took a CloudTrail lookup through SecurityReadOnly.
AWS made them. A developer opened the console home page, and Resource Explorer
onboarded itself through its service-linked role (session name `onboarding`).
Nobody built anything. The answer existed in the trail the whole time. The
digest just did not go and get it.

Two further gaps are already recorded:

- **B-155:** the digest carries alarms and instrumentation exceptions only.
  Design 11's *Manager digest* (services newly enabled, roles created,
  guardrail failures, cost against budget, telemetry gaps) was never built.
- **The developer guide promises it anyway.** §4.15 and §4.16 tell Arc8's
  developers the digest lists services enabled, roles created, guardrail
  denials, telemetry gaps and HIPAA-eligibility lines. It lists none of them.
  The eligibility sweep's `digest`-class findings go to the review queue,
  which nothing reads (B-051).

## 2. Two audiences

| | Manager digest | Platform digest |
|---|---|---|
| Reader | The client's development manager. AltDigital CC'd | AltDigital only |
| Question | What did my team do in the last 24 hours, and what needs my attention? | Did we miss a chance to keep the platform scalable and supportable? |
| Scope | One account | Every tenant account |
| Cadence | Daily, 13:00 UTC | Daily, after the manager digests |
| Evidence | Yes. The rendered body goes to the Log Archive (ADR-001) | No. Internal working document |
| Clock | The client's time zone (§5) | Mountain |

They are separate emails because the platform digest is AltDigital's own
assessment of the platform, written for us. It may say, in effect, that the
client's team did something the platform should have prevented or made easy.
That belongs in our inbox, not theirs. A section visible only in the CC copy
would need two renders of every account's email, and one mistake would send
the internal section to the client.

## 3. What goes where

| Signal | Manager | Platform | Source (§6) |
|---|---|---|---|
| Resources created: what, by whom, when, how | ✓ | | trail |
| Team activity summary, per person | ✓ | | trail |
| Services used for the first time in the account | ✓ | | trail |
| Roles created, their trust and what they can reach | ✓ | | trail |
| Guardrail denials (SCP/RCP), itemised | ✓ | | trail |
| Guardrail friction: denials grouped across tenants, recurring | | ✓ | trail |
| ClickOps: resources made from the console or CLI rather than IaC | ✓ itemised | ✓ counts and trend | trail |
| Missing `platform-layer` tag (B-127, B-128) | ✓ itemised | ✓ counts across the estate | Config aggregator |
| Spec gaps: types 06a does not know, across the estate, with age | | ✓ | instrumentation exceptions, plus trail for the creator |
| Could not instrument (today's exception list) | ✓, now with who/what/when/how | | instrumentation exceptions |
| New alarms, alarms in ALARM | ✓ (unchanged) | | CloudWatch, per account (unchanged) |
| HIPAA eligibility lines, telemetry gaps | ✓ | | router log (digest class) |
| Cost against budget | ✓, as "no budget configured" until one exists | | not available (§7.4) |
| Pipeline health: accounts not queryable, facts missing, digests failed or bounced, review-queue depth | | ✓ | gatherer, digest metrics |

The missing tag is in both emails because both readers can act on it. The
team fixes its own resources, and the platform reads the tag to route alarms
(B-127), so an estate-wide count tells AltDigital whether the enforcement is
working.

ClickOps is itemised for the manager because only the team can change how it
deploys. In the platform digest it is a count, because a rising count in test
and above means there is no easy deployment path the team wants to use.

## 4. Every resource line answers what, who, when and how

The line from §1 becomes:

```
  - Resource Explorer index (AWS::ResourceExplorer2::Index), us-east-2
    f4f465a2-c173-46c8-b365-9ef95c7686f5
    Created 2026-10-09 11:00 MDT by AWS, on behalf of <developer> (DeveloperDev),
    when they signed in to the console. Nobody asked for it.
```

**What.** A readable name for the type, then the type itself, the region, and
the resource's own id. The full ARN is noise in an email. The id is enough to
find the resource, and the ARN stays in the facts document.

**Who and how: the actor class.** Derived from the creating event's
`userIdentity`, `invokedBy`, `userAgent` and `sessionCredentialFromConsole`.

| Class | How it is recognised | Rendered as |
|---|---|---|
| `human-console` | Human session, `sessionCredentialFromConsole = true` | "by <person> (<permission set>), in the console" |
| `human-cli` | Human session, CLI or SDK user agent | "by <person> (<permission set>), from the CLI" |
| `human-iac` | CloudFormation acting for a human session, or a Terraform/CDK user agent | "by <person> (<permission set>), through <tool>" |
| `deployer` | `AppDeployer` role (prompt 8.x) | "by the deployment pipeline" |
| `platform` | Session issuer is a `Platform*` role or the StackSet execution role | "by AltDigital platform automation" |
| `aws-service` | Service-linked role, or `invokedBy` is `*.amazonaws.com`, with no human session in the chain | "by AWS (<service>)", plus the human who triggered it when the trail shows one within 60 s |
| `unattributed` | No creating event found | "creator not found in CloudTrail", never left blank |

A person is named by their Identity Center session name, which is what the
trail records. Personal data: see §8.

**When.** In the reader's time zone, with the zone abbreviation shown (§5).

**Finding the creating event.** Match the resource's ARN or id against the
trail's `resources` column and `responseelements`, from the Config item's
`configurationItemCaptureTime` back 24 hours. Resource types whose create
event does not carry the id get a per-type rule in the gatherer. A type with
no rule, or no match, is `unattributed`. That is shown as the truth, not
guessed.

## 5. Time zone

- **Manager digest:** a new attribute on the CLIENT registry record,
  `timezone`, as an IANA name. Absent means `America/Denver`. Set with
  `registry.sh put-client --timezone`.
  - **It is the reader's zone, not the team's.** The first draft of this section
    said Arc8's record should be `Asia/Kolkata` because Arc8's developers work
    India hours (developer guide §4.14). That was wrong. The digest goes to the
    development manager, who is not in India, and questionnaire 5.2's zone is
    the developers' schedule default, so it is not a substitute.
  - **Arc8 is left on the default.** Its manager's zone is not recorded.
  - **The questionnaire still needs this question.** 3.6 should ask the
    manager's zone beside the manager's address.
- **Platform digest:** always `America/Denver`.
- **Evidence and facts:** UTC, ISO 8601. An evidence record needs one
  unambiguous clock. The email shows the zone abbreviation on every timestamp,
  and the header states the window in both the local zone and UTC.
- **If the zone cannot be loaded** (tzdata missing from the Lambda image, or a
  bad name on the record), the digest renders in UTC and says so in its header.
  This deliberately differs from `scheduling/src/hoop.py`, which refuses to
  run. There, a wrong clock turns production off. Here, a refusal loses the
  email, and the email is the evidence. The digest runs on python3.13. tzdata
  is bundled in the deployment package rather than trusted to the runtime.

## 6. Architecture: gather in Audit, render and send in Tooling

```
 Log Archive                 Audit                              Infra Tooling
 ┌─────────────┐  Athena  ┌──────────────────────────┐  S3    ┌──────────────────────┐
 │ org trail   │◄─────────┤ platform-digest-facts    ├──────► │ platform-digest      │──► manager email
 │ (security/  │  query   │  12:15 UTC daily         │ facts  │  13:00 UTC           │     (per account)
 │  50 table)  │  role    │  · trail queries         │ JSON   │  · reads facts       │──► platform email
 └─────────────┘          │  · Config aggregator     │        │  · reads alarms and  │     (AltDigital)
                          │  · router log (digest)   │        │    exceptions per    │
                          └──────────────────────────┘        │    account (today)   │
                                                              └──────────────────────┘
```

**Gatherer** (`platform-digest-facts`, Audit, daily 12:15 UTC):

1. **Org trail, through Athena.** The `platform-audit` workgroup and
   `platform_audit.cloudtrail` table from `security/50-trail-query.yaml`. A
   narrow role in Log Archive, `PlatformDigestFactsQuery`, has
   `StartQueryExecution` on that workgroup, read on the trail prefix and on
   its own results prefix, and nothing else. One query per signal per run,
   covering every tenant account, partition-pruned to `dt` and `account`.
   Sources: activity, creators, ClickOps, guardrail denials, services used for
   the first time, roles created.
2. **The Config aggregator** `platform-organization`, which is local to Audit.
   Sources: missing `platform-layer` tag (this is B-128's already-decided
   mechanism), and the current trust and attached policies of each role the
   trail says was created.
3. **The router's decision log**, `/aws/lambda/platform-router`, also local to
   Audit. Routing decisions with `digest: true`, per account. That is the
   eligibility sweep's lines today, and telemetry gaps once B-148 gives the
   router a normaliser for them. This is B-051's option of having the digest
   read the routing log, and it keeps W-010 intact: the digest shows what the
   router decided was digest-class, and never decides that itself.

Output: one JSON facts document per tenant account and one estate document,
written to `platform-digest-facts-<audit-account>` (SSE-KMS, 30-day expiry).
Tooling's `PlatformDigestSender` gets `s3:GetObject` on it and nothing else.
The facts are input, not evidence. What was sent is the evidence, and that is
already written to the Log Archive (ADR-001).

**Digest** (Tooling, 13:00 UTC, unchanged trigger): reads the facts for each
account and keeps its current per-account read of alarms and exceptions, then
renders and sends. After the per-account loop, it renders the platform digest
from the estate document plus its own run results (sent, skipped, failed,
bounced).

**The else path.** If an account's facts are missing, or older than the run
they should have come from, the manager digest still sends. It carries the
monitoring sections, plus one line saying "team activity could not be read for
this window; AltDigital has been told". It also emits `AccountsFactsMissing`.
The platform digest lists the account under pipeline health. A new alarm,
`platform-digest-facts-missing`, routes low urgency through the router
(D-028). A missing input degrades the email. It never suppresses it, and never
silently shortens it.

**Accounts the trail table cannot see.** The table's `account` partition is
projected from a list supplied at deploy time (`security/50`'s own comment),
so a newly vested account is invisible until that stack is redeployed. The
gatherer compares registry accounts with the projected list. Any registry
account the table cannot query is a pipeline-health line, and its manager
digest takes the else path above. Otherwise a new tenant's first month would
look like a quiet team.

## 7. Manager digest: content

Plain text, as today (`render()`'s docstring still applies). Sections run in
order of what needs the reader to act. An empty section is left out, except
where its absence would be ambiguous, as noted below.

### 7.1 Needs your attention

- **Guardrail denials.** Action, service, region, who, how many times, and which
  policy denied it (SCP or RCP, from `errormessage`). One line per distinct
  (person, action, policy) group.
- **ClickOps.** Resources created by `human-console` or `human-cli`. In dev,
  under the heading "made by hand: fine in dev; test and above will need these
  in your deployment code". In test, uat and prod, under "made by hand outside
  the deployment pipeline".
- **Missing `platform-layer` tag.** Resource, type, created by. "Alarms on these
  will page the wrong team."
- **HIPAA eligibility** and **telemetry gaps**, as the router recorded them.

### 7.2 What your team did

- **"Your team"** is any human session whose permission set is a tenant one
  (`Developer*`, the tenant's own sets). Grouped per person: a count of
  changes, then the resources they created (§4 line format) and the services
  they used for the first time.
- **Roles created.** Name, who created it, trusted principals, attached
  policies. Flagged when a policy is `AdministratorAccess`, or grants `*` on
  `*`, or trust reaches outside the organization.
- **AltDigital in your account.** One line counting platform-automation
  changes. Any **human** AltDigital session (SecurityReadOnly excepted,
  break-glass included) is itemised. It is their account, and they should see
  us in it.
- **Quiet day:** "No changes by your team in this window." Always shown. A
  missing section would be indistinguishable from a broken gatherer.

Caps: 15 lines per person, 60 for the section, then "and N more". The facts
document holds the complete list.

### 7.3 Monitoring

Today's content: new alarms, alarms in ALARM, could not instrument. The
could-not-instrument lines take the §4 format. "Unrecognised resource type"
becomes "we have no monitoring standard for this type yet. AltDigital has
been told (no action needed from you)". That fits who has to act: a spec gap
is ours to close (§8 platform digest), not the manager's.

### 7.4 Cost

"No budget is configured for this account." Shown, not omitted, until
budgets exist. `provisioning/src/saga.py` marks per-tenant budget creation as
not built, and the developer guide (§4.15) promises a budget alarm. Building
budgets is not part of this work. When budgets exist, this becomes
month-to-date spend against budget.

## 8. Platform digest: content

One email, all tenants, to `DigestCc` (Jamie today, then a shared mailbox
under B-155's trigger). Mountain time. Always sent, quiet days included, and
pipeline health always comes first. A quiet platform digest is only
believable if it shows the pipeline was healthy.

1. **Pipeline health.** Accounts digested, skipped, failed, bounced; facts
   missing; registry accounts the trail table cannot query; review-queue depth
   (B-039: no consumer yet, so it only grows).
2. **Spec gaps.** Unrecognised types across the estate, grouped by type:
   accounts affected, first seen, days open, creator class. A type created by
   `aws-service` in every account carries the hint "AWS-created. Likely
   resource_types_no_alarms or aws_default_resources". The Resource Explorer
   pair would have shown here on 2026-10-09 with that hint, and again every day
   it stayed open.
3. **Guardrail friction.** Denials across tenants, grouped by (action, policy),
   with distinct people, accounts, and the days it recurred in the last 14. A
   denial that recurs across people or days is a missing paved road, or a
   guardrail in the wrong place.
4. **ClickOps.** Count per account per tier, with the 14-day trend. Test and
   above itemised.
5. **Missing `platform-layer` tag.** Count per account, new since yesterday.

Days open, recurrence and trend need history. The gatherer reads back up to 14
days of its own facts documents rather than keeping a separate state table.
Facts expire at 30 days, which covers it.

## 9. Personal data

The manager digest names developers by their Identity Center session name,
which is their sign-in email. The rendered body is stored in the Log Archive
under COMPLIANCE Object Lock for six years (ADR-001), and cannot be removed
early.

**Accepted, with this reasoning.** The same identities are already in the org
trail for the same period, so this creates no new retention of a new kind of
data. The recipient is those people's own manager, and the CC is the operator
of the account they work in. Developer activity reporting to the dev manager
is what design 11 specifies.

**Limit.** The digest never includes request parameters beyond resource
identifiers, and never anything read from application logs. The facts bucket
holds the same identities with a 30-day expiry.

## 10. Dependencies and costs

- **`security/50-trail-query.yaml` is uncommitted and not deployed or proved**
  (auditor-guide §1.3: "Built 2026-10-08, not yet deployed or proved"). The
  gatherer is blocked on it.
- **Missing table column.** The trail table has no `sessioncredentialfromconsole`
  column, which is a top-level CloudTrail field. It must be added for the
  console-or-CLI distinction. That edit is to `security/50`, so it has to be
  coordinated with whoever owns that file.
- **Scan cost.** "Services used for the first time" compares 24 hours against
  the prior 90 days of `eventsource` per account, which is a 90-day scan per
  run. At Arc8's volume that is cents a day. It becomes a W-item: replace it
  with a seen-services record once a single run scans more than 5 GB.
- **Trail delivery lag.** CloudTrail delivers to S3 within roughly 5 to 15
  minutes. The 24-hour window ends at 12:00 UTC, not at the run time, so the
  gather does not read a trail that is still arriving.

## 11. Build order

1. `security/50` deployed and proved, with the column added. Owned by the
   session that built it; Jamie is closing that out (2026-10-14).
2. Gatherer, facts bucket and `PlatformDigestFactsQuery`. Its output is
   checked by hand against the 2026-10-09 Resource Explorer events.
3. Manager digest render (B-171). The CLIENT `timezone` attribute, the
   `dynamodb:Attributes` grant for it, and developer guide §4.15 brought into
   line with what actually ships.
4. Platform digest (B-172).

Separately, and done 2026-10-14: `AWS::ResourceExplorer2::Index` and `::View`
are in 06a's `resource_types_no_alarms`.

## 12. Open

- ~~Break-glass and AltDigital humans in the client's email (§7.2).~~ Resolved:
  itemised (Jamie, 2026-10-14).
- **Questionnaire 3.6** gains a time-zone question. The wording is for whoever
  owns the questionnaire's move to self-service.
- **Cost (§7.4)** stays a placeholder until per-tenant budgets are built. That
  is not scheduled.
