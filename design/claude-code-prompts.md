# Claude Code Build Prompts

Sequenced build instructions. Each prompt assumes the design documents are
present in the repository and readable.

**Before starting any of these:**

```bash
# Repository layout assumed by these prompts
platform/
├── design/                  # this package
├── baseline/                # StackSet templates
├── vesting/                 # account provisioning pipeline
├── runbooks/                # SSM Automation documents
├── instrumentation/         # auto-instrumentation Lambda
├── recovery/                # Step Functions restore workflow
├── containment/             # security response automation
├── policies/                # SCPs, cfn-guard rules
├── provisioning/            # saga orchestration, external system modules
├── scheduling/              # HOOP scheduler, turndown/turnup, dormant lifecycle
├── partners/                # partner onboarding, OU structure, billing roll-up
├── pagerduty/               # Terraform — services, policies, routing
└── scripts/                 # CLI wrappers
```

**Credential hygiene.** Run Claude Code under a **read-only or plan-only AWS
profile** by default. Assume the deploy profile deliberately, for the deploy
step only.

```bash
aws sso login --profile platform-readonly
export AWS_PROFILE=platform-readonly
```

**Standing instruction to include in every session** (or put in `CLAUDE.md`):

> Read `design/01-design-principles.md` before making architectural choices.
> Never write code that terminates, deletes, deregisters or purges a resource as
> part of automated remediation. All templates must pass `cfn-lint` and
> `cfn-guard` against `policies/` before being considered complete. Prefer
> explicit over clever — this code will be read by auditors.

---

## Phase 0 — Repository and validation loop

### Prompt 0.1 — Scaffold

```
Read design/01-design-principles.md and design/08-change-and-release.md.

Set up the repository skeleton described in prompts/claude-code-prompts.md.

Create:
- A Makefile with targets: lint, guard, validate, changeset, deploy-canary,
  deploy-nonprod, deploy-prod
- cfn-lint configuration
- A cfn-guard rules file in policies/ that enforces, at minimum:
  - No S3 bucket without encryption and BlockPublicAccess
  - No security group with 0.0.0.0/0 on any port except 443 via ALB
  - No IAM policy containing Action "*" with Resource "*"
  - All KMS keys have EnableKeyRotation true
  - All CloudWatch log groups specify a KmsKeyId and RetentionInDays
- A pre-commit hook running lint and guard
- CLAUDE.md containing the standing instruction from the prompts document

Do not create any AWS resources. This is local scaffolding only.
```

### Prompt 0.2 — CLI wrappers

```
Read design/08-change-and-release.md, section "Development environment".

Create scripts in scripts/ wrapping the StackSet operations we will use
repeatedly. StackSet CLI output is terse and the flags are easy to get wrong,
so these should be thin, well-named, and print what they are about to do before
doing it.

Needed:
- create-or-update-stackset.sh
- deploy-to-ou.sh  (takes OU id, wave name, max concurrent count)
- detect-drift.sh  (all stacksets, summarise results)
- describe-failures.sh  (pull the actual failure reason for failed instances)

Every script must support --dry-run and must refuse to run against the
production Members OU unless --confirm-production is passed explicitly.
```

---

## Phase 1 — Organization and guardrails

### Prompt 1.1 — OU structure

```
Read design/02-platform-architecture.md.

Write CloudFormation for the Organization OU structure described there. Assume
the Organization already exists with all features enabled.

Output the OU ids to SSM Parameter Store in the Platform Tooling account so
later templates can reference them without hardcoding.
```

### Prompt 1.2 — Service Control Policies

```
Read design/04-security-controls-and-evidence.md, section "Protecting the
controls from the account", and design/03-identity-and-access.md, section
"Quorum-controlled actions".

Write the SCPs for the Members OU. They must deny member-account principals
from:
[list from design 04]

Each denial must exclude the platform roles by aws:PrincipalArn condition.
Parameterise the excluded role ARNs.

Also write the Sandbox OU SCP (more permissive) and the Security OU SCP (most
restrictive).

Important: write these so that a member account with full local administrator
cannot remove them or grant itself an exception. Explain in comments how you
have ensured that, and flag anything you are unsure about rather than assuming.
```

### Prompt 1.3 — Test the guardrails

```
Write a test harness that verifies the SCPs actually work.

For each denial in the Members OU SCP, the harness should attempt the denied
action from a test principal with AdministratorAccess in a sandbox member
account, and assert the attempt is denied.

This is a load-bearing assumption in our design (see P10) and it breaks quietly
when SCPs are edited. The harness should be runnable on a schedule.

Do not use any destructive action for the positive test path — test the denial
of ScheduleKeyDeletion against a key created for the purpose, and assert denial
rather than relying on the action failing for another reason.
```

---

## Phase 2 — Baseline StackSet

### Prompt 2.1 — Detective controls

