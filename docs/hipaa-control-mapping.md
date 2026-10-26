# HIPAA Security Rule — control mapping

Which HIPAA Security Rule safeguards (45 CFR 164.308, 164.310, 164.312, 164.314)
the platform evidences, by which control, and where it does not. Phase 15.6
step 4.

Read this alongside the [auditor guide](auditor-guide.md). The guide walks
each platform control and carries the HIPAA citations per section. This
document runs the other way: it starts from the safeguard.

---

## 1. What this is, and what it is not

**It is** a statement of which automated checks produce evidence relevant to
each safeguard, built from data that can be regenerated (section 2).

**It is not** a claim of HIPAA compliance. HIPAA is technology-neutral. A
passing configuration check shows that a resource is set up the way the
safeguard implies. It does not show that the safeguard is met. AWS says the
same about its own mapping, which this document builds on: the *Operational
Best Practices for HIPAA Security* page is sample guidance, not a compliance
attestation.

**Scope.** These are AWS infrastructure controls in accounts the platform
operates (`platform-layer: infrastructure`). The tenant's application, and
its workforce, policies and training, sit outside the platform. They appear
here only where the safeguard cannot be discussed without them.

---

## 2. How it was built

Built 2026-10-26 from three sources:

1. **AWS's HIPAA mapping.** The Config developer guide page
   *Operational Best Practices for HIPAA Security*, retrieved 2026-10-26. It
   maps 35 HIPAA citations to 129 AWS managed Config rules, one of them a
   manual process check. These are the same rules as AWS's sample conformance
   pack `Operational-Best-Practices-for-HIPAA-Security`, the name
   [vesting/derivation-rules.yaml](../vesting/derivation-rules.yaml) maps to
   `hipaa`.
2. **What Security Hub actually evaluates.** The controls enabled under the
   central configuration policy
   ([security/36-securityhub-policy.yaml](../security/36-securityhub-policy.yaml),
   ADR-011, D-032): FSBP v1.0.0 (338 enabled) and NIST 800-53 r5 (278
   enabled). Rule-to-control pairs came from 10,640 active findings in
   Audit, each of which names both its control and its Config rule.
3. **The platform's own controls**, as described per section of the
   [auditor guide](auditor-guide.md).

**Method.** Each rule in the AWS mapping is placed in one of four groups:

| Group | Meaning | Rules |
|---|---|---|
| Evaluated by Security Hub | An enabled control runs the same rule, or the same check under another rule name | 110 |
| Evaluated by the platform | A platform Config rule or coverage check does it | 2 |
| Satisfied or decided | The service makes it moot, or a recorded decision excludes it | 3 |
| Not evaluated | Nothing in the estate runs it — the delta | 13 |

These counts are by rule. A rule mapped to several citations is counted once.
One process check (annual risk assessment) is outside the four groups.

Fifteen rules evaluate the same check as an enabled control under a
different rule name. Each pairing was confirmed against the control's title,
not inferred from the name:

| AWS HIPAA rule | Control |
|---|---|
| `ec2-instance-managed-by-systems-manager` | SSM.1 |
| `ec2-managedinstance-patch-compliance-status-check` | SSM.2 |
| `iam-password-policy` | IAM.7 |
| `multi-region-cloudtrail-enabled`, `cloudtrail-enabled` | CloudTrail.1 |
| `restricted-common-ports` | EC2.19 |
| `ec2-stopped-instance` | EC2.4 |
| `kms-cmk-not-scheduled-for-deletion` | KMS.3 |
| `dynamodb-in-backup-plan` | DynamoDB.4 |
| `ebs-in-backup-plan` | EC2.28 |
| `rds-in-backup-plan`, `aurora-resources-protected-by-backup-plan` | RDS.26 |
| `s3-bucket-replication-enabled` | S3.7 (cross-region, which is stricter) |
| `redshift-cluster-configuration-check` | Redshift.4, Redshift.10 |
| `sagemaker-notebook-instance-kms-key-configured` | SageMaker.21 |

