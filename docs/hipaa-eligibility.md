# HIPAA-eligible service enforcement — design

**Status: APPROVED, 2026-10-04 (Jamie). Build in progress — B-159 to B-165.**
D-030 is registered in [deviations.md](deviations.md). Open items E1–E12 are
in §9; the ones still marked open are decisions, not build work.

**Naming rule** (Jamie, 2026-10-04): every build element carries the framework
it enforces — `hipaa-eligible-services.tsv`, `PlatformHipaaEligibleOnly`,
target `hipaa-enforced`, evidence stream `hipaa-eligibility`, override
`OVERRIDE#hipaa-eligibility:<prefix>`. The `eligibility/` directory is the
mechanism and is framework-neutral; what sits in it is not. A second framework
with its own service list becomes a second profile beside HIPAA rather than a
rename (E12).

---

## Scope: who this touches

**Platform, built once:** everything in B-159 to B-165. There is no
Arc8-specific code.

**Per HIPAA tenant, data only:** the registry `compliance_scope` containing
`hipaa` (already set from questionnaire 4.1), the policy attachment on its
uat/prod accounts (made by vesting), exception records, digest lines, evidence
records.

**Tenants without HIPAA exposure: nothing changes.** No SCP attached, the
sweeps do not scan their accounts, no digest lines, no tickets. They keep
exactly today's guardrails — region, tags, boundary — and may use any service
AWS offers. This is deliberate: the control exists because of a BAA, and an
account outside the BAA has no reason to carry it.

**The one bridge:** an account that declared no HIPAA exposure and is found
holding regulated data goes through design 11's scope-change procedure. That
upgrade adds `hipaa` to the account's `compliance_scope`, and this control
comes with it. The app acquired HIPAA exposure, so it acquires HIPAA rules.

### The HIPAA profile in the provisioning process

Triggered by questionnaire 4.1 containing `hipaa`, and nothing else:

| Stage | What changes |
|---|---|
| **Intake** | 4.1 states the consequence: uat and prod are limited to AWS HIPAA-eligible services, and dev warns first. The tenant learns it before signing, not at UAT. |
| **Vesting** | uat/prod accounts get `PlatformHipaaEligibleOnly` attached; go-live blocks without it (B-165) |
| **Kickoff briefing** | Eligibility sits beside the tagging requirement already briefed (SCP 04) |
| **Build** | Dev digest names non-eligible use as it happens (B-161) |
| **Ongoing** | Exceptions by approval (B-162); evidence to Truveon (B-163) |

Sources read for this note: `CLAUDE.md`; design 10 (tagging), 11, 14, 15;
`policies/scp/README.md`, `attachments.tsv`; `docs/truveon-ingestion-contract.md`,
`scale-watch.md`, `deviations.md` (D-028, D-029); `design/open-items.md`;
`completed_qs/oeight.questionnaire.yaml` and the Arc8 questionnaire;
`alerting/src/router.py`; `registry/10-registry-table.yaml`; `scripts/registry.sh`.
The AWS reference page was fetched once (see §1, "What was fetched").

---

## 0. The problem, and what the design does about it

Under the AltDigital ↔ AWS BAA, PHI may be held only in services on the AWS
HIPAA Eligible Services Reference. AWS's own scope statement on that page:
*"Services not listed may still be used for non-ePHI purposes within HIPAA
accounts."* That sentence matters. It is why a non-eligible-service carve-out
list (§3) and an exception path (§5) are legitimate, not loopholes.

Arc8 is built by partner contractors. If nothing tells them which services
are allowed, the first they hear of it is a production deploy that fails in a
way that is hard to trace. The design moves that discovery to dev:

| Tier | Preventive | Detective | Routing |
|---|---|---|---|
| dev | none | daily sweep | digest (design 14) |
| test | none | daily sweep | digest + Jira |
| uat | **SCP deny** | daily sweep + hourly attachment check | Jira + commercial |
| prod | **SCP deny** | daily sweep + hourly attachment check | PagerDuty for control failure (§4.4) |

Scope is **accounts whose registry `compliance_scope` contains `hipaa`**. For
those accounts the SCP attaches to uat and prod only. It also attaches to any
dev or test account whose posture is upgraded by design 11's scope-change
procedure. **Never Members-wide.**

---

## 1. Source of truth

### The file

`eligibility/hipaa-eligible-services.tsv`. It is a new directory, created by
its own phase (B-159) as CLAUDE.md requires. It is one file, because the SCP,
the sweep, the developer guide and the evidence all read it. Splitting it
would give those four consumers a way to disagree.