```
Read design/04-security-controls-and-evidence.md.

Write the baseline CloudFormation for the detective control stack:
CloudTrail (org trail), Config recorder + delivery channel, GuardDuty,
Security Hub, Macie, Inspector, IAM Access Analyzer.

All aggregate to the Audit account; CloudTrail delivers to Log Archive with
Object Lock.

Parameterise: which conformance packs to attach (driven by the tenant's
frameworks), and the log retention floor.

Include CloudWatch Logs data protection policies applied at the account level
so they cover log groups created after deployment. Detect and mask, at minimum:
credit card numbers, US SSN, and common credential patterns. This is our direct
answer to an application writing sensitive data to stdout, so get the audit
trail for masking events right.
```

### Prompt 2.2 — KMS and secrets

```
Read design/04-security-controls-and-evidence.md, sections "Encryption at rest"
and "Secrets".

Write the KMS baseline: the four-key standard set (storage, database, secrets,
logs) with:
- Automatic annual rotation enabled
- 30-day deletion window, not 7
- Key policies allowing member-account use but denying disable, delete, and
  policy modification to everyone except the platform quorum pipeline role

Also write the Secrets Manager baseline and a Config rule that flags secrets
exceeding a maximum age, parameterised.

Add conditional resources for the questionnaire exceptions: dedicated
regulatory key, CloudHSM-backed key, external key material.
```

### Prompt 2.3 — Networking and egress profiles

```
Read design/02-platform-architecture.md, section "Networking".

Write three parameterised network baselines corresponding to the egress
profiles: locked, controlled, public-facing.

All three include: VPC with multi-AZ subnets (production) or single-AZ
(dev/test), VPC flow logs to Log Archive, VPC endpoints for the AWS services
our baseline uses so that platform traffic does not traverse NAT.

Locked profile adds Network Firewall with a domain allow-list parameter.
Public-facing adds ALB + WAF + CloudFront scaffolding.

There is deliberately no peering, Transit Gateway, or private connectivity
between member accounts. Do not add any.

Also create the isolation security group (deny all except the Forensics
collection path) in every VPC, unattached, ready for incident use.
```

### Prompt 2.4 — Staged rollout pipeline

```
Read design/08-change-and-release.md, section "The platform's own pipeline".

Build the CodePipeline in the Platform Tooling account that deploys the baseline
StackSet in waves: Sandbox canary → non-production Members → production Members,
with a bake period between waves and automatic rollback on stack failure.

Production wave must use a limited MaxConcurrentCount.

Add a manual approval gate before the production wave. Approver: a role, not a
named individual, so the second-approver arrangement works without a code
change.

Log every deployment as a change event to the reconciliation system.

This pipeline can modify every member account. Treat its own IAM as the most
sensitive thing in the repository and explain your choices in comments.
```

---

## Phase 3 — Vesting

### Prompt 3.1 — Vesting pipeline

```
Read intake/onboarding-questionnaire.md and design/02-platform-architecture.md,
section "Account vesting", plus diagrams/account-vesting.mermaid.

Build the vesting pipeline. Input is a completed questionnaire as structured
data (define the schema); output is three fully baselined accounts plus a
registry entry plus an evidence record.

The derived fields ([D] in the questionnaire) must be computed by the pipeline,
not supplied — region default, conformance packs, retention floor, monitoring
tier, dedicated host requirement.

Vesting must fail loudly if the Truveon tenant registration does not confirm
first evidence receipt. A vested account that is not sending evidence is worse
than no account.

Include a --dry-run that prints every derived value and every resource that
would be created, for review before the first real run.
```

### Prompt 3.2 — Platform registry

```
Build the platform account registry: the authoritative record of every member
account and its parameters.

It must support the two reports required by design principle P9:
1. Which accounts are behind the current global baseline version
2. What declared local overrides exist, why, and who owns each

Undeclared difference between an account's actual configuration and its
recorded parameters is drift and must surface as a finding.

Keep this simple. A DynamoDB table plus a query CLI is sufficient; do not build
a web application.
```

---

## Phase 4 — Observability

### Prompt 4.1 — Auto-instrumentation Lambda

```
Read design/06-observability.md, section "Auto-instrumentation".

Build the instrumentation Lambda. EventBridge triggers it on resource creation;
it applies the standard alarm set for the resource type and account tier, tags
the alarms platform-managed: true, and records the action.

The alarm sets are specified in design/06a-alarm-specification.yaml. Load that
file as configuration — do not transcribe it into code. Adding a resource type or
changing a threshold must be a config edit.

Things in that file the code must actually handle:
- threshold_mode: relative thresholds (percent_of_max_connections,
  percent_of_allocated, percent_of_configured_timeout, percent_of_account_limit,
  reference_metric) must be resolved against the live resource at alarm-creation
  time. A fixed number is wrong across instance classes.
- threshold_source: questionnaire_7_4 must read the acceptable response time
  from the onboarding record. That value is used three times — canary timeout,
  SLA pass condition, and this alarm. Never hardcode it.
- A [TUNE] threshold of null means the alarm CANNOT be created. That is an
  exception record, not a skip.
- severity_by_tier overrides the tier default_routing. Two alarms rely on this
  (no-healthy-hosts pages in UAT; telemetry gap never pages in dev).
- applies_when conditions gate whether an alarm is created at all.
- inherits on a resource type means take the parent's set plus additional_alarms.
- Omitting a tier from threshold_by_tier suppresses that alarm in that tier.

Critical: anything it cannot handle produces an exception record. It must never
fail silently. An unrecognised resource type is an exception, not a no-op.

Include the SSM association that installs the CloudWatch agent on new
instances, since memory and disk alarms are useless without it.
```