**Why not the NIST SP 800-66 r2 crosswalk.** NIST 800-53 r5 is the standard
enabled, and NIST publishes a HIPAA-to-800-53 crosswalk. Going through it
would map HIPAA to 800-53 controls, then 800-53 controls to Security Hub
controls, and the second step is AWS's own many-to-many tagging. AWS's HIPAA
page maps straight to the rules that run, and every pairing it gives can be
checked against the estate. The crosswalk is the better source for the
administrative safeguards, which no rule evaluates. If an assessor asks for
it, section 4 is where it belongs.

**To regenerate**, repeat the three reads and the grouping. The
equivalences above are the only hand-made step. Re-check them whenever
Security Hub adds or retires controls.

---

## 3. By safeguard

Section numbers (§) refer to the [auditor guide](auditor-guide.md). "Gaps"
are the guide's gap register (§18) entries that bear on the safeguard.
Appendix A lists every control per citation.

### 164.308 Administrative safeguards

| Safeguard | Platform control | Automated evidence | Gaps |
|---|---|---|---|
| (a)(1)(ii)(A) Risk analysis | None. A tenant and AltDigital process | None. AWS lists it as a process check | Non-infrastructure, §17 |
| (a)(1)(ii)(B) Risk management | Guardrail SCPs §5; vulnerability management §12 | 52 controls: public access, encryption, patch state (SSM.2), stopped instances (EC2.4), key deletion (KMS.3) | No patching or remediation SLAs (B-141, B2) |
| (a)(1)(ii)(D) Information system activity review | Organization trail §6; detection §7; alerting §8; change reconciliation §10 | **Not mapped by AWS's page.** The evidence is the platform's own: CloudTrail.1, .2 and .4, GuardDuty and the router | No flow logs (B-012); no DNS query logs |
| (a)(2) Assigned security responsibility | [Responsibility matrix](../design/responsibility-matrix.md) | None | Matrix unsigned (§15) |
| (a)(3) Workforce security | Identity Center only, no IAM users §3; segregation of duties §4 | 31 controls: IAM.1/.2/.4/.8/.21, KMS.1/.2, public-exposure checks | No periodic access review; standing admin grant (D-007) |
| (a)(3)(ii)(C) Termination procedures | Identity Center group membership, from gitignored `config/identity.env` | IAM.3 (access key rotation) | Removal is a manual discipline; no access review |
| (a)(4) Information access management | Permission sets §3; SCPs §5; tenant isolation §16 | 8 controls for (a)(4)(i), 31 for (ii)(A) and (C), largely the (a)(3) set | OAAR exempt from SCPs (15.5a/b) |
| (a)(5)(ii)(B) Protection from malicious software | GuardDuty and Inspector, org-wide §7, §12 | SSM.1, SSM.2, SSM.3: managed, patched, association compliant. GuardDuty runs everywhere, which `verify-security-coverage.sh` checks | GuardDuty Malware Protection and Inspector Lambda scanning off by choice (B-174) |
| (a)(5)(ii)(C) Log-in monitoring | Root and console sign-in events in the trail §3, §6 | CloudTrail.1, GuardDuty.1, RDS.9 | No root or OAAR use detection (15.5a/b) |
| (a)(5)(ii)(D) Password management | Identity Center. No IAM users anywhere, so IAM password policy governs nobody | IAM.7 (set in every account for hygiene, B-139) | Identity Center MFA enforcement unverified (15.5b) |
| (a)(5)(i), (ii)(A) Training and reminders | None | None | Non-infrastructure, §17 |
| (a)(6) Security incident procedures | Router and PagerDuty §8; containment §9 | GuardDuty.1 for (a)(6)(ii); `verify-security-coverage.sh` (detective services on everywhere) | No written incident-response or breach-notification procedure (X6) |
| (a)(7) Contingency plan | Backup vault, cross-account copies and restore tests §13 | 17 controls: backup plans (DynamoDB.4, EC2.28, RDS.26, EFS.2), PITR (DynamoDB.2), replication (S3.7), versioning and lifecycle (S3.14, S3.10) | No organization backup policy; restore validation 1 of 5 (B-043, B-044) |
| (a)(7)(ii)(D), (E) Testing; criticality analysis | Restore tests §13 | None | B-044 |
| (a)(8) Evaluation | Security Hub continuous evaluation; this mapping; the dress rehearsal | GuardDuty.1; Security Hub itself; `verify-security-coverage.sh` | — |
| (b)(1) Business associate contracts | BAA chain; vesting refuses a PHI tenant without the AWS BAA §15 | None | Downstream agreement and notification window open (X1, X8) |