| Column | Example | Why it exists |
|---|---|---|
| `service` | `Amazon Simple Queue Service (SQS)` | **Verbatim** AWS page name, bracket qualification included. The drift check diffs on this column, so it must match the page exactly. |
| `on_aws_list` | `yes` / `no` | Whether the row mirrors the page. Only `yes` rows take part in the drift diff. *(As built: the bracket qualification stays inside `service`, verbatim, rather than in a column of its own. Splitting it would have meant re-joining it for every diff; the diff reports a changed bracket as `requalified` instead.)* |
| `status` | `eligible` / `carve-out` / `pending` / `not-allowlisted` / `platform-only` | Keeps "eligible" and "not eligible, holds no PHI" as different values, never one list (§3). |
| `iam_prefixes` | `sqs` | What the SCP's `NotAction` is built from. One row may carry several. |
| `config_namespaces` | `AWS::SQS`, or a full type such as `AWS::ECR::PublicRepository` (most specific wins) | What the inventory sweep matches (§4.2). This is a different vocabulary from IAM prefixes. |
| `deny_actions` | `ds:CreateDirectory` | Action-level denies for an excluded feature that maps to distinct actions (§2.3). |
| `alternative` | `ECS on Fargate` | What the developer is told to use instead (§7). Only on non-eligible rows. |
| `date_added` | `2026-10-xx` | When the row entered *this file*. AWS publishes no per-service date, so this is not the date AWS added the service. |
| `reviewed_by` | opaque id | Who approved the row. Not a name: a personal identifier would break the repository rule. |
| `notes` | `prefix shared with EC2` | Why the mapping is what it is, wherever it is not obvious. |

A header line carries `list_version: <page "Last Updated" date>/<file
revision>`. Evidence (§6) records that value as the list version in force.

### What was fetched, and why it does not seed the file

On 2026-10-04 the page reported **"Last Updated: September 3, 2026"** and 174
entries. The fetch went through a summarising tool, so the transcription
cannot be relied on. The entries that look newest (Kiro, AWS MCP Server, Nova
Act, DevOps Agent) are the ones most worth checking by eye. **B-159 builds the
file by hand from the page, not from this fetch.** The analysis in §2 and §3
uses the fetch only to find the *classes* of mismatch. Every specific
eligibility claim gets re-checked when the file is built.

### Review cadence

- **Daily, automated:** a drift check (below). *As built: daily, not weekly, because an alarm cannot look back more than seven days and the check's own silence must be alarmable. Repeats are suppressed to one report a week until fixed.*
- **Quarterly, human:** a reviewer re-reads the page against the file, even
  with no diff, and records a `list-reviewed` evidence record (§6). An
  auditor will ask how you know the list is current. "The scraper said
  nothing changed" is a weaker answer than a dated human review.
- **On drift:** within 10 business days. Proposed default, decision E6.

### Drift check — flags, never regenerates

A scheduled Lambda (`eligibility/src/upstream.py`). *As built: in Audit, not Platform Tooling — beside the router's bus, so nothing crosses an account on the reporting path; Audit Lambdas already reach the internet for Truveon and PagerDuty.* Each day it:

1. Fetches the page and parses the service list and the "Last Updated" date
2. Diffs the parsed list against the `on_aws_list=yes` rows of the catalog it was packaged with, after normalisation (dashes, whitespace, the page's literal `
`). *As built: no snapshot bucket. The catalog in the package is the stable base; a snapshot would only have recorded the page, which the Wayback Machine and the page itself already do.*
3. Treats a parse with no footer marker, fewer than 100 services, or a swing of more than 10% against the catalog as **blind**, never as a diff
4. Emits one event to the router per outcome:

| Outcome | Severity | Meaning |
|---|---|---|
| No change, parse healthy | none; heartbeat only | |
| Service **added** | low | An opportunity: something new may be allowlisted |
| Service **removed**, or qualification **narrowed** | **high** | Existing usage may now be non-compliant. Ticket against AltDigital. |
| Qualification text changed otherwise | low | Human reads it |
| **Parse failed, zero services, or count moved by >10%** | **high** | The check is blind. A scraper that silently returns "no change" is the failure this design most needs to avoid. |
| Lambda did not run | n/a | Covered by the existing heartbeat/cadence watch (`alerting/40-cadence.yaml`) |

**It never edits the file and never regenerates the SCP.** A scraped list
deployed automatically would hand the preventive control to whatever HTML
AWS's site serves that day. The only path from upstream change to deployed
policy is: human edits the TSV → PR → generator → `validate-policies.sh` →
harness → `deploy-policies.sh`.

Alternative considered: a GitHub Actions workflow. Rejected for two reasons.
It sits outside the router and the evidence ledger, so a failed run would
reach GitHub notifications and not the on-call path. And its run history is
not evidence Truveon holds.

---

## 2. Service → prefix mapping

The page lists *services and features*. SCPs work on *IAM service prefixes*.
The two do not line up one-to-one. There are five classes of mismatch, each
handled differently. Examples come from the fetched list and the platform's
own templates. **Each needs confirming when B-159 builds the file. None is
asserted here as final.**

### 2.1 One eligible service, many prefixes: allowlist all of them, *if* they are the same service

| Service on the list | Prefixes | Uncertain members |
|---|---|---|
| Amazon CloudWatch | `cloudwatch` | `synthetics`, `rum`, `application-signals`, `evidently`, `internetmonitor`, `networkmonitor`, `oam`. **Are these "generally available features of CloudWatch" (covered by the page's catch-all clause) or separate services (not listed)?** |
| Amazon EventBridge | `events` | `scheduler`, `pipes`, `schemas` |
| AWS Systems Manager | `ssm`, `ssmmessages`, `ec2messages` | `ssm-incidents`, `ssm-contacts`, `ssm-quicksetup`, `ssm-guiconnect` |
| Amazon Cognito | `cognito-idp`, `cognito-identity`, `cognito-sync` | — |
| AWS IAM Identity Center | `sso`, `sso-directory`, `identitystore` | `sso-oauth`, `identitystore-auth` |
| Amazon Kinesis (×3 rows) | `kinesis`, `firehose`, `kinesisvideo` | — |
| Managed Service for Apache Flink | `kinesisanalytics` | — |
| Amazon MSK | `kafka`, `kafka-cluster` | — |
| Amazon Route 53 | `route53`, `route53resolver` | `route53domains`, `route53profiles` |
| Application Recovery Controller | `route53-recovery-*`, `arc-zonal-shift` | — |
| Amazon S3 | `s3` | `s3express`, `s3tables`, `s3-object-lambda`, `s3vectors` |
| Amazon OpenSearch Service | `es` | `aoss` (Serverless) |
| Amazon EMR | `elasticmapreduce` | `emr-serverless`, `emr-containers` |
| Amazon Redshift | `redshift`, `redshift-data` | `redshift-serverless` |
| Amazon Timestream | `timestream` | `timestream-influxdb` |
| Amazon Inspector | `inspector2` | `inspector`, `inspector-scan` |
| API Gateway | `apigateway`, `execute-api` | — |
| WAF | `wafv2`, `waf`, `waf-regional` | — |
| AWS Backup | `backup` | `backup-storage`, `backup-gateway` |
| Amazon Connect | `connect` | `profile`, `wisdom`/`qconnect`, `cases`, `voiceid`, `app-integrations` and others |

**Rule:** the "Uncertain members" column goes in only on documented evidence
that AWS treats the prefix as part of the listed service. "It's in the same
console" is not evidence. Until confirmed, an uncertain prefix is
`not-allowlisted`: denied in uat/prod, flagged in dev/test. If that is wrong
it fails closed, and the cost is an exception request.

**Two of these block the platform itself, so they are open item E1:**

- `synthetics`. Design 14 runs canaries in uat and prod, and
  `canaries/10-canary.yaml` uses the prefix. **This is the most important
  item in the note.** A canary takes screenshots and HAR files of the pages
  it visits. Pointed at an authenticated page in prod, it stores PHI. If
  Synthetics is not eligible, canaries in HIPAA accounts must be confined to
  pages that render no PHI. That needs the canary design changed, not an
  exception filed.
- `application-signals`. Used by platform templates (design 11 production
  visibility).

### 2.2 One prefix shared with unlisted things

| Prefix | Listed as | Also covers | Consequence |
|---|---|---|---|
| `ec2` | EC2, EBS, VPC, Verified Access | Transit Gateway, Client VPN, Traffic Mirroring, and so on | Allowlisted as a whole. These are features of listed services, covered by the page's GA-features clause. |
| `rds` | RDS (six engines), Aurora | DocumentDB and Neptune *management* APIs use `rds:` | Every engine reachable through `rds` is on the list. No gap today. If one appears, `rds:DatabaseEngine` can split it. |
| `amplify` | "AWS Amplify **Console**" | All of Amplify (Gen 2, backend) | **Mismatch.** The list names a subset; the prefix grants the whole. E2. |
| `sagemaker` | SageMaker AI, Augmented AI | Studio Lab, workforce configuration | Exclusions are feature-level (§2.3) |
| `quicksight` | "Amazon Quick, web only" | Mobile access | Not enforceable by SCP. Detective only, if at all. |
| `cloudfront` | CloudFront, excluding Embedded PoPs | Embedded PoP delivery | Not enforceable. Irrelevant to Arc8. Recorded and not acted on. |

### 2.3 Feature-scoped exclusions: action-level deny where the feature has its own actions, detective where it does not

| Exclusion on the page | Enforceable? | How |
|---|---|---|
| Directory Service: Simple AD | **Yes** | `ds:CreateDirectory` is the Simple AD call (`CreateMicrosoftAD` is separate). Goes in `deny_actions`. |
| Pinpoint/End User Messaging: WhatsApp | **Yes** | WhatsApp is its own prefix (`social-messaging`). Left off the allowlist. |
| Pinpoint/End User Messaging: voice messages | Probably | `sms-voice:SendVoiceMessage` and the voice-channel actions. Confirm the action list (E3). |
| SageMaker / A2I: public and vendor workforce | **Yes** (as built) | `sagemaker:WorkteamType` is a condition key on `CreateFlowDefinition` and `CreateLabelingJob` (Service Authorization Reference, 2026-10-04). Conditional deny on `public-crowd`, `vendor-crowd`. |
| SageMaker: Studio Lab, Ground Truth Plus | Partly | Studio Lab is a separate product with no IAM surface in the account. Ground Truth Plus may be `sagemaker-groundtruth-synthetic`; confirm. |
| Fargate: ECS and EKS engines only | n/a | Those are the only engines |
| RDS: six engines only | n/a | All RDS engines are listed (§2.2) |
| WorkDocs, Managed Services, Kiro Web | n/a | Not used, and not enforceable at the prefix. Recorded only. |

### 2.4 Services that need an agreement beyond the BAA: off the allowlist

- **Alexa for Business** ("requires Alexa Skills BAA"). Also discontinued.
- **Amazon Connect Health** ("HCLS Addendum applies"). Its prefix is unknown (E3).

Both are `not-allowlisted` until AltDigital holds the extra agreement. Being
on the page does not make them usable under *our* BAA.

### 2.5 Prefixes with no eligible row and no carve-out

Denied in uat/prod. Detected in dev/test. Arc8 will plausibly reach for
these, each with an `alternative`:

| Prefix | Likely reason a developer reaches for it | Alternative |
|---|---|---|
| `apprunner` | Simplest container hosting | ECS on Fargate |
| `lightsail` | Simplest anything | EC2 / ECS |
| `ec2-instance-connect` | SSH | Session Manager. Design 11 already says there is no SSH path. |
| `codeartifact` | Private Python packages | ECR (OCI artifacts) or S3; confirm need |
| `codestar-connections` / `codeconnections` | CodePipeline ↔ GitHub | Arc8 deploys by GitHub OIDC (B-142), so not needed |
| `q`, `codewhisperer` | Amazon Q Developer | Q *Business* (`qbusiness`) is listed; Q Developer is not |
| `ecr-public` | Public image pulls | Pull-through cache into private ECR |

---

## 3. Control-plane carve-outs: not eligible, holds no PHI

`status = carve-out`. A carve-out is **not** a statement that the service is
eligible. It is a statement that the service cannot hold or process PHI, so
the BAA does not reach it. If one of these is later found on the eligible
list, its row becomes `eligible` and the carve-out entry is deleted. The two
statuses are never merged.

Candidates. Each is confirmed absent from the list when B-159 builds the file:

| Prefix | Reason it holds no PHI and is needed |
|---|---|
| `iam` | Identities and policies. Every account needs it. |
| `sts` | Credential issuance. Nothing works without it. |
| `signin` | Console sign-in |
| `access-analyzer` | IAM analysis of policies, not data |
| `account` | Account contacts and region settings (B-139) |
| `health` | AWS service health events |
| `support`, `supportapp` | Support cases. **Rule: no PHI in a case**, stated in the developer guide. A case can hold free text, which makes this the weakest carve-out. |
| `trustedadvisor` | Configuration checks |
| `servicequotas` | Quota reads and requests (B-140, B-151) |
| `tag`, `resource-groups` | Tag reads and writes. Tags must never carry PHI, already a design 10 rule. |
| `application-autoscaling` | ECS service scaling. Moves capacity numbers, not data. |
| `ce`, `cur`, `budgets`, `billing`, `aws-portal`, `payments`, `invoicing`, `tax`, `pricing`, `freetier`, `bcm-data-exports`, `cost-optimization-hub`, `savingsplans` | Cost and billing. Design 10 budgets and the commercial view. |
| `compute-optimizer` | Sizing recommendations |
| `notifications` | AWS User Notifications configuration |
| `uxc` | Console settings |

**Explicitly not a carve-out:**
- `ec2-instance-connect`. It is an SSH path, which design 11 forbids anyway.
- `synthetics`. It captures page content (§2.1, E1).

### Platform-only carve-outs

If E1 resolves `synthetics` and `application-signals` as **not** eligible,
the platform still needs them in uat/prod, and the tenant should not get
them. The SCP handles this with two statements, so the prefix list is not
duplicated:

```
Statement A  Deny  NotAction [eligible + carve-out + platform-only]   → everyone
Statement B  Deny  Action    [platform-only]                          → everyone except named platform principals
```

Statement B is short and exempts **named principals in the v2 shape (B-135)**:
exact `arn:aws:iam::*:role/<Name>`, no leading wildcard, derived by
`scripts/derive-scp-exemptions.py`. **`TenantAppDeployer-*` must never appear
in that list.** It is the main path by which resources reach uat and prod,
and exempting it would be B-136 again.

### No principal is exempt from Statement A

That includes `OrganizationAccountAccessRole` and `stacksets-exec-*`.

Every prefix the platform needs is either eligible or a carve-out. An
exemption would give a principal *every* non-eligible service, which is far
broader than the specific prefixes the platform needs. It would also mean
AltDigital's own automation could put PHI somewhere the BAA does not cover,
with nothing to stop it.

**Before attach, B-160 proves this from evidence, not from the template
grep:** IAM service-last-accessed for every platform role in Arc8 dev and test
over 30 days. Any platform role touching a prefix that is neither eligible nor
carved out is a finding to resolve before attach. Resolving it by adding an
exemption is not an option.

---

## 4. Enforcement and detection

### 4.1 Where the SCP attaches — the size question is the wrong one

**Re-verified live 2026-10-04:**
- Members OU carries **9 SCPs**.
- The OEight partner OU and the OEight client OU (`/platform/org/ou/members/oeight/oeight`) carry FullAWSAccess only.
- Arc8 dev (624606539840) and test (904883674541) each carry FullAWSAccess only, directly.
- Arc8 has `uat_required: false`. **Arc8 prod does not exist yet** (B-140 quota case).

The per-target limit of 10 comes from the repository. It could not be read
from the Service Quotas API (the lookup returned no such quota). E10.

**A standalone policy fits.** Estimate:

| Part | Bytes |
|---|---|
| ~180–220 prefixes as `"x:*",` at ~14 bytes each | ~2,500–3,100 |
| Carve-outs | ~350 |
| Statement B and the exemption list (03-v2's largest list is 406 bytes for 9 principals) | ~550 |
| `deny_actions` statement | ~150 |
| Scaffolding | ~150 |
| **Total** | **~3,700–4,300 against 5,120** |

That is minified on the wire by `deploy-policies.sh`, which already does
this. The generator measures the real figure. What does not fit is an
*extension* of an existing Members policy, and Members is the wrong target in
any case: it would hit every tier of every tenant.

| Option | Verdict |
|---|---|
| **(a) Client OU** | Rejected. It holds every tier, so dev and test would be blocked. This contradicts the brief. |
| **(a′) Enforced child OU under the client** (`oeight/oeight/enforced/`, holding uat and prod) | Workable but worse. It adds an OU level design 15 does not draw. A design 11 scope-upgraded dev account would have to be *moved* to get the control. And an account with an exception (§5) would need an OU of its own. |
| **(b) Attach directly to each enforced account; target set derived from the registry** | **Recommended** |
| **(c) Prefix compression** (`iot*:*`, `kinesis*:*`) | Rejected. A wildcard prefix automatically allows any *future* service that matches, before AWS lists it. It also saves a few hundred bytes the policy does not need. |
| **(d) Allow-list mode** (detach FullAWSAccess at the account and attach Allow-only policies, which union at one level) | **Fallback only, if (b)'s policy overflows.** Splits across policies, which deny-mode cannot. Detaching FullAWSAccess from a live prod account is a sharp operation: attach-before-detach, same discipline as the B-135 swap. |

#### Why (b)

1. Compliance scope by tier is **a property of the account**. Design 10 puts
   account properties in the registry, not in the OU tree. The registry
   already holds `compliance_scope` and `environment` per account, so the
   target set is derived with nothing hardcoded:
   `hipaa ∈ compliance_scope AND (environment ∈ {uat, prod} OR posture=upgraded)`.
2. Design 11's scope upgrade (regulated data found in dev) becomes *attach a
   policy*, not *move a live account*.
3. Per-account exception variants (§5) attach where the account is, with no
   per-exception OU.
4. Smallest blast radius: one account per attachment. Each account has 9 free
   slots.
5. Design 15's OU tree is unchanged.

#### Cost of (b)

- Attachment lives in the registry, not in the visible OU structure.
- `deploy-policies.sh` gains a derived target type (`hipaa-enforced`). Its
  targets are fixed names today.
- **This is a departure from design 15**, which says per-client SCPs attach
  "at OU level". Proposed **D-030**, registered on approval.

#### Failure mode, and its backstop

A uat or prod account vested without the attachment. Three things close it:
- **Vesting blocks:** B-165 adds a check that refuses go-live of an enforced
  account without the policy attached and live content matching the repo.
- **The hourly attachment check (§4.3) pages.**
- **The daily sweep would still see non-eligible use.**

The control is preventive *and* detective so that one omission is not
silent.

**Member accounts cannot detach it.** `organizations:DetachPolicy` is already
denied by 03-v2, and SCPs do not apply to the management account, which is
where attachment happens.

**Timing:** Arc8 prod does not exist yet. The cheapest moment to attach is at
vest, before the first workload. B-165 makes vesting do it.

### 4.2 Detection: daily sweeps, not a per-call EventBridge rule

The brief suggests CloudTrail/EventBridge on calls to non-allowlisted
prefixes. **I recommend against a real-time rule, for three reasons:**

1. **Vocabulary.** An EventBridge `anything-but` filters on CloudTrail
   `eventSource` (`monitoring.amazonaws.com`, `es.amazonaws.com`, ...). That
   is a third vocabulary, different from IAM prefixes and from Config types,
   for ~200 services. Every mapping error would be a silent miss.
2. **Pattern size.** ~200 sources × ~25 chars ≈ 5 KB. I believe that exceeds
   EventBridge's event-pattern limit, but I am not certain of the limit (E9).
3. **No consumer needs real time.** Dev and test route to a *digest*. In uat
   and prod the SCP is the real-time control, and what needs detecting there
   is the SCP's own failure (§4.3), which is not a per-call question.

**Instead, two daily sweeps from the Audit account** (new component
`eligibility/`):

| Sweep | Source | Catches | Why this source |
|---|---|---|---|
| **Usage** | IAM service-last-accessed (`GenerateServiceLastAccessedDetails`) for every role in each HIPAA account, via `PlatformHipaaEligibilityReader` (see Deployment) | Any principal touching a non-allowlisted prefix: read, write, console spillover | Reports in **IAM namespaces**, the same vocabulary as the SCP, so no mapping table and no drift between them. Covers read-only calls, which EventBridge does not reliably receive. |
| **Inventory** | The organization Config aggregator in Audit (B-157 phase C), advanced query by account and resource type, with tags | Existing resources of non-eligible types, and **`DataClassification=regulated` on a non-eligible type** | Already deployed. Sees resources created before the sweep existed, which a call-based detector never would. |

**Known limits, stated:**
- Service-last-accessed lags up to ~4 hours.
- I believe it records attempts as well as successes, but this is unverified
  (E9).
- Config does not record every resource type. A non-eligible service with no
  Config type is caught by the usage sweep only.
- CloudTrail in Log Archive remains the drill-down for a human: who, which
  action, when. It is not the automated detector.

**Owner:** new `eligibility/` component. Not `instrumentation/`, whose job
(design 06) is "what must be monitored", per resource. This is a compliance
judgement across an account. Not `telemetry/`, which is about agent silence.

**Deployment:** `eligibility/10-hipaa-eligibility.yaml` in Audit, and the StackSet `platform-hipaa-eligibility-reader` (`eligibility/00-hipaa-eligibility-reader.yaml`) targeted at **HIPAA client OUs only** (`deploy-stackset.sh --ou client:<partner>/<client>`). *As built: the design said "no new member-side stack" and assumed an existing reader. None fits: the verification reader holds synthetics and backup reads for another purpose, and widening it is what PlatformTelemetryReader's header warns against. A hand-deployed role would be drift (B-143). So a minimal role of its own, by StackSet, scoped to HIPAA clients — never Members-wide.*

**Scope source:** `scripts/sync-hipaa-accounts.sh` publishes `/platform/eligibility/hipaa-accounts` in Audit: scope from the registry, `enforced` from the **live** SCP attachment, never from registry intent.

### 4.3 Attachment check, hourly

For each enforced account:
- Is the expected policy attached? It is read from the management account by
  the existing `org/30-verification-org-reader.yaml` role.
- Does its live content match the generator output for that account?
- Is every enforced-by-registry account attached?
- Is anything attached to an account the registry does *not* enforce? That
  case is also drift.

### 4.4 Severity and routing

The emitter assigns the *natural* severity. **The router decides
destination**, through a new normaliser (`from_hipaa_eligibility`) in
`alerting/src/router.py`, using the existing tier map. No path to PagerDuty
bypasses it (B-117, W-010).

| Detection | Emitter severity | dev | test | uat | prod |
|---|---|---|---|---|---|
| Non-allowlisted prefix used (any status other than eligible or carve-out) | medium | digest | digest + Jira | — † | — † |
| Same, in uat or prod, **succeeded** (no exception) | high | — | — | Jira + commercial | **PagerDuty**: the SCP has failed |
| Same, in uat or prod, **denied** (SCP held) | low | — | — | digest | digest |
| **`DataClassification=regulated` on a non-eligible resource type** | **critical** | Jira, immediate | Jira, immediate | Jira + commercial | **PagerDuty** |
| Attachment missing or drifted | high | — | — | PagerDuty (infra) | PagerDuty (infra) |
| Upstream list: removal or narrowing, or check blind | high | AltDigital Jira | | | |
| Exception expiring in ≤14 days, or expired | medium / high | AltDigital Jira; tenant contact | | | |

† Cannot succeed in uat/prod while the SCP holds. If it does, it is the
"succeeded" row.

**What `regulated` on a non-eligible type means differs by tier, and the
routing has to reflect that:**
- **In dev:** developers must tag every resource (SCP 04) and will
  truthfully tag it with the class of data it is *designed* to hold. So it
  usually means "this design will put PHI on a non-eligible service". That is
  a guaranteed prod blocker, worth a ticket on day one, and **not** a PHI
  incident. If Macie also finds PHI in it, design 11's scope-change procedure
  applies.
- **In prod:** possible PHI on a service the BAA does not cover. It pages. It
  is handed to **Art for breach assessment**. It does **not** start the
  notification clock automatically (`discovery_time` is a human
  determination; E7).

**Dev and test critical go to Jira immediately, not the next digest.** The
router routes the dev tier to digest by default, so this is a per-detection
routing rule that only *lowers* the latency of an already-ticketing class.
That needs Jamie's confirmation as a routing decision (E8).

**Noise control:**
- One open ticket per `(account, prefix)`, tracked in a small state table in
  Audit.
- Denied **read-only** calls in uat/prod are console spillover (a CloudWatch
  console calling `rum`). They are counted in the digest as one line per
  account, never itemised.

**No automated remediation.** No resource is stopped, deleted, re-tagged or
rolled back. The sweep reads. The router routes. Design 11's scope-upgrade
procedure is the human path for regulated data in the wrong place.

---

## 5. Exception path

**Use case:** a tenant needs a non-eligible service for a purpose that does
not touch PHI. Example: App Runner for a marketing page in prod.

### Record

The registry already reserves `ACCOUNT#<id> / OVERRIDE#<name>`, with its own
reason, owner and review date. Name: `OVERRIDE#hipaa-eligibility:<prefix>`.

| Field | Content |
|---|---|
| `prefix` | One IAM prefix. Never a wildcard. |
| `purpose` | What it does and why no eligible service fits |
| `non_phi_attestation` | Tenant statement that no PHI is stored, processed or transmitted by it |
| `requested_by` | Opaque id, tenant side |
| `attested_by` | Opaque id, the tenant's change approver (questionnaire 3.3) |
| `approved_by` | Opaque id, AltDigital. **Proposed: Art** as CISO, or his named delegate (E5). Must differ from `requested_by` and `attested_by` (B12 rule, entity-blind). |
| `granted_at` / `expires_at` | Default 90 days, maximum 180 (E5) |
| `ticket` | Jira reference |

### Mechanism

The shared policy cannot carry an account-specific allowance without naming
the account. Naming the account in the policy text would hardcode an account
id. So an account with ≥1 live exception gets a **generated variant**:
`PlatformHipaaEligibleOnly-<alias>`, whose `NotAction` is the base list plus
its excepted prefixes. That variant replaces the base on that account
(attach the variant before detaching the base).

The generator is the only thing that writes either policy, so the diff in the
PR *is* the security review, same as every other SCP here. This is the
strongest operational argument for account-level attachment (§4.1).

### What the exception does not do

- **It does not silence detection.** A `DataClassification=regulated`
  resource of the excepted type still raises critical. The attestation said
  no PHI.
- **Optional, decide at build:** the variant can also deny creation calls
  for the excepted prefix where `aws:RequestTag/DataClassification =
  regulated`. That makes the attestation partly self-enforcing, at the cost of
  bytes and condition-key support per service.

### Expiry: warns, never acts

- At 14 days: Jira ticket to AltDigital and a notice to the tenant contact.
- At expiry: a high-severity finding.

**The deployed variant is not regenerated automatically.** Re-imposing the
deny on a live prod workload calling that service would take a service down.
The hard rule says such automation stops and pages. A human renews the
exception, or retires the usage and then regenerates.

---

## 6. Evidence to Truveon

A new stream, **`hipaa-eligibility`**, in the existing envelope: one record
per submission, sequenced, `content_hash`, the same completeness controls.
Destination follows **D-029 by layer**:

| Record | Layer → destination | Content |
|---|---|---|
| `list-in-force` (on change, and a daily attestation) | infrastructure → `altdigital-platform` | `list_version`, file digest, counts by status |
| `list-reviewed` (quarterly, and on drift resolution) | infrastructure → `altdigital-platform` | Reviewer (opaque id), page date reviewed against, diff summary |
| `attachment-state` (hourly per enforced account) | infrastructure → `altdigital-platform` | Account id, expected policy, attached yes/no, content digest match |
| `detection` | **Application usage → the client's declared destination. Control failure (attachment, a succeeded call in u/p) → `altdigital-platform`** | Account id, tier, prefix, status, resource *type*, principal **role ARN with the session name removed**, first and last seen |
| `exception` (grant, renew, expire) | infrastructure, with a copy to the client destination | Every §5 field. Identities opaque. |

### Zero PHI, and zero personal data

- **Session names are stripped.** Identity Center session names are usernames.
- **Resource names and ARNs are not sent, only a SHA-256 of the ARN.**
  Developers name resources, and a name can carry anything. The digest
  resolves back through the Config aggregator in Audit.
- **No request parameters.**

This goes beyond what the ingestion contract strictly requires. I recommend it
because the alternative is trusting every contractor's naming.

**Partner chain.** This is AltDigital's control. OEight's HIPAA and SOC 2
scope relies on it by carve-out (design 15), so `altdigital-platform` is
where an auditor finds it. The client-destination copies are what let
OEight show its own client that its developers were told.

---

## 7. Developer experience

An SCP denial is a bare `AccessDenied ... explicit deny in a service control
policy`. Policy names do not appear. A policy ARN may (E9). And a member
account cannot call `organizations:DescribePolicy` to look one up.

So the teaching cannot depend on the error. It depends on **dev telling them
first**:

1. **The dev digest is the primary channel.** Message shape:
   > `apprunner` used in arc8-dev by role `arc8-builder` (first seen 2026-10-07).
   > **Not HIPAA-eligible — this will be denied in prod.** Use instead: ECS on
   > Fargate. Needed for something that never touches PHI? Request an exception
   > (developer guide §4.16).

   One line per `(account, prefix)`, until it stops being used.
2. **Developer guide §4.16** (new): what the control is, that dev and test
   warn and uat and prod deny, and the eligible-services list. That list is
   **generated from the TSV into the guide**, because contractors do not have
   repository access. Also: the alternatives table, the exception procedure,
   and "never put PHI in a support case".
3. **§4's "Reading an access-denied error" table gains a row:** an SCP deny
   in prod on a call that worked in dev is most likely this control; check
   §4.16.
4. **§4.15 is amended.** "Expensive services are allowed" stays true for dev
   only. In a HIPAA tenant's uat and prod, a service must also be eligible.
5. **Policy naming:** `PlatformHipaaEligibleOnly`. The description is
   `HIPAA eligible services only — developer guide 4.16`. It is visible to
   anyone who can see the policy, which is AltDigital. The guide is the
   tenant-facing equivalent.
6. **Optional, later:** a pre-flight check developers run in CI against
   their CloudFormation: resource types against `config_types`. This is the
   earliest possible warning, but it only covers IaC. Deferred until a
   developer asks or the digest shows repeated hits (trigger, not a date).

---

## 8. Proposed deviation (registered on approval)

**D-030 · Per-client compliance SCPs attach to accounts, not OUs.**
- Design 15 says per-client SCPs attach "at OU level".
- This control varies by tier *within* a client and can vary per account
  (exceptions), so its target is a registry-derived account set.
- Closes if: the OU tree gains tier-level OUs for some other reason, at which
  point (a′) costs nothing extra.

Not a deviation, but design 11's bypass table says cost- and scope-heavy
service enablement is "not denied". That remains true in dev. In a HIPAA
tenant's uat and prod, a *non-eligible* service is now denied. Note for
design 11 when it is next revised.

---

## 9. Open items

| # | Item | Blocks | Owner |
|---|---|---|---|
| **E1** | **Are `synthetics` and `application-signals` (and the other §2.1 "uncertain" prefixes) covered as CloudWatch features?** If Synthetics is not eligible, prod canaries must avoid PHI-rendering pages: a canary design change, not an exception. | B-160 attach | Jamie, with Art |
| E2 | ~~`amplify`~~ **Narrowed (as built):** `amplify` is the Hosting (formerly Console) API, so it is eligible. `amplifybackend` and `amplifyuibuilder` are separate prefixes, held `pending`. Remaining question: allowlist those two? | — | Jamie |
| E3 | Action lists for the §2.3 exclusions (voice messages, SageMaker workforce types, Ground Truth Plus) and Connect Health's prefix | B-159 | Build, verify against AWS docs |
| E4 | `support` as a carve-out depends on a "no PHI in support cases" rule that only the guide enforces. Accept? | B-159 | Art |
| E5 | Exception approver (Art or delegate), default and maximum expiry (90/180 proposed) | B-162 | Jamie, Art |
| E6 | Upstream drift resolution SLA (10 business days proposed) and quarterly reviewer | B-159 | Jamie |
| E7 | Prod `regulated`-on-non-eligible: page plus breach assessment by Art. Confirm it does not start the notification clock until a human determines discovery. | B-161 | Art |
| E8 | Dev/test critical go to Jira immediately, not the next digest: a per-detection routing rule in the router | B-161 | Jamie |
| E9 | Unverified AWS behaviour: EventBridge pattern size limit; whether service-last-accessed records denied attempts; whether SCP denials carry the policy ARN in error messages | B-161, B-164 | Build: verify in sandbox |
| E10 | Per-target SCP quota is 10 per the repository; not confirmable through the Service Quotas API. Arc8 accounts carry 1, so not blocking. | — | Build |
| E11 | Fetched list transcription was through a summariser: B-159 builds by hand from the page | B-159 | Build |
| E12 | SOC 2-only tenants: does a framework other than HIPAA need an eligibility profile? Not built. **Trigger:** first tenant with a framework that has its own service eligibility list (FedRAMP, or PCI once D-025 lifts). | — | Deferred |

### V-item dependencies

- **V1** (member admin cannot remove detective controls). Unchanged, and
  extended: 03-v2's `DetachPolicy` deny is what keeps this attachment in
  place. **Detaching or weakening 03 weakens this control too.**
- **Proposed V12:** "Non-eligible service calls are denied in enforced
  accounts, and eligible ones are not." Proven by `scripts/test-guardrails.sh`
  in the provable way:
  - a non-allowlisted call (e.g. `apprunner:ListServices`) denied, naming the SCP;
  - an eligible call permitted from the same principal (the differential probe);
  - a platform-only prefix permitted for a named platform principal and denied
    for the subject.
- **Registry correctness becomes load-bearing.** A wrong `compliance_scope` or
  `environment` puts the policy on the wrong set of accounts. The hourly check
  (§4.3) compares registry and Organizations, but both could be wrong
  together. The vesting check (B-165) is where the registry value is first
  asserted against the questionnaire.

### Build order recommended

Arc8 developers start now, in dev, and prod does not exist yet. That inverts
the obvious order:

1. **B-159** — the source file, by hand.
2. **B-161 + B-164** — the dev/test sweep and the digest wording. This
   delivers the "learn in dev" goal while Arc8 is in dev.
3. **B-160** — the policy, sandbox harness, Access Analyzer, cfn checks.
4. **B-165** — vesting attaches it when Arc8 prod is vested.
5. **B-162, B-163** — exceptions and evidence, before prod go-live.