### Prompt 4.2 — Drift detection for alarms

```
Extend the instrumentation system with drift detection.

If a platform-managed alarm is deleted or modified, recreate it and log the
event. Run on a schedule and on CloudWatch alarm deletion events.

Without this, monitoring coverage erodes quietly and we discover it during an
incident.
```

### Prompt 4.3 — Canaries

```
Read design/06-observability.md, section "Synthetic monitoring".

Build:
1. A CloudWatch Synthetics canary template parameterised by the questionnaire
   fields 7.2 through 7.6 — endpoint, success condition, timeout, auth.
   Credentials come from Secrets Manager, never from the canary code.
2. A Fargate-based runner for tenants with existing Playwright suites, publishing
   results to CloudWatch as custom metrics on the same metric names, so the
   downstream alarm path is identical either way.

The timeout value is used twice: as the canary pass condition and as the SLA
measurement condition. Make that explicit in the code so nobody changes one
without the other.
```

### Prompt 4.4 — Digest reporting

```
Read design/06-observability.md, sections "The dev team notification loop" and
"Reporting".

Build the batched digest. Per account, daily or weekly (parameterised), to the
named contact from questionnaire 1.5, CC to the AltDigital address.

Batched, not per-resource. A Terraform run creating forty resources must produce
one email.

Content: resources that appeared, alarms applied, exceptions pending, and an
invitation to specify additional monitoring.

This doubles as compliance evidence that monitoring requirements were
communicated, so the send record must land in Truveon with timestamp and
recipient.

Handle bounces: a bounced digest is a finding, because it means the contact has
rotted.
```

---

## Phase 5 — Response automation

### Prompt 5.1 — Runbook framework

```
Read design/07-response-automation.md, Part 1, and
diagrams/alarm-response.mermaid.

Build the four-stage runbook framework as SSM Automation documents.

Structure it so that a runbook is defined by data — (alarm type x resource type)
mapped to a set of stage 1 gather actions, stage 2 remediations, stage 3
remediations — and the framework handles staging, revalidation, safety rails and
dossier assembly generically.

Safety rails are not optional and must live in the framework, not in individual
runbooks: circuit breaker (3x in 1 hour), per-account rate limit, redundancy
check before stage 3, one resource at a time in stage 3, kill switch.

Every automated action emits a change record.

The dossier format is in design 07. Same structure whether the outcome is repair
or escalation.
```

### Prompt 5.2 — First runbooks

```
Using the framework from 5.1, implement these runbooks end to end:

1. RDS high connection count
2. ECS task flapping / repeated restarts
3. EC2 disk space exhaustion
4. ALB 5xx rate elevated
5. Lambda error rate elevated

Each must exercise all four stages, including the redundancy check before any
stage 3 action.

For each, document in the runbook file: the common causes, what stage 1 gathers
and why, and what stage 2 and 3 will attempt. That documentation is the thing a
Tier 2 human reads at 3am, so write it for that reader.
```

### Prompt 5.3 — PagerDuty routing

```
Build the EventBridge routing layer described in design/06-observability.md,
section "Alerting and routing".

Route by severity AND environment. Production canary failure pages. A dev
environment CPU alarm does not.

Three destinations: PagerDuty (actionable now), Jira (owned and dated, no page),
review queue (no page, no ticket).

The PagerDuty payload must carry the full dossier, not just the alarm name.
```

---

## Phase 6 — Recovery

### Prompt 6.1 — Unified restore workflow

```
Read design/05-resilience.md, section "The unified restore path". This is
design principle P4 and it is the most important constraint in this prompt.

Build ONE Step Functions workflow with a mode parameter:
- test mode: restore into isolated test VPC, validate, record, tear down
- recovery mode: restore into production networking, validate, record, STOP

Same code path. Same sequence. Same validation. The only differences are the
target and whether teardown runs.

Do not build two workflows. Do not build a "test harness" that wraps a separate
production procedure. If you find yourself writing mode-specific logic beyond
target selection and teardown, stop and flag it.

Validation steps: resource health, database connects, row counts plausible,
volumes mount, checksums match.

Record elapsed time against the RTO target and emit to Truveon.
```

### Prompt 6.2 — Restore test scheduling and thresholds

```
Schedule the restore workflow monthly per member account in test mode.

Implement two-tier threshold alerting on restore duration:
- Warning tier: Jira ticket with owner and due date, email, no page
- Breach tier: PagerDuty page

The threshold percentages are parameters, not constants — they have not been
decided yet (see design 05 and open-items.md). Default them to placeholder
values and make it obvious in the code that they are unset.

Also implement duration trend tracking, so creep toward the RTO target is
visible months before it breaches.
```

---

## Phase 7 — Security incident response

### Prompt 7.1 — Declaration and containment