### 164.310 Physical safeguards

The data centre half is AWS's under the shared responsibility model, and
inherited through the Organization-level BAA (§15). The platform's share is
164.310(d)(2)(iv) *data backup and storage*: §13, the same evidence as
164.308(a)(7). Workstation controls (164.310(b), (c)) concern the tenant's and
AltDigital's own endpoints, and sit outside the platform.

### 164.312 Technical safeguards

| Safeguard | Platform control | Automated evidence | Gaps |
|---|---|---|---|
| (a)(1) Access control | Identity Center permission sets §3; SCPs §5; isolation §16 | 31 controls | As (a)(3)–(a)(4) above |
| (a)(2)(i) Unique user identification | One Identity Center identity per person; no shared or IAM users | IAM.4 (no root access key), the only control AWS maps here | — |
| (a)(2)(ii) Emergency access procedure | **Not built.** AWS's page maps this to backup coverage (11 controls). The safeguard as usually read is break-glass access | 11 controls: Backup.1, the backup-plan controls, PITR, replication | Break-glass not built (F-3, V5) |
| (a)(2)(iii) Automatic logoff | Permission set sessions expire after 1 to 4 hours (`identity/*.yaml`) | None. AWS maps no rule | Session expiry, not idle timeout. Say so if asked |
| (a)(2)(iv) Encryption and decryption | KMS keys with rotation, log archive encryption §11 | 24 controls; `platform-secret-uses-cmk` | Platform DynamoDB tables use the AWS-managed key (§11) |
| (b) Audit controls | Organization trail to Object Lock, six years §6, §14; Config in every account and region (ADR-010) | 16 controls: CloudTrail.1, GuardDuty.1, EC2.6 (flow logs), S3.9 and S3.11, and log delivery per service | **Management events only, so no S3 object reads are recorded**; CloudTrail.5 disabled by decision (ADR-011, B-005) |
| (c)(1) Integrity | Object Lock COMPLIANCE on the archive; CloudTrail digest validation §14 | CloudTrail.4 (log file validation), S3.14 (versioning), S3.15 (Object Lock) | Log custody built, never deployed (B-158) |
| (d) Person or entity authentication | Identity Center with MFA §3 | IAM.5, IAM.6, IAM.9, IAM.19 (MFA); IAM.7. IAM.6 is suppressed for the management root (D-006) | MFA enforcement unverified (15.5b) |
| (e)(1), (e)(2) Transmission security | TLS-only bucket policies; TLS required on the digest (SES.3) §11 | 33 controls for (e)(1); 10 for (e)(2)(i), including S3.5, ELB.1/.2/.3/.8 and APIGateway.2 | — |

### 164.314 Organizational requirements

AWS's page maps 164.314(b), the requirements for *group health plans*, to
encryption and network rules. Those rules are counted in Appendix A as AWS
gives them. The provision itself governs plan sponsors, and the platform
has no evidence role there. 164.314(a), the business associate contracts,
is §15.

---

## 4. Safeguards with no automated evidence

No configuration rule can evidence these. They are listed so their absence
from Security Hub is not read as coverage:

