# Auditor Guide — Walking the AltDigital Managed Platform

For the person examining the platform as an auditor: in the first instance the
CISO, running an internal SOC 2 and HIPAA dress rehearsal, and later an
external assessor. It is a walkthrough. Each section states what the platform
claims, where that claim is implemented, how to check it yourself, what
evidence it leaves behind, and what is known to be missing.

**Read this first.** This guide is written to be argued with. Every gap the
build knows about is listed here, and listed where you will be looking for it,
not in an appendix. If you find something this guide does not mention, that
is a finding about the guide as well as about the platform.

Contents:

0. [Scope, and what this guide is not](#0-scope-and-what-this-guide-is-not)
1. [Your access](#1-your-access) — what `SecurityReadOnly` shows, what it
   cannot, and what is proposed
2. [The estate](#2-the-estate) — accounts, OUs and where things live
3. [Identity and privileged access](#3-identity-and-privileged-access)
4. [Segregation of duties and two-person control](#4-segregation-of-duties-and-two-person-control)
5. [Preventive guardrails](#5-preventive-guardrails)
6. [Audit logging](#6-audit-logging)
7. [Detection](#7-detection)
8. [Alerting and escalation](#8-alerting-and-escalation)
9. [Incident response and containment](#9-incident-response-and-containment)
10. [Change management](#10-change-management)
11. [Encryption and key management](#11-encryption-and-key-management)
12. [Vulnerability management and patching](#12-vulnerability-management-and-patching)
13. [Backup and recovery](#13-backup-and-recovery)
14. [Evidence integrity and retention](#14-evidence-integrity-and-retention)
15. [HIPAA-specific controls](#15-hipaa-specific-controls)
16. [Tenant isolation](#16-tenant-isolation)
17. [What this repository does not hold](#17-what-this-repository-does-not-hold)
18. [Gap register](#18-gap-register)

---

## 0. Scope, and what this guide is not

**In scope:** the AWS infrastructure layer that AltDigital builds and operates.
That is the AWS Organization, its accounts, the guardrails, identity, logging,
detection, alerting, change reconciliation, key management, backup and the
evidence pipeline. All of it is defined in this repository.

**Out of scope:** the tenant applications. Arc8 is the first. Under the
[responsibility matrix](../design/responsibility-matrix.md) the client owns
application IAM, application patching, change approval for its own code and
secret content. SOC 2 for infrastructure operations is intended to be offered
on a **carve-out** basis.

**There is no control-to-criteria mapping in the repository.** Evidence
records carry AltDigital's own `control_ref`, and mapping to frameworks is
left deliberately to the compliance platform, Truveon
([D-016](deviations.md), [evidence/src/record.py](../evidence/src/record.py)).
The values are `config-continuous-evaluation`, `security-finding-triage`,
`threat-detection`, `change-management-reconciliation`,
`quorum-two-party-control`, `security-incident-response`,
`backup-restore-verification`, `containment-exercise`,
`evidence-pipeline-liveness` and `evidence-pipeline-completeness`. A HIPAA
mapping document is requested in [phase-15-prompts](../design/phase-15-prompts.md)
and does not exist yet.

The **criteria** line at the head of each section below is AltDigital's
working crosswalk, written for this rehearsal. No auditor has validated it.
Treat it as a pointer to where to look, not as a claim of coverage.

**State as of 2026-10-08.** The newest estate snapshot
(`estate/estate.json`) was taken on **2026-09-22**, before the Arc8 accounts
existed. Ask the operator to regenerate it with `scripts/dump-estate.sh`
before you start, and work from the fresh one. Where this guide says "per
the snapshot", the live state may differ.

**Stale statements you will meet in the repository.** These are known to be
wrong:

- The build-state table in the [README](../README.md) is out of date.
- [policies/scp/README.md](../policies/scp/README.md) and comments in
  `attachments.tsv` say SCPs 04 to 07 are on Sandbox only. They also target
  Members.
- [verification-sweep.md](verification-sweep.md) says the registry and
  evidence sequence numbering do not exist. Both now do.

---

## 1. Your access

### 1.1 What you have: `SecurityReadOnly`

Defined in [identity/30-security-readonly.yaml](../identity/30-security-readonly.yaml).
It is assigned to a group in **every active account, including management**,
by [scripts/deploy-security-access.sh](../scripts/deploy-security-access.sh).
Sessions last 4 hours. Sign-in is through `https://altdigital.awsapps.com/start`
(CLI setup: [developer-guide.md §1](developer-guide.md#1-signing-in), with your
AltDigital identity).

It is the AWS managed policies `SecurityAudit` and `ViewOnlyAccess`, minus
four denies: identity and Organization mutation, disabling controls, reading
secret values and KMS decryption, and assuming roles. The denies matter most
in the management account, where SCPs do not apply.

**It answers "how is it configured?" well.** Checked against the current
policy versions (SecurityAudit v94, ViewOnlyAccess v46), it can read:

| Area | Can read |
|---|---|
| Organization | Accounts, OUs, every SCP and RCP, their targets, effective policies, delegated administrators |
| Identity Center | Permission sets, their policies, boundaries, account assignments, groups, users, group memberships |
| IAM | Roles, trust policies, credential report, account authorisation details, last-accessed data, policy simulation, OIDC providers |
| Detection | Security Hub findings and standards, GuardDuty findings and members, Config compliance and aggregator queries, Inspector findings and coverage, Macie finding *list*, Access Analyzer |
| Logging configuration | Trails, trail status, event selectors, log group settings, subscription and metric filters, Config recorder and delivery status, flow log and resolver settings |
| Storage configuration | Bucket policies, encryption, versioning, **bucket-level Object Lock**, replication, lifecycle, public access block |
| Keys | Key policies, rotation status, grants |
| Monitoring | Alarms, alarm history, metric data, EventBridge rules and targets, SNS subscriptions, SQS queue attributes |
| Backup | Vaults, vault lock, plans, recovery points, backup and restore jobs |
| Deployment | CloudFormation stacks, templates, events, StackSets and instances |
| Activity | `cloudtrail:LookupEvents`: management events, 90 days, per account and region |

### 1.2 What it cannot show you

**No. `SecurityReadOnly` is not enough for an audit.** It shows how each
control is configured. It cannot show the control's output, and the output is
most of what an audit samples:

| You will want to | Blocked because | Where it matters |
|---|---|---|
| Read the org CloudTrail beyond 90 days, or query it | No `s3:GetObject`, `kms:Decrypt` is denied, and no Athena access. CloudTrail is not delivered to CloudWatch Logs (B-005). | §6. The trail is retained for 6 years and unreadable to this role past day 90. §1.3 closes this. |
| Prove an object is under Object Lock | No `s3:GetObjectRetention` or `s3:GetObjectLegalHold`. Bucket defaults are visible; per-object retention is not. | §14 |
| Read any log *content* | No `logs:GetLogEvents`, `FilterLogEvents` or Insights queries | Router decisions, containment actions, reconciliation runs, digest sends |
| Read the evidence ledger, change events, quorum records, incident records | No DynamoDB `GetItem`, `Query` or `Scan` | §4, §9, §10, §14. These tables *are* the evidence. |
| Follow a Step Functions execution | No `states:DescribeExecution` or `GetExecutionHistory` | Containment, vesting, quorum execution |
| Read published platform configuration | No `ssm:GetParameter`, so `/platform/*` is invisible. All of it is plain String; none of it is secret. | Approver lists, tier map, notification windows |
| Read a Macie finding in full | `ListFindings` only, not `GetFindings` | §15 |
| Read patch baselines and SSM documents | No `ssm:GetPatchBaseline` or `GetDocument` | §12. Moot today, as nothing patches. |
| Read the digest send records | No `s3:GetObject` on the evidence bucket | §8 |

Systems outside AWS are not covered by any AWS role:

| System | Holds | You need |
|---|---|---|
| Jira (AltDigital ops project) | Platform change records, quorum approvals | Read on the change and quorum projects. You are already a quorum approver, so check what you can browse. |
| PagerDuty | Incident history, escalation policies, schedules, acknowledgement times | A read-only user (PagerDuty's *Observer* or *Restricted Access* role) |
| GitHub (`AltDigitalOfficial/AltDigMSPMgmt`) | This repository: the platform's own change history | Read access |
| Truveon | Evidence as received, framework mapping | A reviewer seat on AltDigital's tenant |
| Mailbox | Digests, AWS security notices | Already received where you are the security contact |

### 1.3 Your evidence access: `AuditEvidenceReader`

**Built 2026-10-08, not yet deployed or proved.** Defined in
[identity/40-audit-evidence-reader.yaml](../identity/40-audit-evidence-reader.yaml),
with the trail query layer in
[security/50-trail-query.yaml](../security/50-trail-query.yaml). Both are
deployed by [scripts/deploy-audit-access.sh](../scripts/deploy-audit-access.sh).
Before an external assessor is given it, every allow must be shown to succeed
against a real object and every deny to fail against a real resource, as was
done for `SecurityReadOnly`.

**Why it is separate.** `SecurityReadOnly`'s membership is the security
function *plus every quorum approver*. The approvers are there so they can
see what they are approving. Widening it would hand log contents, which carry
personal data such as principal names and source IPs, to people who need
none of it. An auditor's grant also has to be given for an engagement and
taken away afterwards, without touching a standing role.

**Assignment.**

- A dedicated `Auditors` group, assigned to **every active account**, so you
  can walk a member account's configuration.
- Every grant beyond the managed policies is scoped to a resource the
  platform named, so assigning it to a tenant account gives no reach into
  tenant data.
- It is granted **for an engagement**. The script refuses to grant without an
  end date (`AUDIT_ENGAGEMENT_ENDS`) or after it.
- Access is taken away with `--end-engagement`. Nothing revokes it
  automatically when the date passes. That check is not built.

**What it adds to `SecurityReadOnly`'s view:**

| Adds | Scope |
|---|---|
| Object read, version read, **per-object retention and legal hold** | `altdig-log-archive-*`, `altdig-config-archive-*`, `altdig-flow-logs-*`, replicas included. The digest send records are under `digest-evidence/` in the log archive. |
| `kms:Decrypt` | Only keys aliased `platform-log-archive`, `platform-archive-replica` or `platform-audit-query`, and only when S3 makes the call |
| **Athena over the org trail** | Workgroup `platform-audit`, table `platform_audit.cloudtrail`, in Log Archive. Six saved queries are described below. |
| Log contents and Logs Insights | `/aws/lambda/platform-*`, `/aws/platform/*`, `/aws/vendedlogs/states/platform-*` |
| DynamoDB item reads | Evidence ledger and sequence, change events, quorum evidence, security incidents, HIPAA eligibility state, remediation history. **Not the registry**, which holds client contact details and is explicitly denied. |
| Step Functions execution history | `platform-*` state machines: containment, quorum, restore, exercises |
| Parameter values | `/platform/*` |
| Detail | `macie2:GetFindings`, patch baselines, SSM documents |

**It keeps every denial `SecurityReadOnly` has:**

- no mutation of identity or the Organization;
- no disabling of controls;
- no secret values;
- no role assumption.

It adds three more:

- no `macie2:GetSensitiveDataOccurrences`, which would show the actual
  sensitive values Macie matched;
- no deleting or re-locking objects;
- no direct use of any KMS key. Decryption only ever happens through S3 or
  DynamoDB, and only with the keys listed above.

**Saved queries** in the `platform-audit` workgroup. Each looks back 90 days;
change the `dt` filter to widen it.

| Query | Answers |
|---|---|
| `root-user-activity` | Every call made as an account root user (§3) |
| `organization-account-access-role-use` | Who assumed the SCP-exempt operator role, where and when (§3, §4) |
| `identity-center-authentication` | Sign-in events with the credential type used. This is evidence for or against MFA (§3). |
| `access-grants-and-revocations` | Permission set, assignment, membership and user changes: the joiner, mover and leaver trail (§3) |
| `control-tampering-attempts` | Attempts to stop, delete or weaken a control, allowed or denied, and by whom (§4, §5) |
| `guardrail-denials-by-account` | SCP and RCP refusals, which show the guardrails operating and not just configured (§5) |

Athena bills per byte scanned, and the workgroup cancels any query over
50 GiB. Always filter on `dt`, and on `account` where you can.

**Independence.** In this rehearsal the auditor is also a quorum approver and
an infrastructure change approver, so you will be examining controls you
operate. That is acceptable for a rehearsal, and should be noted in its
report. An external assessor will raise it if the same person signs the
management assertion.

### 1.4 Things to ask the operator to run while you watch

Several controls can only be *exercised* with write access. The platform's
own rule is "every check must exercise the thing, not assert that
configuration exists"
([verification-sweep.md](verification-sweep.md)). Observing these is
stronger evidence than reading configuration:

| Script | Proves |
|---|---|
| `scripts/test-guardrails.sh --account <id>` | Each SCP denial holds against a real attempt |
| `scripts/test-alert-path.sh` | A synthetic alarm reaches PagerDuty and pages the rotation |
| `scripts/verify-security-coverage.sh` | GuardDuty, Security Hub, Macie and Inspector membership equals the account count, in every allowed region. Config is **not** checked. |
| `scripts/test-hipaa-scp.sh` | The HIPAA eligible-services SCP denies and allows as claimed (Sandbox) |
| `scripts/test-deployer-conditions.sh` | The tenant deploy role's conditions |
| `scripts/dump-estate.sh` | A point-in-time snapshot of the whole estate, for sampling |

---

## 2. The estate

The source of truth is AWS Organizations, so list it yourself:
`aws organizations list-accounts` in the management account. Account names
follow `altdig-<partner>-<client>[-<app>]-<env>` ([D-023](deviations.md)).

| OU | Account | Role |
|---|---|---|
| (root) | Management `738815759702` | Holds the Organization, SCPs, Identity Center (home `us-east-2`), the org trail. Nothing else. **SCPs do not apply here.** |
| Security | Log Archive | Org CloudTrail, Config archive and flow log buckets, plus `us-west-2` replicas |
| Security | Audit | Delegated administrator for GuardDuty, Security Hub, Config, Inspector, Macie, Access Analyzer, Backup and Trusted Advisor. Also hosts the alert router, evidence pipeline, change reconciliation, quorum and containment. |
| Security | Security Forensics | Isolated evidence copies from containment |
| Security | Security Backup | Locked backup copy vault |
| Infrastructure | Infra Tooling | StackSet administrator, platform registry, artifacts, daily digest |
| Sandbox | Sandbox Canary, Sandbox Exercise | Heartbeat source, guardrail and containment exercises |
| Members / `oeight` / client | Arc8 dev, test | First tenant. No UAT. **Prod does not exist yet** (Organization account quota, B-140). |

Layout code is in [org/](../org/). OU ids are published under
`/platform/org/ou/*` in SSM and never hard-coded.

**Registry.** DynamoDB `platform-registry` in Infra Tooling, defined in
[registry/10-registry-table.yaml](../registry/10-registry-table.yaml) and
encrypted with a customer-managed key. It holds a record per partner, client,
account and override. Account records carry `environment`,
`service_model` and `compliance_scope`, which drive which controls apply.
Client records hold contact details, so do not export rows into working
papers.

---

## 3. Identity and privileged access

**Criteria:** CC6.1, CC6.2, CC6.3, CC6.6 · HIPAA 164.308(a)(3), (a)(4),
(a)(5)(ii)(C); 164.312(a)(1), (a)(2)(i)–(iii), (d)

**Claim.** Every human reaches AWS through IAM Identity Center with
short-lived credentials. There are no IAM users or access keys for people in
member accounts. Every permission is granted to a **group**, never to a user.
Each permission set is least-privilege for one job.

**Permission sets** (all in [identity/](../identity/), all group-assigned):

| Set | Accounts | Session | Purpose |
|---|---|---|---|
| `SecurityReadOnly` | All, including management | 4h | Security function and quorum approvers (§1) |
| `CommercialReadOnly` | Management only ([D-009](deviations.md)) | 4h | Billing read. A `NotAction` deny blocks everything outside billing. |
| `DeveloperDev` | Tenant dev | 4h | PowerUser, plus IAM role creation with a mandatory boundary |
| `DeveloperTest` | Tenant test | 4h | Read, plus restart/run task/invoke. No data-plane reads. |
| `DeveloperProd` | Tenant uat, prod | 2h | Observe only. No data plane, no task definitions (they expose env values, [D-017](deviations.md)). |
| `TenantReadOnly` | Every tenant account | 2h | Oversight. Fail-closed `NotAction` deny. |
| `SecretsCustodian` | Tenant test, uat, prod | 1h | Write secret values, never read them back, never delete |

`cfn-guard` refuses any templated permission set containing
`AdministratorAccess` or `Action: *`, or a session other than 1, 2 or 4
hours ([policies/guard/baseline.guard](../policies/guard/baseline.guard)).

**How AltDigital administers the platform.** Be clear about this one,
because it is the weakest part of the identity model:

- **`PlatformBootstrapAdmin`**: `AdministratorAccess` on the management
  account, 1-hour sessions, assigned to one person. It was created by hand in
  the console and is not in code. This is a standing admin grant with no
  just-in-time elevation ([D-007](deviations.md)).
- From management, operators reach member accounts by assuming
  **`OrganizationAccountAccessRole`** (OAAR). About fifty script call sites do
  this. OAAR is exempt from SCPs 01 to 07 so it can repair what they protect,
  and it is protected from modification by name.
- The design's tiered operator roles (PlatformReadOnly, PlatformOperator,
  just-in-time PlatformAdmin and SecurityResponder) **do not exist**. Neither
  does narrowing OAAR's trust. Both are planned for phase 15.5.

**Root.**

- **Management root:** generated password, no access keys, a **virtual**
  TOTP device. Hardware MFA is outstanding ([D-006](deviations.md)). The root
  mailbox is an alias onto one person's mailbox, not a monitored distribution
  list ([D-001](deviations.md)).
- **Member roots:** every action denied by SCP 03
  (`DenyMemberAccountRootUser`). Centralised root access is not enabled
  (planned for 15.5a).

**Break-glass: not built.** The design calls for directory-independent
accounts with hardware MFA and split custody, paging on every use and tested
on a schedule. The decision is two management-account IAM users with
passkeys and split custody (F-3). None of it exists, and neither does
detection of root or OAAR use. HIPAA's *emergency access procedure*
(164.312(a)(2)(ii)) is a required implementation specification, and today
the practical answer is root or `PlatformBootstrapAdmin`.

**MFA for Identity Center: not enforced in code, and not verified.** The
directory is Identity Center's built-in directory, not Entra
([D-010](deviations.md)). Identity Center has a setting to require MFA on
every sign-in and restrict the allowed methods. Whether it is configured has
not been checked ([phase-15-plan](phase-15-plan.md) §15.5b). Check it in the
console under *Settings → Authentication*. If `SecurityReadOnly` cannot open
that page, ask the operator for a screenshot with the URL and time visible.

**Joiners, movers and leavers.**

- Membership is configuration. Platform groups are listed in
  `config/identity.env` (gitignored, because it holds names). Tenant
  developers come from a roster supplied through the onboarding
  questionnaire ([vesting/roster.schema.json](../vesting/roster.schema.json)).
- The scripts **add and never remove**. A person in a group but absent from
  configuration is reported and left alone. The intent is that removal is a
  deliberate, recorded act, not a side effect of a deployment.
- **There is no periodic access review.** No recertification process or
  script exists, and the design defers review evidence to Truveon. Expect
  this to be an exception.
- `console_access: false` on a roster entry is accepted and enforces nothing
  (B-131).

**Machine identity: the tenant deploy role.** `TenantAppDeployer-<app>` is
assumed from GitHub Actions over OIDC, with a 1-hour session and a
permissions boundary ([change/00-appdeployer.yaml](../change/00-appdeployer.yaml)).
Trust pins `aud` exactly and `sub` by pattern. The template's minimum length
does not refuse a wildcard subject, and nothing else validates it. Pinning to
the exact claim is open (B-142).

**How to check:**

```bash
# Management account, SecurityReadOnly
aws sso-admin list-instances
aws sso-admin list-permission-sets --instance-arn <arn>
aws sso-admin list-account-assignments --instance-arn <arn> \
  --account-id <id> --permission-set-arn <ps-arn>
aws identitystore list-group-memberships --identity-store-id <id> --group-id <gid>
aws iam generate-credential-report; aws iam get-credential-report   # per account
aws iam list-users                                                 # expect none for humans in members
```

Sample one tenant developer end to end, from roster entry to group to
assignment to CloudTrail sign-in events. Sample one leaver, if there has been
one, and ask for the record of the removal.

**Evidence:** Identity Center configuration (live); CloudTrail
`CreateAccountAssignment`, `DeleteAccountAssignment` and sign-in events; IAM
credential reports.

**Gaps:** D-001, D-006, D-007, D-010; break-glass; Identity Center MFA
unverified; no access review; B-131; B-142; no OAAR or root-use detection.

---

## 4. Segregation of duties and two-person control

**Criteria:** CC6.3, CC8.1, CC5.2 · HIPAA 164.308(a)(3)(ii)(A), (a)(4)

**Claim.** Destructive actions against controls need two approvers, neither of
whom is the requester. Approval happens in Jira and is checked by machine.
The action is then executed by a dedicated role that can do nothing else.

**What needs quorum** ([quorum/src/verify.py](../quorum/src/verify.py)):

- KMS key deletion
- backup vault deletion and vault lock changes
- removing an account from the Organization
- stopping or deleting CloudTrail
- stopping or deleting the Config recorder

**How it works.**

1. An approved change record in Jira names the action and the resource.
2. The quorum pipeline in Audit ([quorum/10-quorum.yaml](../quorum/10-quorum.yaml))
   checks five conditions:
   1. each approver is authorised (the list is published at
      `/platform/quorum/approvers`, not taken from Jira group membership,
      [D-022](deviations.md));
   2. no approver is the requester;
   3. the approvals postdate the request;
   4. there was no edit or reopen after approval;
   5. the declared `quorum-action` and `quorum-resource` match.

   It also requires at least two approvals.
3. It returns one of three verdicts: *approved*, *refused* or *undeterminable*.
4. Execution goes through `PlatformQuorumExecutor`
   ([quorum/00-executor-role.yaml](../quorum/00-executor-role.yaml)): 1-hour
   sessions, session name `quorum-*`.
5. KMS key policies deny the destructive actions to everyone except that role.
6. A reverse sweep of CloudTrail looks for destructive actions that did *not*
   come through quorum.

**Where it is weaker than it looks.**

- **OAAR and `PlatformDeployer` are exempt from the SCP denies** that make
  quorum the only path. An operator holding OAAR can perform a quorum action
  directly, and only the reverse sweep would catch it. That makes this
  control detective for administrators, not preventive.
- **B-070:** the Jira credential the platform files records with is a
  personal account. Condition 2 therefore fails for every record the platform
  files itself.
- Approver membership: [design/open-items.md](../design/open-items.md) item
  D6 contradicts D-022.

**Infrastructure change approval** has its own approver list
(`config/change-approvers.env`): three approvers, two required, the builder
excluded (§10).

**Entity separation.** AltDigital partners also act for OEight under a
consulting agreement. Separation between the two is entity-blind and
blocking ([partners/src/separation.py](../partners/src/separation.py)), and
identity follows the role: OEight identity for tenant roles, AltDigital
identity for platform roles.

**How to check:** pull a quorum record from the quorum-evidence table (needs
§1.3) and trace it to the Jira record, the executor session in CloudTrail and
the reverse sweep result. Then, from CloudTrail, list destructive actions by
any principal other than `PlatformQuorumExecutor` over the period.

**Gaps:** OAAR bypass, which is detected but not prevented; B-070; D6
against D-022.

---

## 5. Preventive guardrails

**Criteria:** CC6.1, CC6.6, CC6.8, CC5.2 · HIPAA 164.308(a)(4), 164.312(a)(1)

**Claim.** Organization policies deny, for every principal in a member account
including administrators, the actions that would defeat a control. Every
role a developer creates carries a permissions boundary, so nothing built can
exceed its builder.

**SCPs and RCP** ([policies/scp/](../policies/scp/); attachment topology in
`attachments.tsv`):

| Policy | Purpose | Attached to |
|---|---|---|
| 00 deny leave/close | No leaving the Organization, no closing accounts | Root |
| 01 v2 | Protect CloudTrail, Config, GuardDuty, Security Hub, flow/DNS logging, platform log groups | Sandbox, Members, Security, Infrastructure |
| 02 v2 | Protect KMS keys, backup vaults, snapshots, evidence buckets, account public access block | Same |
| 03 v2 | Protect platform roles and tags, prevent role-name squatting, deny member root | Same |
| 04 require tags | `Owner`, `Application`, `DataClassification` (four values), `platform-layer` on create | Sandbox, Members |
| 05 require boundary | Every created role carries `PlatformDeveloperBoundary` | Sandbox, Members |
| 06 regions | `us-east-1`, `us-east-2`, `us-west-2` only | Sandbox, Members |
| 07 deny bypass | Network bypass, snapshot/AMI sharing, unapproved OIDC/SAML providers | Sandbox, Members |
| 08 protect monitoring | Platform alarms, rules, functions, parameters, stacks | Sandbox, Members |
| 09 HIPAA eligible only | HIPAA-eligible services only | **Nothing yet** (§15) |
| RCP 00 | No `sts:AssumeRole` from outside the Organization | Sandbox, Members |

Notes:

- SCPs never apply to the management account. This is an AWS property. The
  compensations are the heavy denies on the two read-only permission sets
  and keeping the account empty of workloads.
- SCPs 04 to 08 are not on the Security or Infrastructure OUs.
- The v1 originals of 01 to 03 are detached and kept for rollback.

**Permissions boundary.** `PlatformDeveloperBoundary`
([baseline/80-developer-boundary.yaml](../baseline/80-developer-boundary.yaml))
allows everything except the platform denials. Bounded roles can only create
bounded roles, and the boundary cannot be removed. B-143 records it reaching
Sandbox first, so **confirm it is present in every Members account**.

**How to check:**

```bash
aws organizations list-policies --filter SERVICE_CONTROL_POLICY
aws organizations list-targets-for-policy --policy-id <id>
aws organizations describe-effective-policy --policy-type SERVICE_CONTROL_POLICY --target-id <acct>   # where supported
aws iam get-policy --policy-arn arn:aws:iam::<acct>:policy/PlatformDeveloperBoundary   # per member account
```

Compare live attachment against `attachments.tsv`. Then ask the operator to
run `scripts/test-guardrails.sh` against a tenant account while you watch,
and check the denials in CloudTrail.

**Evidence:** the policy documents (live and in git), the guardrail harness
output, and `AccessDenied` events in CloudTrail.

**Gaps:** management-account coverage, which cannot be closed; boundary
reach (B-143); B-135 and B-136 history is worth reading for how exemptions
were tightened.

---

## 6. Audit logging

**Criteria:** CC7.2, CC4.1 · HIPAA 164.312(b), 164.308(a)(1)(ii)(D),
164.316(b)(2)(i)

**Claim.** Every API call in every account and allowed region is recorded
once, to an account nobody works in, in a form that cannot be altered or
deleted for six years.

**CloudTrail.** `platform-organization-trail`, defined in the management
account ([org/30-organization-trail.yaml](../org/30-organization-trail.yaml)):

- an organization trail, multi-region, with global events;
- **log file validation on**;
- encrypted with `alias/platform-log-archive`.

It delivers to `altdig-log-archive-<acct>` in Log Archive
([security/10-log-archive.yaml](../security/10-log-archive.yaml)):

- **Object Lock COMPLIANCE, 2190 days** ([D-024](deviations.md));
- moves to Glacier Instant Retrieval at 90 days;
- the bucket policy denies insecure transport, wrong encryption and deletion
  of versions;
- replicated to `us-west-2` under the same lock.

This trail records management events only, and has **no CloudWatch Logs
delivery** (B-005). The Athena workgroup in §1.3 is how the archive is
searched.

**S3 object access** is recorded by a second organization trail,
`platform-s3-data-events`
([org/31-s3-data-events-trail.yaml](../org/31-s3-data-events-trail.yaml),
[ADR-013](adr/ADR-013-s3-object-access-logged-in-every-account-by-a-second-organization-trail.md)),
live since 2026-10-27:

- every S3 object read and write in every account and region;
- delivered to the same locked bucket under `data-events/`, so it carries the
  same six-year Object Lock;
- **two exclusions**, both stated in the template:
  - writes to the log archive and its replica, which would otherwise log the
    trail's own deliveries in a loop;
  - S3 replication's reads of them. Every other read of the archive is
    recorded, which is how "who read the audit logs" is answered.

No Athena table reads `data-events/` yet, so these records are retained but
not yet queryable through §1.3.

**AWS Config.** The recorder records all supported resource types, with
global types in the home region only. It delivers to
`altdig-config-archive-<acct>`. That bucket cannot carry Object Lock, because
Config cannot write to a locked bucket, so immutability comes from the
locked replica. An organization aggregator in Audit covers all three
regions ([security/40-config-aggregator.yaml](../security/40-config-aggregator.yaml)).

**Recorder coverage: every account, every allowed region**, continuous
recording in every tier. Until 2026-10-25 recorders existed only in the two
Sandbox accounts in `us-east-2`. Phase 15.6a put one in all 30
account-regions, configured from a single home-region stack per account as
Config's service-linked role
([baseline/10-config-recorder.yaml](../baseline/10-config-recorder.yaml),
[ADR-010](adr/ADR-010-config-records-every-region-from-one-home-region-stack.md)).
`verify-security-coverage.sh` (§7) now fails on any account-region that is
not recording, not merely missing a recorder. Check it yourself:

```bash
aws configservice describe-configuration-recorder-status --region <each allowed region>
```

Config feeds Security Hub's checks, the conformance packs (§7) and the
discovery that creates alarms (§8). A recorder that stops is therefore a
blind spot in all three, which is why coverage is checked rather than
assumed.

**VPC flow logs: not collected.** The network baseline ships with
`EnableFlowLogs=false` because delivery to S3 fails (B-012, unresolved). The
destination bucket exists and is empty.

**DNS query logs: not collected.** A DNS Firewall exists for the
`dns-filtered` egress profile. Resolver query logging is configured nowhere.

**CloudWatch Logs.**

| Retention | What |
|---|---|
| 2557 days | Evidence, containment, change, quorum and eligibility logs |
| 365 days | Router, data-protection findings, instrumentation exceptions |
| 90 days | Digest, response |
| 30 days | Instrumentation Lambda, canary |

The design's 90-days-hot-then-Deep-Archive tiering is not built.

**Log data protection.** The account-level `PlatformDataProtection` policy
audits and masks twelve identifier types, including card numbers, SSNs, bank
accounts, HICN, MBI and NPI
([baseline/20-log-data-protection.yaml](../baseline/20-log-data-protection.yaml)).
Findings raise `platform-sensitive-data-in-logs`. AWS's own canonical test
values are not detected, so it has not been proven against a live sample
(B-009).

**How to check:**

```bash
aws cloudtrail get-trail-status --name platform-organization-trail          # management
aws cloudtrail get-event-selectors --trail-name platform-organization-trail
aws s3api get-object-lock-configuration --bucket altdig-log-archive-<acct>  # Log Archive
aws s3api get-bucket-replication --bucket altdig-log-archive-<acct>
aws cloudtrail validate-logs --trail-arn <arn> --start-time <t>             # needs §1.3 access
aws cloudtrail get-trail-status --name platform-s3-data-events              # management
aws cloudtrail get-event-selectors --trail-name platform-s3-data-events     # the two exclusions
```

`validate-logs` is the integrity test that matters: it proves the digest
chain is unbroken. It needs object read, and therefore needs §1.3.

**Gaps:**
- B-012 (flow logs) and DNS query logs;
- B-005, and data events not yet queryable in Athena;
- B-175: the management account forwards no events, so stopping or narrowing
  either organization trail pages nobody and is visible only afterwards,
  through the `control-tampering-attempts` query;
- B-009; B-158 (§14).

---

## 7. Detection

**Criteria:** CC7.1, CC7.2 · HIPAA 164.308(a)(1)(ii)(D), (a)(5)(ii)(B)

**Claim.** Threat detection, posture checks, vulnerability scanning, data
discovery and external-access analysis run in every account and allowed
region, administered centrally from Audit.

| Service | State |
|---|---|
| GuardDuty | Org-wide via auto-enable, 15-minute publishing. **S3 Protection and Lambda Protection on in every account** ([ADR-014](adr/ADR-014-guardduty-s3-and-lambda-protection-everywhere-other-plans-per-tenant.md)). EKS, ECS and EC2 runtime, Malware Protection and RDS plans are per tenant, where the workload exists. |
| Security Hub | **Central configuration from Audit**: one policy at the organization root ([ADR-011](adr/ADR-011-security-hub-central-configuration-disable-only-what-no-account-can-satisfy.md)). Standards are FSBP and NIST 800-53 r5 ([D-032](deviations.md)). A finding aggregator in `us-east-2` links all three regions (B-013). Eight controls are disabled because no account can satisfy them; four automation rules suppress platform-caused findings per resource, each with its reason in the finding's note. B-174 lists the real findings that remain. |
| Inspector | EC2 and ECR. Lambda scanning per tenant (ADR-014). |
| Macie | Inventory, plus **automated sensitive data discovery in every account** ([ADR-017](adr/ADR-017-cardholder-tripwire-macie-discovery-on-cost-watched-not-capped.md)). It had in fact been on since 2026-09-17, by Macie's default, while the repository said it was off. Card identifiers are included. A card-data finding in any tier becomes a scope-change ticket and an immediate client conversation. |
| Access Analyzer | Delegated to Audit, but **no analyzer is created anywhere** (B-058) |
| Config rules | Security Hub's service-linked rules; two platform rules, `platform-secret-max-age` (90 days) and `platform-secret-uses-cmk`; and the **platform conformance packs** ([ADR-012](adr/ADR-012-platform-conformance-pack-in-every-account-not-the-aws-hipaa-pack.md)): nine HIPAA-mapped rules Security Hub does not run, in every account and region, and a check that the S3 data events trail is logging. |

Design 04 asks for CIS and PCI standards and framework-driven conformance
packs. The platform runs FSBP and NIST 800-53 r5 instead (D-032), and one
platform pack everywhere in place of AWS's HIPAA sample pack, most of whose
rules Security Hub already runs (ADR-012).
[hipaa-control-mapping.md](hipaa-control-mapping.md) maps each HIPAA
safeguard to the controls that evidence it, and shows where nothing does.

**Coverage is gated.** [scripts/verify-security-coverage.sh](../scripts/verify-security-coverage.sh)
asserts that members equal the active accounts minus one, per service and per
region. It runs after every account creation. Since 15.6 it also fails on
any account-region whose Config recorder is not recording, on Security Hub
not being centrally configured in a region, and on the S3 data events trail
not logging or carrying any exclusion beyond the log archive. It was written
after delegation, enablement and enrolment were found to look
identical in the console while the Audit account had zero members (B-008).

**How to check** (Audit account):

```bash
aws guardduty list-detectors; aws guardduty list-members --detector-id <id>
aws securityhub get-enabled-standards     # expect: FSBP and NIST 800-53 r5
aws securityhub describe-organization-configuration   # expect: CENTRAL
aws securityhub list-automation-rules     # the suppressions, each with its reason
aws securityhub get-findings --filters '{"SeverityLabel":[{"Value":"CRITICAL","Comparison":"EQUALS"}]}'
aws inspector2 batch-get-account-status --account-ids <ids>
aws configservice describe-organization-conformance-pack-statuses
aws macie2 get-automated-discovery-configuration      # expect: ENABLED, ALL
aws accessanalyzer list-analyzers          # expect: none
```

Sample five HIGH or CRITICAL findings from the period and trace each into the
router and onward (§8). That is the test of whether detection connects to
anyone.

**Gaps:**
- B-013 (single-region aggregation), B-058, B-174;
- Macie discovery samples rather than scanning everything, so it is a
  tripwire, not a guarantee;
- its cost is checked by hand until 15.9 (ADR-017);
- sensitive data other than card data is found but not routed.

---

## 8. Alerting and escalation

**Criteria:** CC7.3, CC7.4, CC2.2 · HIPAA 164.308(a)(1)(ii)(D), (a)(6)(ii)

**Claim.** Every alarm and finding passes through one router, which alone
decides who is told and how urgently. Nothing pages except through it. The
router and its inputs are watched, so silence is itself detected.

**Where alarms come from.** [design/06a-alarm-specification.yaml](../design/06a-alarm-specification.yaml)
is the authoritative alarm specification. It is consumed directly as data,
not transcribed. Its status is `DRAFT`, and thresholds marked `[TUNE]` are
not final (open item B16).

- Config `ResourceDiscovered` triggers `platform-instrumentation`, which
  creates alarms for 15 resource types. Each alarm description carries a
  routing trailer.
- Anything the instrumentation cannot handle becomes an exception record.
- An hourly drift sweep, plus a trigger on `DeleteAlarms`, recreates alarms
  that are removed.

Note that this whole chain depends on Config (§6 recorder gap).

**The router.** [alerting/src/router.py](../alerting/src/router.py), in Audit.

| Tier | Route |
|---|---|
| dev | Digest |
| test | Digest and Jira |
| uat | Jira and commercial |
| prod | PagerDuty |

- An unknown account is treated as prod and pages.
- Findings page only at HIGH or CRITICAL.
- An untagged alarm goes to a review queue.
- `platform-layer` decides which team: `infrastructure` → AltDigital,
  `application` → the tenant ([CLAUDE.md](../CLAUDE.md) hard rule).

**The emitter rule.** An emitter may downgrade its own event and may never
escalate it. The router enforces this with a rank ordering and refused
paths ([D-028](deviations.md)). It exists because meta-alarms once bypassed
tier routing and dev drift paged people (B-117).

**PagerDuty.** Defined in Terraform in [pagerduty/](../pagerduty/) (the only
Terraform in the platform):

- a platform service at constant high urgency;
- a low-urgency dev/test service;
- a per-tenant module with a service per tier ([D-018](deviations.md));
- escalation after 15 minutes, two loops.

Terraform state is local and holds the routing key (B-016), and the drift
check is not scheduled (B-120).

**Watching the watchers.**

| Watches | Alarm or mechanism |
|---|---|
| Router health | `platform-router-errors`, `-dlq-not-empty`, `-throttled` |
| End-to-end liveness | 15-minute heartbeat from Sandbox Canary, `platform-router-heartbeat-missing`. Proven to fire about 67 minutes after a break. |
| Scheduled sweeps | Cadence sweep, `platform-cadence-sweep-stalled` (treats missing data as breaching) |
| Instance telemetry | Gap scan every 15 minutes with 30-minute grace |
| Canaries | `-failed`, `-slow`. A canary that never runs is silent (B-150). |
| Jira path | `platform-jira-intents-backlog`, `platform-jira-token-expiring` |

**Accepted residual risk:** if the router and its watchdogs fail together,
nobody is paged. `platform-alert-delivery-failed` has no independent path
(B-021).

**The daily digest.** `platform-digest` runs in Infra Tooling at 13:00 UTC
daily ([reporting/](../reporting/)).

- Recipient: the client's development manager, with AltDigital copied. The
  copy currently goes to an individual, not a shared mailbox (B-155).
- Contents: alarms new or changed, alarms in ALARM, instrumentation
  exceptions. **Not yet included:** telemetry gaps, guardrail denials, cost.
- A send record is written to an Object Lock bucket.
- Bounces, complaints and rejects raise `platform-digest-bounced`.
- `platform-digest-not-delivered` covers failed and skipped runs, but not a
  schedule that never fires.

**How to check:** ask the operator to run `scripts/test-alert-path.sh`. You
watch the page arrive, then find the incident in PagerDuty and the router
decision in its log. Then take the five findings from §7 and, for each,
establish the routing decision, the destination and, for anything that
paged, the acknowledgement time.

**Gaps:** 06a draft thresholds; B-016, B-120, B-021, B-150, B-155;
single-region alerting (B-017); memory and disk alarms are inert because the
CloudWatch agent is never configured (B-103).

---

## 9. Incident response and containment

**Criteria:** CC7.3, CC7.4, CC7.5 · HIPAA 164.308(a)(6)(i)–(ii),
164.410 (notification by a business associate)

**Claim.** Security automation isolates and preserves. It never terminates,
deletes or rolls back. If containment would take a production service down,
it stops and pages ([CLAUDE.md](../CLAUDE.md) first hard rule; one recorded
exception, [D-012b](deviations.md): stopping a process or a task).

**Built** ([containment/](../containment/), running from Audit,
[D-014](deviations.md)):

- Isolation and evidence preservation to Security Forensics.
- Incident records in `platform-security-incidents`, with an append-only
  `discovery_time`. That is the clock HIPAA breach timing runs from.
- A monthly containment exercise.
- Alarms on execution failure and on the declaration DLQ.
- Notification windows published at `/platform/incident/notification-windows`.

**Not built or partial:**

- No memory capture (B-053).
- Isolation is proven at the control plane only (B-063).
- A lone production instance can be isolated without checking for
  redundancy (B-168), which conflicts with the rule above.
- **There is no written incident-response or breach-notification runbook.**
  Runbooks exist for operational alarms ([runbooks.md](runbooks.md)) and
  runaway cost ([runbooks/runaway.md](runbooks/runaway.md)). Open item X6
  expects external counsel's contact to be in an incident runbook that does
  not exist.
- The AltDigital→partner BAA and its notification window are unresolved
  (open items X1, X8). The registry's `baa_state` defaults to `unknown`.

**How to check:** read a containment exercise record and its Step Functions
execution (needs §1.3). Confirm `discovery_time` cannot be overwritten (the
condition expression in the containment code). Ask who would call whom, and
in what order, if a tenant reported a breach tomorrow. The answer is not
written down.

---

## 10. Change management

**Criteria:** CC8.1, CC6.8 · HIPAA 164.308(a)(1)(ii)(D), (a)(8)

**Claim.** Changes are not gated. They are **detected** from CloudTrail and
**reconciled** against approved change records. The control works whether or
not anyone remembers it.

**How** ([change/10-reconciliation.yaml](../change/10-reconciliation.yaml),
[change/src/reconcile.py](../change/src/reconcile.py)):

- `platform-capture-deployments` captures 16 deployment event types:
  OIDC role assumption, CloudFormation, ECS, Lambda, launch templates and RDS
  modification.
- Reconciliation runs every 6 hours, looks back 26 hours, and matches within
  24 hours.

| Outcome | Result |
|---|---|
| `matched` | Evidence only |
| `no_change_record` | Tier default. Pages in prod. |
| `outside_approved_window` | Digest and Jira |
| `change_without_deployment` | Digest and Jira |
| `change_source_unavailable` | Digest and Jira |

**Platform changes** reconcile against AltDigital's ops Jira project: two
approvers required, requester excluded. The platform's own source history is
this repository. Every template must pass `cfn-lint` and `cfn-guard` before
it is complete ([scripts/lint.sh](../scripts/lint.sh),
[scripts/guard.sh](../scripts/guard.sh)). B-144 records a publish that
deployed another session's uncommitted code. It is now guarded, and worth
reading.

**Tenant application changes: not reconciled by the platform today.** Arc8's
change records live in the client's Jira, which the platform cannot read
([D-027](deviations.md)). Every Arc8 deployment therefore reconciles as
`change_source_unavailable`. Secrets written by hand in prod are not
reconciled at all (B-152).

**How to check:** sample deployments from the change-events table (needs
§1.3) across all five outcomes. For each `matched`, open the Jira record and
check the approver, the timing and that the requester did not approve. Then
sample git commits to `master` over the period and match each to a
reconciled deployment.

**Gaps:** D-027, B-152, B-070 (applies here too). The repository has no CI
(`.github/` is absent), so lint and guard depend on operator discipline.

---

## 11. Encryption and key management

**Criteria:** CC6.1, CC6.7 · HIPAA 164.312(a)(2)(iv), (e)(1), (e)(2)(ii)

**Claim.** Every data store is encrypted with a customer-managed key. Keys
rotate. Destroying a key needs quorum.

**Keys.**

- **Member baseline** ([baseline/30-kms-and-secrets.yaml](../baseline/30-kms-and-secrets.yaml)):
  `platform-storage`, `platform-database`, `platform-secrets` and
  `platform-logs`, with optional `platform-regulatory` and BYOK.
- **Shared accounts:** `platform-log-archive`, `platform-archive-replica`,
  `platform-artifacts`, `platform-registry`, `platform-alerts` and
  `platform-backup-copies`.
- All have rotation on (BYOK excepted, as AWS does not rotate imported
  material), a 30-day pending window and `DeletionPolicy: Retain`.
- Member key policies deny `ScheduleKeyDeletion`, `DisableKey`,
  `DisableKeyRotation` and `DeleteImportedKeyMaterial` to everyone except
  `PlatformQuorumExecutor`.

**Enforcement.**

- SCP 02 protects keys.
- The deploy role refuses unencrypted RDS, EFS and ElastiCache.
- Creating a secret without a CMK is refused, and a Config rule flags any
  that slip through.
- Every bucket enforces TLS-only access.
- S3 buckets are SSE-KMS with a CMK, versioned, with all public access
  blocked (per snapshot).

**Deviation from design 04:** these platform DynamoDB tables set
`SSEEnabled: true` with no key, which means the **AWS-managed key
(`aws/dynamodb`)**, not a customer-managed key:

- the evidence ledger and sequence
- quorum evidence
- security incidents
- change events
- remediation history
- eligibility state

Only the registry uses a CMK. This is not recorded as a deviation.

**How to check:**

```bash
aws kms list-aliases; aws kms get-key-rotation-status --key-id <id>; aws kms get-key-policy --key-id <id> --policy-name default
aws dynamodb describe-table --table-name platform-evidence-ledger --query 'Table.SSEDescription'   # Audit
aws s3api get-bucket-encryption --bucket <name>
```

---

## 12. Vulnerability management and patching

**Criteria:** CC7.1, CC8.1 · HIPAA 164.308(a)(1)(ii)(B), (a)(5)(ii)(B)

**Claim, per design 09:** vulnerabilities are found continuously, fixed
within an SLA, and an SLA breach is itself an event.

**Actual state:**

- Inspector scans EC2 and ECR. Its findings reach the router through Security
  Hub.
- **Remediation SLAs are not set** (open item B2).
- **Nothing patches.** There is no patch baseline, maintenance window or
  `AWS-RunPatchBaseline` association (B-141). Dev and test are scheduled off
  out of hours, so nothing would wake them to patch anyway.
- SLA-breach events are not built.
- Under the responsibility matrix, application and (in Model A) OS patching
  belong to the client.

**Expect this to be an exception** for any infrastructure the platform runs
on EC2. Today the platform's own compute is Lambda, so the practical exposure
is tenant EC2 under Models B and C.

---

## 13. Backup and recovery

**Criteria:** A1.2, A1.3, CC7.5 · HIPAA 164.308(a)(7)(ii)(A), (B), (D),
164.310(d)(2)(iv)

**Built:**

- Per-account vaults and plans (continuous, plus every 2 hours)
  ([recovery/30-backup-policy.yaml](../recovery/30-backup-policy.yaml)).
- Copies go to a **locked vault in Security Backup**: Vault Lock with
  35–2555 days retention, a 3-day change window, and
  `platform-backup-copy-deleted` alarmed
  ([recovery/40-backup-vault.yaml](../recovery/40-backup-vault.yaml)).
- `platform-backup-job-failed`, for prod only.
- A monthly automated restore test on the second Monday
  ([recovery/20-restore-schedule.yaml](../recovery/20-restore-schedule.yaml)).
- DynamoDB point-in-time recovery on every platform table.

**Not built or partial:**

- **The Organization backup policy has not been written**, although the
  backup template expects one (B-043, partly built).
- Restore validation implements 1 of 5 checks. Records honestly say
  `coverage: control-plane` (B-044).
- Restores read the local copy, not the locked copy (W-003).
- RPO is never evaluated (W-004).
- A restore duration breach does not page in UAT ([D-013](deviations.md)).
- Restore-test isolation (open item V4) is defined by design 05 and its
  network floor is asserted in `recovery/src/worker.py`. IAM-granted reach to
  production is out of that check's scope, and the evidence record says so.

**How to check:** in Audit or Security Backup, list recovery points and
restore jobs over the period:

```bash
aws backup list-restore-jobs
aws backup describe-backup-vault --backup-vault-name platform-backup-copies
```

Then read a restore-test evidence record and check what it actually
validated.

---

## 14. Evidence integrity and retention

**Criteria:** CC4.1, CC7.2, C1.1 · HIPAA 164.312(b), (c)(1), 164.316(b)(2)(i)

**Claim.** Evidence is produced continuously, cannot be silently lost, and is
retained for six years.

**Pipeline** ([evidence/](../evidence/), in Audit):

- `platform-collect-evidence` takes Config, Security Hub, GuardDuty and
  platform events (change, quorum, response, security, recovery, exercise).
- It writes them to `platform-evidence-ledger` (point-in-time recovery, no
  TTL, retained) and delivers them per tenant to Truveon, which has been
  live since 2026-09-23 (B-088).
- Raw logs stay in Log Archive.

**Four completeness controls** (design 04):

1. Sequence numbering (`platform-evidence-sequence`)
2. An hourly heartbeat
3. Buffering: SQS with 14-day retention and a DLQ
4. Count reconciliation every 12 hours

There is also an hourly replay and gap scan. The results of these checks are
themselves written as evidence ([D-016](deviations.md)).

**Gaps:**

- **Log custody is built and has never been deployed** (B-158). This is the
  hourly per-account proof that logs actually arrived.
- The evidence queue and DLQ are not alarmed (B-156).
- Routing evidence by layer is incomplete (B-157).
- Vesting checks that the Truveon tenant is linked, not that evidence was
  received ([D-021](deviations.md)).
- One Object Lock mode applies to every tenant ([D-024](deviations.md)).

**How to check:**

- Pick an hour and a tenant. Count events in CloudTrail and in the ledger,
  and confirm the sequence numbers are contiguous (needs §1.3).
- Pick an object from the log archive and read its retention:
  `aws s3api get-object-retention` (needs §1.3).
- Run `aws cloudtrail validate-logs` across the period.

---

## 15. HIPAA-specific controls

**Criteria:** HIPAA 164.308(b)(1), 164.314(a), 164.316, 164.312(a)(1);
SOC 2 C1.1

**BAA chain.** The design is AltDigital → partner → client.
AltDigital is a subcontractor business associate.

- The repository asserts an Organization-level AWS BAA accepted through AWS
  Artifact. Vesting checks Artifact and refuses to vest a PHI tenant without
  it ([scripts/vest-client.sh](../scripts/vest-client.sh)).
- No acceptance record is held in the repository. **Obtain the Artifact
  acceptance record directly.**
- The downstream agreement and its notification window are open (X1, X8).
- The [responsibility matrix](../design/responsibility-matrix.md) signature
  block is unsigned.

**Eligible services only** ([docs/hipaa-eligibility.md](hipaa-eligibility.md),
[D-030](deviations.md)). For accounts whose `compliance_scope` includes
`hipaa`:

- **uat and prod:** SCP 09 denies every non-eligible service.
- **dev and test:** the same usage is flagged in the digest.

The SCP is **proven in Sandbox (14 of 14 cases) and attached to nothing**,
because no HIPAA uat or prod account exists yet. The safeguard that vesting
attaches it, or refuses to proceed, is not built (B-165). Neither are the
exception path (B-162) or its evidence stream (B-163). The daily usage
sweeps are live.

**Regulated data outside production.** Macie runs in dev the same as in
prod, but automated discovery is off (§7). The tripwire therefore depends on
classification jobs that the questionnaire drives. A resource tagged
`DataClassification=regulated` on a non-eligible service is CRITICAL in
every environment.

**Retention:** six years (2190 days, Object Lock COMPLIANCE), against
164.316(b)(2)(i).

---

## 16. Tenant isolation

**Criteria:** CC6.1, CC6.6 · HIPAA 164.308(a)(4)(ii)(A)

**Claim** ([docs/isolation-tiers.md](isolation-tiers.md)):

- An account per environment per client.
- No network path between tenants: peering, Transit Gateway, VPN and
  endpoint services are denied by SCP 07.
- Keys per account.
- An RCP refusing principals from outside the Organization.
- Snapshot and AMI sharing denied.
- Identity providers restricted.

The egress profiles are `isolated`, `controlled` and `dns-filtered`. The
`locked` profile (Network Firewall) is not built (B-010).

**How to check:**

```bash
aws ec2 describe-vpc-peering-connections; aws ec2 describe-transit-gateway-attachments   # each tenant account: expect none
aws iam list-open-id-connect-providers   # expect GitHub only, where a deploy role exists
```

Then read the trust policy of every role in a tenant account and confirm
that no principal is outside the Organization.

---

## 17. What this repository does not hold

A SOC 2 examination and a HIPAA risk analysis both need things that are not
infrastructure, and none of them are here. This guide cannot evidence them:

- Information security policies, acceptable use, data classification policy
  (the tag values exist; the policy document behind them does not)
- **HIPAA risk analysis and risk management plan** (164.308(a)(1)(ii)(A)–(B)),
  a required specification
- Security awareness training and workforce sanction policy
- Background checks, onboarding and offboarding procedure for AltDigital
  personnel
- Vendor management of AWS, PagerDuty, Atlassian, GitHub and Truveon
  (SOC reports on file, reviewed annually)
- Business continuity and disaster recovery plan as a document; the backup
  mechanics are §13
- Incident response plan and breach notification procedure (§9)
- Board or management oversight of security (CC1.x, CC2.x)
- The signed responsibility matrix and BAAs

---

## 18. Gap register

AltDigital's own assessment of how each gap would land in an examination.
"Exception" means a tester sampling the control would likely record a
failure. "Observation" means a design weakness with compensating controls.
This is a starting position for the rehearsal, not a conclusion.

### Likely exceptions

| Area | Gap | Ref |
|---|---|---|
| Logging | No VPC flow logs | B-012 |
| Logging | No DNS query logs | — |
| Identity | No periodic access review | design 03 |
| Identity | Break-glass and emergency access not built | F-3, V5 |
| Identity | Identity Center MFA enforcement unverified | 15.5b |
| Identity | Standing admin grant, created by hand, not in code | D-007 |
| Patching | No patching, and no remediation SLAs | B-141, B2 |
| Incident | No written incident-response or breach-notification procedure | X6 |
| Backup | No Organization backup policy; restore validation 1 of 5 | B-043, B-044 |
| Change | Tenant application changes not reconciled | D-027 |
| Change | Platform-filed records fail the requester check | B-070 |
| Evidence | Log custody built, never deployed | B-158 |
| Non-infra | Risk analysis, policies, training, vendor management (§17) | — |

### Observations

| Area | Gap | Ref |
|---|---|---|
| Identity | Root MFA virtual; root mailbox personal | D-006, D-001 |
| Identity | OAAR exempt from SCPs, trust not narrowed; no OAAR or root-use detection | 15.5a/b |
| Identity | Deploy-role subject not pinned exactly; wildcard not refused | B-142 |
| Identity | `console_access` flag enforces nothing | B-131 |
| SoD | Quorum is detective, not preventive, against OAAR | §4 |
| Detection | Access Analyzer has no analyzer | B-058 |
| Detection | Single-region aggregation and alerting | B-013, B-017 |
| Detection | Macie discovery samples rather than scans; its cost watch is run by hand | ADR-017 |
| Alerting | Simultaneous router and watchdog failure pages nobody | B-021 |
| Alerting | PagerDuty state local; drift check unscheduled | B-016, B-120 |
| Alerting | Memory and disk alarms inert | B-103 |
| Alerting | 06a thresholds draft | B16 |
| Encryption | Platform DynamoDB tables use the AWS-managed key, not a CMK | §11, unrecorded |
| Logging | CloudTrail not in CloudWatch Logs; S3 data events retained but not yet queryable in Athena | B-005, ADR-013 |
| Logging | Management account forwards no events: organization trail tampering pages nobody | B-175 |
| Logging | Log masking unproven against canonical values | B-009 |
| Containment | No memory capture; lone prod instance isolated without redundancy check | B-053, B-168 |
| HIPAA | Eligible-services SCP unattached (no HIPAA uat/prod yet); vesting gate unbuilt | B-165 |
| HIPAA | Downstream BAA and notification window open | X1, X8 |
| Process | No CI enforcing lint and guard | §10 |