```
Read design/07-response-automation.md, Part 2, and
diagrams/incident-containment.mermaid.

Build the declaration-to-containment automation.

Absolute constraints:
- discovery_time is set by the system at detection and is never editable. Where
  a human declares on something detected earlier, record both and treat the
  earlier as governing.
- Sequence is preserve, then isolate, then revoke, then blast radius. Nothing
  destructive happens before preservation completes.
- Isolation leaves the instance running. Only reachability changes.
- Revocation attaches a deny-all policy. It does not delete the principal.
- Session revocation by token issue time is required, not optional. Disabling a
  principal does not invalidate sessions already issued.
- The automation NEVER terminates, deletes, deregisters, rolls back or purges.
- If further action would take a production service down, stop and page for
  human authorisation.

Confidence thresholds differ by environment — see the table in design 07.

This executes from the platform account via a role member accounts cannot deny.
```

### Prompt 7.2 — Incident readout

```
Build the security incident readout generator.

Contents per design 07: what fired, discovery_time, what was preserved and
where, what was isolated, what the principal did in the preceding 24 hours from
CloudTrail, what else that principal can reach, whether the same indicators
appear in other accounts, current containment state, and what was deliberately
not done.

The cross-account indicator check is the one that distinguishes a single
compromised workload from a platform-level event. Do not skip it for speed.

Attach to the PagerDuty incident and write to Truveon.
```

### Prompt 7.3 — The notification clock

```
Build the notification clock.

Starts at discovery_time. Counts against the tenant's contractual notification
window, which is a per-tenant field (questionnaire 2.6) currently a placeholder
owned by Art and Wayne.

Surfaces a live countdown on the incident and escalates at defined fractions of
the window.

Nobody should be doing date arithmetic during a live incident. Make the deadline
impossible to miss and impossible to miscalculate.

Handle the case where the window is unset: that is itself a blocking finding at
vesting, not a runtime surprise.
```

### Prompt 7.4 — Containment exercise

```
Read design/07-response-automation.md, section "Exercising containment
automation".

Build the scheduled containment exercise. It fires the REAL containment
automation against a purpose-built target in an isolated account.

Same code path as production, different target. This is P4 applied to security
response.

Steps: plant a finding, let it declare, let it preserve and isolate, verify
snapshots landed in Forensics and isolation actually isolated, measure elapsed
time, tear down.

Output is tabletop evidence with real timings. Write it to Truveon in a form an
auditor can read.

Also build the blast-radius verification: confirm a member account with full
local admin cannot block the containment role. Run it on the same schedule.
```

---

## Phase 8 — Change reconciliation

### Prompt 8.1 — AppDeployer role

```
Read design/08-change-and-release.md, sections "The control point".

Write the AppDeployer role template. Assumable only via OIDC federation from the
tenant's CI identity — no static credentials, ever.

Scoped to the resources that application legitimately deploys, parameterised.
Denied from platform-managed resources by SCP (already handled) and by the
role's own boundary.

Include a permissions boundary so the role cannot escalate itself.
```

### Prompt 8.2 — Reconciliation engine

```
Read design/08-change-and-release.md, section "Reconciliation".

Build the reconciliation engine: compare deployment events captured from
CloudTrail and EventBridge against change records in Jira, and produce the four
outcomes in the table in design 08.

Capture the deployment event types listed there.

Findings route per the table. A deployment with no change record is a
low-severity page plus a Jira ticket to the tenant's change approver.

Output feeds two consumers: Truveon (audit evidence) and the SLA exclusion
determination (design 10). Make the customer-caused-outage correlation query a
first-class function, because it is what we rely on commercially.
```

### Prompt 8.3 — Quorum verification

```
Read design/08-change-and-release.md, section "Quorum verification".

Build the quorum-gated execution pipeline for: KMS key deletion, backup vault
deletion or lock modification, account removal from the Organization, CloudTrail
disable.

The pipeline queries Jira at execution time and verifies all five conditions
listed in design 08 — including that neither approver is the requester, and that
the issue has not been edited since approval.

Validate; do not trust the ticket status field. Jira permissions are easy to get
subtly wrong.

Snapshot the approval evidence immutably into Truveon at the moment of
execution, because Jira history can be altered by a project admin and auditors
will ask about that.

Then build the reverse reconciliation: every ScheduleKeyDeletion in CloudTrail
must map to an approved change. Any that does not is a finding. That reverse
check is what makes this a control rather than a procedure.

Alarm on every attempt against these actions, successful or denied. A denied
attempt is either confusion or something worse.
```

---

## Phase 9 — Evidence and cost

### Prompt 9.1 — Truveon ingestion

```
Read design/04-security-controls-and-evidence.md, section "Evidence pipeline",
and diagrams/evidence-flow.mermaid.

Build the push pipeline from AWS into Truveon, per tenant.

The completeness controls are the point of this prompt, not the transport:
- Sequence numbering per tenant stream, so gaps are detectable
- Heartbeat per tenant, so absence of data is an event rather than a quiet month
- Buffering via SQS or Kinesis, so a Truveon outage delays delivery rather than
  losing it
- Periodic count reconciliation between source and Truveon

A silently failed pipeline that looks like a clean month is the failure mode
that destroys an audit. Build against that.

Note: Truveon's own architecture is out of scope here. Treat it as an external
endpoint that may be unavailable.
```