- risk analysis, 164.308(a)(1)(ii)(A);
- assigned security responsibility, (a)(2);
- training and security reminders, (a)(5);
- written incident and breach procedures, (a)(6) (X6);
- contingency plan testing and criticality analysis, (a)(7)(ii)(D)–(E);
- business associate contracts, (b)(1) and 164.314(a).

The evidence for these is documents, signatures and records of
exercises. The guide's §17 lists what the repository does not hold.

---

## 5. The delta — what nothing in the estate evaluates

Step 4 asked for "only the delta as the platform conformance pack". These are
the 13 rules in AWS's HIPAA mapping that no enabled Security Hub control or
platform rule runs. A proposed disposition is given for each. **Nothing here
is deployed yet** (section 7).

| Rule | Citations | Proposed | Why |
|---|---|---|---|
| `backup-plan-min-frequency-and-min-retention-check` | 164.312(a)(2)(ii) | Adopt | Backup plans exist; nothing checks their frequency and retention. Fails until B-043's organization backup policy exists, which is accurate |
| `backup-recovery-point-manual-deletion-disabled` | 164.312(a)(2)(ii) | Adopt | Vault Lock is on the platform's copy vault; this checks every vault, including tenants' |
| `ec2-resources-protected-by-backup-plan` | 164.312(a)(2)(ii) | Adopt | Security Hub checks EBS volumes (EC2.28) but not instances |
| `elbv2-acm-certificate-required` | 164.312(a)(2)(iv), (e)(2)(ii) | Adopt | ELB.2 covers Classic Load Balancers only |
| `ec2-instance-profile-attached` | 164.308(a)(3), (a)(4), 164.312(a)(1) | Adopt | An instance without a profile gets credentials some other way, usually a long-lived key |
| `no-unrestricted-route-to-igw` | 164.312(e)(1) | Adopt | RDS.46 covers RDS only. B-047's public subnets will fail it, which is the point |
| `ecs-task-definition-user-for-host-mode-check` | 164.308(a)(3), (a)(4), 164.312(a)(1) | Adopt | Evaluates nothing until a tenant runs ECS in host mode |
| `emr-kerberos-enabled` | 164.308(a)(3), (a)(4), 164.312(a)(1) | Adopt | Evaluates nothing until a tenant runs EMR |
| `sagemaker-endpoint-configuration-kms-key-configured` | 164.312(a)(2)(iv), (e)(2)(ii) | Adopt | Evaluates nothing until a tenant runs SageMaker endpoints |
| `cloudtrail-s3-dataevents-enabled` | 164.308(a)(3)(ii)(A), 164.312(b) | **Decision** | Fails in every account, because data events are off (§6). Whether ePHI buckets record object access is a cost decision, and it is the most material HIPAA gap here. Adopt the rule with whatever is decided |
| `iam-no-inline-policy-check` | 164.308(a)(3), (a)(4), 164.312(a)(1) | Do not adopt | Platform roles use stack-scoped inline policies by design, so it would flag every one of them. KMS.2 already covers the dangerous case, inline decrypt on all keys |
| `internet-gateway-authorized-vpc-only` | 164.308(a)(1)(ii)(B), (a)(3), (a)(4), 164.312(a)(1), (e)(1) | Not now | Needs a per-account list of authorised VPCs. It belongs with the registry, once a tenant has more than one VPC |
| `dynamodb-throughput-limit-check` | 164.308(a)(1)(ii)(B), 164.312(b) | Do not adopt | A capacity check, not a safeguard. 06a's DynamoDB alarms cover it |

**The three satisfied or decided rules:**
- `cloud-trail-cloud-watch-logs-enabled`: CloudTrail.5 is disabled, because
  detection is EventBridge to the router, not CloudWatch metric filters
  (ADR-011, B-147).
- `s3-bucket-server-side-encryption-enabled`: S3 encrypts every new object
  by default, and Security Hub retired the equivalent control.
- `ec2-instances-in-vpc`: EC2-Classic is retired, so the rule cannot fail.