### Prompt 9.2 — Cost allocation

```
Read design/10-commercial-model.md, sections "Cost visibility" and "Tagging".

Set up Cost Categories mapping account IDs to tenant, application, environment,
service model and compliance scope — derived from the platform registry, not
from resource tags.

Implement the four-tag enforced set via SCP, and the platform-managed tag set
applied by automation.

Do not build a comprehensive tagging scheme. The design is explicit that account-
level facts live in Cost Categories and the registry, and only four tags are
enforced at creation. Resist the urge to add more.

Build the monthly tenant cost report: raw AWS cost, flat fee, percentage uplift,
shown separately.
```

---

## Ordering note

Phases 0–2 are prerequisites for everything else. Phases 4, 5 and 6 can proceed
in parallel once the baseline is deployable. Phase 7 depends on Phase 2's
isolation security group and the Forensics account. Phase 8 can start any time
but has no value until there are deployments to reconcile.

**Do not begin Phase 3 (vesting) until the Phase 1 guardrail test harness passes.**
Vesting accounts behind guardrails that do not actually hold is worse than not
vesting them.

Phase 10 (developer access) extends Phase 1's SCPs and Phase 4's instrumentation,
so it follows both. Phase 11 (commercial) is independent and can run any time
after Phase 9's cost allocation. **Phase 12 (provisioning orchestration) comes
last** — it composes everything above, and its verification sweep is the
regression test for the whole platform. It cannot be meaningfully built before
the things it verifies exist.

**Phase 14 (partner layer) revises Phase 1 and Phase 12** rather than following
them. If building from scratch, fold 14.1 into Phase 1.1 and 14.3 into Phase 12.1
rather than treating them as later corrections — the OU structure and the saga
precondition are cheaper to build once than to retrofit.

**Phase 13 (environment tiers and scheduling)** is an exception to that ordering.
13.1 (the UAT tier) belongs with Phase 2, since it is baseline
parameterisation. 13.2–13.5 depend on Phase 4's instrumentation and Phase 6's
canaries and are best built alongside Phase 12, because the provisioning
verification sweep must exercise a full turndown/turnup cycle before go-live.

---

## Phase 10 — Developer access

### Prompt 10.1 — Permission sets and boundary

```
Read design/11-developer-access.md.

Build the developer permission sets and the permissions boundary.

Three environments, three grants — see the environment gradient table. The
production grant is larger than "logs and service health": it includes metrics,
ECS stopped reasons and exit codes, running configuration (task definition
revision, env var KEYS and parameter NAMES but never values), deployment history,
traces, and their own alarm state.

Denied in production without exception: any write action, any data plane access
(no S3 object reads, no database queries, no Secrets Manager values), env var
values, parameter values, and any other tenant's accounts.

The permissions boundary is the part most often missed. Write an SCP condition
requiring the platform-managed boundary on iam:CreateRole and iam:PutRolePolicy.
No boundary, no role. The boundary carries the same denies as the control-
protection SCP.

Developers get READ access to their own Config rules, alarms, findings and flow
logs. That is deliberate — do not remove it.
```

### Prompt 10.2 — Bypass controls

```
Read design/11-developer-access.md, section "Bypass controls".

Extend the Members OU SCP with the five bypass denials: region restriction,
networking outside the baseline VPC, cross-account trust in role trust policies,
and the snapshot/AMI sharing calls (ModifyDBSnapshotAttribute,
ModifySnapshotAttribute, ModifyImageAttribute).

Prioritise the region restriction. It is entirely innocent in intent and
produces a total visibility blind spot.

Verify the account-level CloudWatch Logs data protection policy actually covers
log groups created after deployment. This is the stdout control and it is the
one that matters most — test it rather than assuming.

Add these denials to the Phase 1 guardrail test harness.
```

### Prompt 10.3 — Telemetry gap detection

```
Read design/11-developer-access.md, section "Detecting loss of visibility".

Build detection for the case where an agent is stopped on a live instance.

The signal is not absence of data. It is absence of data from something
demonstrably alive: status checks passing, flow logs showing traffic, telemetry
silent. Implement the discriminator table exactly — the other scenarios must not
produce this alarm.

Components:
- Missing-data alarms with treatMissingData: breaching on mem_used_percent as
  the sentinel metric (it exists only if the agent runs)
- A scheduled log-stream staleness check on lastIngestionTime, since CloudWatch
  Logs does not alarm on absence natively
- An expectation registry: what SHOULD be publishing. Absence is only detectable
  against a declared expectation. Source this from the instrumentation layer.
- Correlation with Session Manager StartSession CloudTrail events for attribution

Noise control is required, not optional: maintenance window suppression, a grace
period longer than a normal agent restart, and escalation tied to the SSM
association — a gap that survives the next association run is the real signal.

In dev this is a digest line, not a page.

Also: alarm on any inbound connection on port 22 appearing in flow logs. There
is no SSH path in this architecture, so its presence is itself a finding.
```

---

## Phase 11 — Commercial access

### Prompt 11.1 — Billing access and reporting