**Effect on vesting.** `derivation-rules.yaml` maps `hipaa` to AWS's full
sample pack. Deployed as is, it would re-run 110 rules that Security Hub
already evaluates in that account, paying for each evaluation twice and
reporting each failure twice. Nothing deploys it today (phase 15 plan,
15.6 finding 6). The delta pack should replace it.

---

## 6. Cost

Step 4 asks for a before-and-after estimate of Config and Security Hub cost
per account per month. **Not yet available.** Config was turned on in 26
more account-regions on 2026-10-25 (15.6a), and Security Hub moved to the
FSBP and NIST standards on 2026-10-26 (15.6b). Neither shows in billing yet.
The estimate will be stated as a ratio against the pre-15.6 baseline, from a
week of cost and usage data.

The delta pack's own marginal cost is small. It adds 9 rules beside the
381 Security Hub already runs per account-region, and 3 of the 9 have
nothing to evaluate today.

---

## 7. Open decisions

1. **Where the delta pack applies.** Either an organization conformance
   pack from Audit for every account (detective controls are uniform across
   tiers, design 02 and 14), or only accounts whose `compliance_scope`
   includes `hipaa` (design 04: packs "driven by the tenant's frameworks").
   **Recommended: every account.** Each of the nine is ordinary hygiene, and
   none is specific to PHI. Either way, `derivation-rules.yaml` stops
   mapping `hipaa` to AWS's full pack.
2. **S3 data events for buckets holding ePHI**, which decides
   `cloudtrail-s3-dataevents-enabled`. This is the gap an assessor is most
   likely to press on under 164.312(b).

---

## Appendix A — per citation

Generated from the sources in section 2, 2026-10-26. "Not evaluated" lists
AWS-mapped rules that nothing in the estate runs. The citation set is AWS's:
safeguards it does not map, such as 164.308(a)(1)(ii)(D) and
164.312(a)(2)(iii), are absent here and covered in section 3.