```
Read design/12-commercial-access.md.

Set up the commercial access path:
- CUR export to S3 in a dedicated reporting account
- Delegated Cost Explorer access
- CommercialReadOnly permission set

Critical constraint: this grant includes NO member account access of any kind.
Not read-only, not via the aggregation account. Billing and cost data only.

Do not grant access to the Organization management account to solve the
Organization-level data problem. Use the aggregation layer.

Build the monthly per-tenant cost report (raw AWS cost, flat fee, percentage
uplift, shown separately) and the portfolio view by service model.
```

### Prompt 11.2 — Client-conversation event class

```
Read design/12-commercial-access.md, section "The client-conversation event
class", and design/06-observability.md routing.

Add the fourth routing destination as a PagerDuty business service with
notification rules, not paging rules.

Route the events in the table. Immediate vs batched per the table — outage and
SLA-credit events go immediately because those are the ones where a customer
calls before the commercial team has heard.

IMPORTANT: security incident events route here as AWARENESS ONLY. The commercial
team must know an incident is in progress so they are not blindsided. They must
not be the notifying party — notification is legally governed by the BAA clock
and the record that matters is the one Truveon holds.

Make "informed" and "notifier" structurally distinct in the routing so the
distinction cannot erode under pressure.
```

---

## Phase 12 — Provisioning orchestration

### Prompt 12.1 — The saga engine

```
Read design/13-client-provisioning.md and diagrams/provisioning-saga.mermaid.

Build the provisioning orchestrator as a Step Functions saga. This is NOT a
pipeline — none of it can be transactionally rolled back. You cannot un-create
an AWS account.

Required properties:
- Idempotent: every step safely re-runnable
- Resumable: step 9 can run after step 7 failed at 2am
- Fails closed: a half-provisioned tenant is worse than none because it looks
  supported
- Self-cleaning: failed provisioning is unwound by the deprovisioning path

The registry record is the spine. Create it FIRST, in provisioning state.
Nothing else starts until it exists. Every system writes its identifiers back to
it — AWS account IDs, Jira project key, Truveon tenant ID, PagerDuty service and
routing key IDs, Cost Category mapping.

Follow the ordering in design 13 exactly. External systems (Truveon, PagerDuty,
Jira) come before AWS deliberately — they are most likely to fail on credentials
or permissions, and failing before AWS accounts exist is far cheaper.

Implement the state machine: provisioning → provisioned → verifying → live, with
blocked as a terminal failure state. provisioned and live are deliberately
different: SLA measurement, billing, and on-call rotation all key off the
transition to live.
```

### Prompt 12.2 — PagerDuty provisioning

```
Read design/13-client-provisioning.md, section "PagerDuty — near-total".

Use the official PagerDuty Terraform provider. Configuration lives in the
repository alongside the CloudFormation; a tenant's on-call setup is a module
invocation.

Create per tenant: service (production only), escalation policy, routing keys
written to Secrets Manager, and the business service for the commercial event
class.

Treat it exactly like the AWS baseline — versioned, staged, drift-detected. A
manual console change to a provisioned service is a finding; build that
detection.

Authenticate as a platform service account with tokens in Secrets Manager,
rotated. Not a personal account.

Handle rate limits with backoff and resume, not saga failure.
```

### Prompt 12.3 — Jira provisioning

```
Read design/13-client-provisioning.md, section "Jira — automatable in one
direction".

Scope this narrowly and deliberately.

DO automate: project creation inheriting the shared configuration scheme, the
onboarding epic and its task set, the recurring obligation tickets (annual pen
test, annual matrix review, quarterly access review), and reading change records
for reconciliation.

DO NOT automate per-project workflow, screen, custom field, permission scheme or
request type configuration. One golden scheme is built by hand and shared. A
hundred tenants with independently configured workflows is a reconciliation
nightmare because the change engine would have to understand a hundred approval
semantics.

If you find yourself writing code to configure a workflow, stop and flag it.

Verify current Atlassian API capability before building — this area has been
changing, and Cloud and Data Center differ.

Idempotency: check-then-create, keyed on the registry ID stamped into a label or
custom field.
```

### Prompt 12.4 — Human task gating

```
Build the human task layer.

The pipeline creates tasks in Jira and waits on the blocking set. Blocking and
non-blocking sets are in design 13.

The distinction is load-bearing: if everything blocks, nothing goes live; if
nothing blocks, something goes live without a BAA.

Blocking tasks gate the transition to verifying. Non-blocking tasks are created,
owned and dated but do not gate.

Surface blocked-on-human state clearly — an account sitting in provisioned for
three weeks waiting on a signature should be visible, not silent.
```

### Prompt 12.5 — Verification sweep

```
Read design/13-client-provisioning.md, section "The verification sweep". This is
what makes provisioning a control rather than a script.

Implement all eight checks. Each must actually exercise the thing, not assert
configuration exists:
- Truveon received evidence from all three accounts, sequence clean
- A synthetic test alarm actually reached PagerDuty AND paged the correct
  rotation — send one, verify receipt
- Canary running and passing
- A test change reconciled correctly against Jira — make one, verify it matched
- Backup policy attached and a first recovery point exists
- Phase 1 guardrail harness passes against these specific accounts
- Cost Category mapping resolves and the account appears in the commercial
  by-customer view
- BAA notification window is set and non-null

FAIL CLOSED. Any failure blocks go-live. Do not raise a ticket and proceed.

The BAA check matters more than it looks — an unset value means the incident
notification clock cannot run, and that would otherwise be discovered during an
incident.
```

### Prompt 12.6 — Deprovisioning

```
Build deprovisioning now, not later.

Same machine, reverse order, reading the same registry record.

Three consumers:
1. Contractual exit (design/10-commercial-model.md)
2. Cleanup of failed provisioning — without this, a run that dies at step 6
   leaves debris someone unpicks by hand, badly
3. Scope change: a dev account upgraded to production-equivalent posture because
   regulated data turned up in it. Same orchestration, different target state.

For exit specifically: the data extraction path must be real and tested —
application data, tenant logs from the archive, and the Truveon tenant if
retained. Verify nothing of AltDigital's is entangled in the member account
requiring surgical removal.

Produce an exit evidence record.
```

---

## Phase 13 — Environment tiers and scheduling

### Prompt 13.1 — UAT tier in the baseline

```
Read design/14-environment-tiers.md.

Extend the baseline StackSet parameterisation to support four environment tiers
instead of three. UAT is opt-in and is NOT provisioned by default.

The key thing to get right: UAT splits the two axes that dev and test keep
aligned.
- Data handling: PRODUCTION-EQUIVALENT — in compliance scope, full retention
  floor, full detective controls at production severity, regulated data expected
- Resilience: NON-PRODUCTION — single AZ, no SLA, reduced backup frequency, no
  PagerDuty routing, schedulable

Do not implement UAT by copying the test parameterisation and adjusting. Build
the tier table in design 14 as explicit configuration so the two axes stay
visibly separate.

UAT is included in restore testing and gets a canary. Both follow from the data
being real.

Also: regulated data in UAT must NOT trigger the scope-change finding that
applies to dev and test. UAT is in scope from the outset.
```

### Prompt 13.2 — HOOP scheduler

```
Read design/14-environment-tiers.md, section "HOOPs".

Build the scheduler. Available for dev, test and UAT. Never production —
enforce that, do not rely on configuration.

Turndown: EC2 stop, RDS/Aurora stop, ECS desired count to 0, ASG min and desired
to 0. Storage, load balancers and NAT persist.

CRITICAL — the RDS seven-day problem: AWS automatically restarts a stopped RDS
instance or Aurora cluster after 7 days. The scheduler must detect and re-stop
these, and must RECORD each occurrence. Silent auto-restart produces a cost
report nobody can explain.

Surface auto-restart events in the cost report with a recommendation: this
environment was auto-restarted N times this month, consider seasonal teardown
instead of scheduling.

Manual turnup must be triggerable at any time through the same code path, by the
customer's nominated contact as well as by AltDigital. A team working an unplanned
weekend should not need a ticket.

Time zone comes from questionnaire 3a.7. Do not assume UTC.
```

### Prompt 13.3 — Turndown/turnup testing

```
Read design/14-environment-tiers.md, section "Turndown and turnup must be
tested". This is design principle P4 applied to scheduling.

Turnup is the higher risk. A failed turndown costs money; a failed turnup costs
a QA team or a customer's user group their working day.

Implement the six tests in the table. Specifically:
- Turndown completeness: everything that should stop, stopped; nothing that
  should persist, terminated
- Turnup success: full expected resource set running and healthy
- Turnup duration: trended, so creep is visible before it becomes a complaint
- Functional validation: the canary passes after turnup — running is not the
  same as working
- Monthly full cold cycle in a safe window
- Monthly data integrity check across the cycle

PARTIAL TURNUP is the dangerous state — some resources up, some not, looks
available and isn't. The validation must assert the full expected set, not just
the absence of errors.

Turnup failure is an incident. In UAT it pages. In dev and test it raises a
ticket and notifies the dev team contact immediately, not in the next digest.
```

### Prompt 13.4 — Scheduling interactions

```
Read design/14-environment-tiers.md, section "Interaction with the rest of the
platform". Scheduling touches more systems than it appears to.

Wire up all eight interactions:
1. Alarm suppression during scheduled down, or every turndown pages
2. Canary pause and resume, with resume doubling as turnup validation
3. Telemetry gap detection must recognise scheduled-down as an explicit known
   state. This is the one most likely to be missed — the gap detector
   distinguishes "instance alive, telemetry silent" from other cases, and a
   scheduled stop must not look like an agent being killed
4. Restore test scheduling must fall inside the HOOP
5. Patch and maintenance windows must fall inside the HOOP, or patching silently
   never runs. Validate this at vesting and flag a conflict rather than
   accepting it
6. Backup continues regardless — it targets storage, not running compute
7. A deployment attempted against a stopped environment is a legitimate failure,
   not a change-reconciliation finding
8. Cost reporting shows actual vs expected HOOP hours, with auto-restart events
   called out

For 5 specifically: a maintenance window outside the HOOP is a silent failure of
patch management. Detect it at configuration time.
```