| Citation | Safeguard | Security Hub controls (enabled) | Platform or decided | Not evaluated |
|---|---|---|---|---|
| 164.308(a)(1)(ii)(A) | Risk analysis (Required) | — | annual-risk-assessment-performed (process check) | — |
| 164.308(a)(1)(ii)(B) | Risk management (Required) | AutoScaling.1, CloudTrail.2, CloudTrail.4, CodeBuild.1, CodeBuild.2, DMS.1, DynamoDB.1, DynamoDB.2, EC2.1, EC2.3, EC2.4, EC2.7, EC2.9, EC2.13, EC2.18, EC2.19, EC2.20, EFS.1, ELB.1, ELB.2, ELB.6, EMR.1, ES.1, ES.2, ElastiCache.1, IAM.1, IAM.4, KMS.3, Lambda.1, Opensearch.1, Opensearch.2, RDS.1, RDS.3, RDS.4, RDS.5, RDS.11, Redshift.1, Redshift.2, Redshift.4, Redshift.10, S3.2, S3.3, S3.5, S3.7, S3.14, S3.15, SNS.1, SSM.1, SSM.2, SSM.3, SageMaker.1, SageMaker.21 | EC2-Classic retired (service); S3 default encryption (service) | `dynamodb-throughput-limit-check`, `internet-gateway-authorized-vpc-only`, `sagemaker-endpoint-configuration-kms-key-configured` |
| 164.308(a)(3)(i) | Workforce security | Autoscaling.5, CodeBuild.1, CodeBuild.2, DMS.1, EC2.1, EC2.8, EC2.9, EC2.13, EC2.15, EC2.19, ECS.5, EMR.1, ES.2, IAM.1, IAM.2, IAM.4, IAM.8, IAM.21, KMS.1, KMS.2, Lambda.1, Opensearch.2, RDS.1, RDS.2, Redshift.1, S3.1, S3.2, S3.3, S3.12, SSM.4, SageMaker.1 | EC2-Classic retired (service) | `ec2-instance-profile-attached`, `ecs-task-definition-user-for-host-mode-check`, `emr-kerberos-enabled`, `iam-no-inline-policy-check`, `internet-gateway-authorized-vpc-only` |
| 164.308(a)(3)(ii)(A) | Authorization and/or supervision (Addressable) | CloudTrail.1, EC2.6, ECS.5, ES.4, IAM.1, IAM.2, IAM.21, KMS.1, KMS.2, Opensearch.4, RDS.9, S3.9 | CloudTrail.5 disabled (ADR-011) | `cloudtrail-s3-dataevents-enabled`, `ec2-instance-profile-attached`, `ecs-task-definition-user-for-host-mode-check`, `emr-kerberos-enabled`, `iam-no-inline-policy-check` |
| 164.308(a)(3)(ii)(B) | Workforce clearance procedure (Addressable) | Autoscaling.5, EC2.13, EC2.19, ECS.5, IAM.1, IAM.2, IAM.21, S3.12, SSM.4 | — | `emr-kerberos-enabled`, `iam-no-inline-policy-check` |
| 164.308(a)(3)(ii)(C) | Termination procedures (Addressable) | IAM.3 | — | — |
| 164.308(a)(4)(i) | Information access management | EC2.13, EC2.19, ECS.5, IAM.1, IAM.2, IAM.21, S3.12, SSM.4 | — | `emr-kerberos-enabled`, `iam-no-inline-policy-check` |
| 164.308(a)(4)(ii)(A) | Isolating health care clearinghouse functions (Required) | Autoscaling.5, CodeBuild.1, CodeBuild.2, DMS.1, EC2.1, EC2.8, EC2.9, EC2.13, EC2.15, EC2.19, ECS.5, EMR.1, ES.2, IAM.1, IAM.2, IAM.4, IAM.8, IAM.21, KMS.1, KMS.2, Lambda.1, Opensearch.2, RDS.1, RDS.2, Redshift.1, S3.1, S3.2, S3.3, S3.12, SSM.4, SageMaker.1 | EC2-Classic retired (service) | `ec2-instance-profile-attached`, `ecs-task-definition-user-for-host-mode-check`, `emr-kerberos-enabled`, `iam-no-inline-policy-check`, `internet-gateway-authorized-vpc-only` |
| 164.308(a)(4)(ii)(B) | Access authorization (Addressable) | EC2.13, EC2.19, ECS.5, IAM.1, IAM.2, IAM.3, IAM.4, IAM.7, IAM.8, IAM.19, IAM.21, KMS.1, KMS.2, S3.12, SSM.4, SecretsManager.1 | — | `ec2-instance-profile-attached`, `emr-kerberos-enabled`, `iam-no-inline-policy-check` |
| 164.308(a)(4)(ii)(C) | Access establishment and modification (Addressable) | Autoscaling.5, CodeBuild.1, CodeBuild.2, DMS.1, EC2.1, EC2.8, EC2.9, EC2.13, EC2.15, EC2.19, ECS.5, EMR.1, ES.2, IAM.1, IAM.2, IAM.4, IAM.8, IAM.21, KMS.1, KMS.2, Lambda.1, Opensearch.2, RDS.1, RDS.2, Redshift.1, S3.1, S3.2, S3.3, S3.12, SSM.4, SageMaker.1 | EC2-Classic retired (service) | `ec2-instance-profile-attached`, `ecs-task-definition-user-for-host-mode-check`, `emr-kerberos-enabled`, `iam-no-inline-policy-check`, `internet-gateway-authorized-vpc-only` |
| 164.308(a)(5)(ii)(A) | Security reminders (Addressable) | ElasticBeanstalk.2, RDS.13, Redshift.6, SSM.2 | — | — |
| 164.308(a)(5)(ii)(B) | Protection from malicious software (Addressable) | SSM.1, SSM.2, SSM.3 | — | — |
| 164.308(a)(5)(ii)(C) | Log-in monitoring (Addressable) | CloudTrail.1, GuardDuty.1, RDS.9 | — | — |
| 164.308(a)(5)(ii)(D) | Password management (Addressable) | IAM.7 | — | — |
| 164.308(a)(6)(i) | Security incident procedures | — | verify-security-coverage.sh | — |
| 164.308(a)(6)(ii) | Response and Reporting (Required) | GuardDuty.1 | — | — |
| 164.308(a)(7)(i) | Contingency plan | DynamoDB.1, DynamoDB.2, DynamoDB.4, EC2.20, EC2.28, EFS.2, ELB.6, ELB.9, ElastiCache.1, RDS.5, RDS.7, RDS.11, RDS.26, Redshift.3, S3.7, S3.10, S3.14 | — | — |
| 164.308(a)(7)(ii)(A) | Data backup plan (Required) | DynamoDB.2, DynamoDB.4, EC2.28, EFS.2, ElastiCache.1, RDS.11, RDS.26, Redshift.3, Redshift.6, S3.7, S3.10, S3.14 | — | — |
| 164.308(a)(7)(ii)(B) | Disaster recovery plan (Required) | DynamoDB.2, DynamoDB.4, EC2.28, EFS.2, ElastiCache.1, RDS.11, RDS.26, Redshift.3, S3.7, S3.10, S3.14 | — | — |
| 164.308(a)(7)(ii)(C) | Emergency mode operation plan (Required) | DynamoDB.1, EC2.20, ELB.6, ELB.9, RDS.5, RDS.7 | — | — |
| 164.308(a)(8) | Evaluation | GuardDuty.1 | verify-security-coverage.sh | — |
| 164.312(a)(1) | Access control | Autoscaling.5, CodeBuild.1, CodeBuild.2, DMS.1, EC2.1, EC2.8, EC2.9, EC2.13, EC2.15, EC2.19, ECS.5, EMR.1, ES.2, IAM.1, IAM.2, IAM.4, IAM.8, IAM.21, KMS.1, KMS.2, Lambda.1, Opensearch.2, RDS.1, RDS.2, Redshift.1, S3.1, S3.2, S3.3, S3.12, SSM.4, SageMaker.1 | EC2-Classic retired (service) | `ec2-instance-profile-attached`, `ecs-task-definition-user-for-host-mode-check`, `emr-kerberos-enabled`, `iam-no-inline-policy-check`, `internet-gateway-authorized-vpc-only` |
| 164.312(a)(2)(i) | Unique user identification (Required) | IAM.4 | — | — |
| 164.312(a)(2)(ii) | Emergency access procedure (Required) | Backup.1, DynamoDB.2, DynamoDB.4, EC2.28, EFS.2, ElastiCache.1, RDS.11, RDS.26, Redshift.3, S3.7, S3.14 | — | `backup-plan-min-frequency-and-min-retention-check`, `backup-recovery-point-manual-deletion-disabled`, `ec2-resources-protected-by-backup-plan` |
| 164.312(a)(2)(iv) | Encryption and decryption (Addressable) | APIGateway.2, CloudTrail.2, EC2.3, EC2.7, EFS.1, ELB.1, ELB.2, ELB.3, ELB.8, ES.1, ES.3, Kinesis.1, Opensearch.1, Opensearch.3, Opensearch.8, RDS.3, RDS.4, Redshift.2, Redshift.4, Redshift.10, S3.5, S3.17, SNS.1, SageMaker.21 | platform-secret-uses-cmk; S3 default encryption (service) | `elbv2-acm-certificate-required`, `sagemaker-endpoint-configuration-kms-key-configured` |
| 164.312(b) | Audit controls | APIGateway.1, AutoScaling.1, CloudTrail.1, CloudWatch.15, EC2.6, ELB.5, ES.4, GuardDuty.1, Opensearch.4, RDS.6, RDS.9, Redshift.4, Redshift.10, S3.9, S3.11, WAF.11 | verify-security-coverage.sh; CloudTrail.5 disabled (ADR-011) | `cloudtrail-s3-dataevents-enabled`, `dynamodb-throughput-limit-check` |
| 164.312(c)(1) | Integrity | CloudTrail.4, Opensearch.3, Opensearch.8, S3.14, S3.15 | — | — |
| 164.312(c)(2) | Mechanism to authenticate electronic protected health information (Addressable) | CloudTrail.4, S3.5, S3.14, S3.15 | S3 default encryption (service) | — |
| 164.312(d) | Person or entity authentication | IAM.5, IAM.6, IAM.7, IAM.9, IAM.19 | — | — |
| 164.312(e)(1) | Transmission security | APIGateway.2, APIGateway.4, Autoscaling.5, DMS.1, EC2.1, EC2.2, EC2.9, EC2.13, EC2.15, EC2.18, EC2.19, ELB.1, ELB.2, ELB.3, ELB.8, ELB.16, EMR.1, ES.2, ES.3, Lambda.1, Opensearch.2, Opensearch.3, Opensearch.8, RDS.1, RDS.2, Redshift.1, Redshift.2, S3.1, S3.2, S3.3, S3.5, SSM.4, SageMaker.1 | EC2-Classic retired (service) | `internet-gateway-authorized-vpc-only`, `no-unrestricted-route-to-igw` |
| 164.312(e)(2)(i) | Integrity controls (Addressable) | APIGateway.2, ELB.1, ELB.2, ELB.3, ELB.8, ES.3, Opensearch.3, Opensearch.8, Redshift.2, S3.5 | — | — |
| 164.312(e)(2)(ii) | Encryption (Addressable) | APIGateway.2, CloudTrail.2, EC2.3, EC2.7, EFS.1, ELB.1, ELB.2, ELB.3, ELB.8, ES.1, ES.3, Kinesis.1, Opensearch.1, Opensearch.3, Opensearch.8, RDS.3, RDS.4, Redshift.2, Redshift.4, Redshift.10, S3.5, S3.17, SNS.1, SageMaker.21 | platform-secret-uses-cmk; S3 default encryption (service) | `elbv2-acm-certificate-required`, `sagemaker-endpoint-configuration-kms-key-configured` |
| 164.314(b)(1) | Requirements for group health plans | CloudTrail.2, EC2.3, EC2.7, ELB.3, ES.1, ES.3, Opensearch.1, Opensearch.3, Opensearch.8, RDS.3, RDS.4, Redshift.1, Redshift.2, S3.2, S3.3, S3.5, S3.17, SageMaker.21 | S3 default encryption (service) | `sagemaker-endpoint-configuration-kms-key-configured` |
| 164.314(b)(2) | Implementation specifications (Required) | CloudTrail.2, EC2.3, EC2.7, ELB.3, ES.1, ES.3, Opensearch.1, Opensearch.3, Opensearch.8, RDS.3, RDS.4, Redshift.1, Redshift.2, S3.2, S3.3, S3.5, S3.17, SageMaker.21 | S3 default encryption (service) | `sagemaker-endpoint-configuration-kms-key-configured` |
| 164.314(b)(2)(i) | Implementation specifications (Required) | ACM.1, APIGateway.2, CloudTrail.2, DynamoDB.1, DynamoDB.2, EC2.3, EC2.7, EC2.20, ECS.5, EFS.1, ELB.1, ELB.2, ELB.3, ELB.6, ELB.8, ELB.9, ES.1, ES.3, IAM.1, IAM.2, IAM.4, IAM.7, IAM.21, KMS.1, KMS.2, Kinesis.1, Opensearch.1, Opensearch.2, Opensearch.3, Opensearch.8, RDS.3, RDS.4, RDS.5, RDS.7, Redshift.2, Redshift.4, Redshift.10, S3.5, S3.12, S3.15, S3.17, SNS.1, SageMaker.21 | S3 default encryption (service) | `ecs-task-definition-user-for-host-mode-check`, `elbv2-acm-certificate-required`, `no-unrestricted-route-to-igw`, `sagemaker-endpoint-configuration-kms-key-configured` |