### Prompt 13.5 — Dormant lifecycle

```
Read design/14-environment-tiers.md, section "Seasonal lifecycle", and
design/13-client-provisioning.md state machine.

Add the dormant state and the teardown/re-vest cycle. This is the deprovisioning
path doing useful work outside of exit, and it sidesteps the RDS seven-day
problem entirely.

Teardown: final backup taken AND VERIFIED, environment torn down, account
retained in dormant state, registry record preserved with all parameters,
evidence retained in Truveon with the retention clock continuing.

DO NOT delete the AWS account. Deleting and recreating loses the account ID,
evidence continuity and the Truveon linkage. Dormant accounts retain everything
and cost almost nothing.

Re-vest: rebuild from the same registry record, restore data from the retained
recovery point, run the verification sweep before returning to live.

Re-vest must be tested on a schedule, not just exercised when a customer needs
it. A seasonal environment that cannot be brought back is a far worse failure
than one that was never torn down.
```

---

## Phase 14 — Partner layer

### Prompt 14.1 — Partner and client OU structure

```
Read design/15-partner-model.md.

Revise the OU structure from Phase 1.1 to insert two levels beneath Members:
partner, then client. Accounts sit inside the client OU.

Reserve the direct/ branch for AltDigital's own clients from the outset. Without
it the first direct client gets bolted on awkwardly.

Account alias naming: ad-<partner>-<client>-<env>, with an application segment
only where a client has more than one application. Lowercase alphanumeric and
hyphens only — aliases permit nothing else and are globally unique across all of
AWS. Validate the prefix is actually available before committing to it.

Account NAME in Organizations may be human-readable; the alias is the machine
identifier. Keep them deliberately distinct.

Email: plus-addressed from msp-mgmt@altdigital.ai. Verify the provider delivers
plus-addressed mail to the base mailbox before relying on it — a bounced root
address on account creation is a bad failure.

Emit partner and client OU ids to SSM Parameter Store alongside the existing OU
ids.
```

### Prompt 14.2 — Partner onboarding

```
Read design/15-partner-model.md, section "Partner onboarding".

Build partner onboarding as a separate, one-time process distinct from client
onboarding.

Creates: partner OU, partner-level SCPs if any, Cost Category dimension, Truveon
tenant for the partner, Jira project or component, commercial access group in
Entra scoped to the partner OU, and a registry entry in partner state.

Captures the field list in design 15 — notably the partner's DOWNSTREAM SLA
commitments to their clients, because AltDigital's commitment must be at least as
strong, and the BAA notification window, because AltDigital's must be materially
tighter than the partner's.

Validate the back-to-back relationship at onboarding: if the partner's downstream
SLA is stronger than what AltDigital is committing, flag it rather than accepting
it silently. Same for the notification windows.
```

### Prompt 14.3 — Partner precondition in the saga

```
Read design/13-client-provisioning.md, section "Partner precondition".

Add step 0 to the provisioning saga: verify the partner exists, is active, and
has a non-null notification window.

This BLOCKS, it does not warn. An account live beneath an incomplete partner
relationship has no defensible notification path.

Re-check at the go-live gate as well as at provisioning start — a partner
relationship can lapse between the two.

Extend the verification sweep: notification windows set and non-null at BOTH
levels, and AltDigital's is tighter than the partner's. Assert the ordering, do
not just check for presence.
```

### Prompt 14.4 — Chained notification clocks

```
Read design/15-partner-model.md, section "Notification clocks run in series",
and design/07-response-automation.md.

Extend the notification clock to run two windows simultaneously from the same
immutable discovery_time: AltDigital→partner, and partner→client.

The incident view must show the CHAIN, not just the next deadline. Someone
looking at the incident needs to see that AltDigital has 24 hours and the partner
then has 48, not just "24 hours remaining".

AltDigital notifies the partner. The partner notifies the client. Encode that —
the informed/notifier distinction from design 12 now applies at two levels, and
it must not be possible for the system to record AltDigital as having notified a
client.
```

### Prompt 14.5 — Two-level billing roll-up

```
Read design/15-partner-model.md, section "Billing rolls up twice", and
design/12-commercial-access.md.

Extend Cost Categories to derive both dimensions from the account ID: client (for
the partner's invoicing to their clients) and partner (for AltDigital's invoicing
to the partner).

Add a partner-scoped variant of CommercialReadOnly: same permission set, scoped
to a single partner OU. Partner business users see their own book and nothing
else — no other partner's data, no portfolio view, and no AWS access beyond
billing.

Test the isolation explicitly. A partner seeing another partner's spend is a
commercial incident.
```

### Prompt 14.6 — Dual-hat segregation of duties

```
Read design/15-partner-model.md, section "Segregation of duties for dual-hatted
staff".

The existing key-deletion quorum is an intra-AltDigital control and is
unaffected. Do not change it.

What to build: for any action where separation between AltDigital and a partner
is claimed, enforce that the requesting and approving individuals are different
people regardless of which entity they act for.

The specific action list is an open item (B12) and not yet defined. Build the
enforcement mechanism and the identity-to-entity mapping so the list can be
populated without a code change. Do not invent the list.
```