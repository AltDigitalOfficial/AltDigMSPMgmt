# 04 — Security Controls and Evidence

## Detective control stack

Deployed to every account at vesting, in every environment, without exception.

| Control | Purpose | Aggregates to |
|---|---|---|
| **CloudTrail** (org trail, all regions) | API activity, change detection, attribution | Log Archive |
| **AWS Config** + conformance packs | Resource inventory; continuous compliance evaluation | Audit |
| **GuardDuty** | Threat detection, anomalous behaviour, credential misuse | Audit |
| **Security Hub** | Finding aggregation and normalisation; CIS/PCI standards | Audit |
| **Macie** | Sensitive data discovery in S3 | Audit |
| **Inspector** | CVE scanning — EC2, ECR images, Lambda | Audit |
| **CloudWatch Logs data protection** | Detect and mask sensitive patterns *at write time* | In-account |
| **IAM Access Analyzer** | External access and unused-permission findings | Audit |

Conformance packs applied are driven by the tenant's frameworks — HIPAA
operational best practices, PCI-DSS, plus a baseline AltDigital pack applied
universally.

### The "developer does something unwise" path

This is the specific case the stack exists to catch, and it is worth naming the
mechanisms:

- **New resource appears** → CloudTrail event → EventBridge → auto-instrumentation
  (see [06](06-observability.md)) and Config evaluation within minutes
- **Bucket made public** → Config rule non-compliant → auto-remediation reverts,
  logged as a change event
- **Unencrypted volume created** → Config rule → remediation or page depending on
  whether in-place fix is possible
- **PHI or card data written to stdout** → CloudWatch Logs data protection policy
  masks the pattern as it is written and raises a finding
- **Sensitive data landed in S3** → Macie finding → page

Data protection policies are genuinely underused and are the direct answer to
the stdout problem. They are applied at the log group level by the baseline, so
they cover log groups created after the fact.

---

## Protecting the controls from the account

An SCP applied to the Members OU denies all principals in the member account —
including account-local administrators — from:

- Deleting or modifying the Config recorder, delivery channel, or conformance packs
- Disabling or modifying CloudTrail
- Modifying or deleting platform-managed alarms, SSM associations, or log destinations
- Deleting or altering KMS keys, or modifying key policies
- Modifying backup vaults, vault lock, or backup plans
- Modifying the platform-managed roles (containment, deployer, instrumentation)
- Leaving the Organization

Exception: the `BaselineDeployer` and platform automation roles, which are
excluded by `aws:PrincipalArn` condition.

Without this, a developer with account admin can quietly remove the detective
layer and nothing in the design notices.

---

## Encryption at rest — KMS

**Customer-managed keys, not AWS-managed.** AWS-managed keys cannot carry a key
policy we control, cannot be rotated on our schedule, and do not give the
per-account isolation story. The cost difference is trivial against an audit
finding.

### Standard key set per account

| Key | Covers |
|---|---|
| `storage` | S3, EFS, EBS |
| `database` | RDS, Aurora, DynamoDB |
| `secrets` | Secrets Manager, Parameter Store SecureString |
| `logs` | CloudWatch Logs, CloudTrail, log archive delivery |

One key per data domain — not one per bucket (unmanageable), not one for
everything (key policy becomes a blunt instrument).

### Key policy shape

- Member account principals may **use** the key (encrypt, decrypt, generate)
- Member account principals may **not** disable, delete, or modify the key or
  its policy
- Deletion authority sits with the platform, gated by the two-person quorum
- Automatic annual rotation enabled
- Deletion window defaults to **30 days**, not 7 — that window is the time to
  notice a mistake

### Encryption in transit

Enforced by policy rather than convention: `aws:SecureTransport` deny conditions
in bucket policies, TLS required on RDS parameter groups, HTTPS-only listeners.

### Questionnaire-driven exceptions

Most applications take the default. The intake asks only about genuine
divergence: dedicated regulatory key requirement, FIPS 140-2 / CloudHSM,
external key material or BYOK.

---

## Secrets

- **Secrets Manager** for anything genuinely secret. Parameter Store
  SecureString for configuration that happens to be sensitive. The distinction
  matters because Secrets Manager rotates natively and Parameter Store does not.
- Encrypted with the account's `secrets` key
- **No secrets in environment variables, task definitions, CloudFormation
  parameters, or repositories.** References only, resolved at runtime.
- Rotation enabled wherever the service supports it — RDS credentials rotate via
  managed Lambda
- Where rotation is not supported (most third-party API keys), the defensible
  position is a documented rotation cadence with a named owner, plus detection
  for secrets exceeding their maximum age

### Detection beats policy here

Dev teams will do things. The detective layer:

- Secret scanning in source repositories
- Macie on S3
- CloudWatch Logs data protection masking credential patterns at write
- Config rules for unencrypted resources
- Secrets Manager age monitoring

Combined with tiered auto-remediation, an exposed credential is revoked and
logged rather than sitting until audit.

---

## Evidence pipeline to Truveon

### Direction: push

AltDigital holds administrative rights over every member account and pushes log
and finding data into Truveon. Each member organisation has a **separate Truveon
tenant** — the isolation boundary holds from AWS account through to the evidence
layer.

### What flows

1. **Config compliance state** — continuous control evidence
2. **Security Hub findings** — normalised, severity-scored
3. **CloudTrail-derived change events** — reconciled against the change register
4. **Operational evidence** — restore test results, instrumentation actions,
   remediation outcomes, JIT elevation records, notification timestamps

These three-plus categories are cheap to produce, hard to fake, and identical in
shape regardless of what the application looks like inside.

### Completeness is a control

Because we push, we must prove nothing was silently dropped.

- **Sequence numbering** per tenant stream; gaps are detectable
- **Heartbeat** per tenant — absence of data is itself an event, distinguishable
  from a quiet month
- **Buffering** — ingestion queues (SQS/Kinesis) so a Truveon outage delays
  delivery rather than losing it
- **Reconciliation** — periodic count comparison between source and Truveon

A silently failed pipeline that looks like a clean month is the failure mode
that destroys an audit. This is the control that prevents it.

### Raw vs derived

Truveon holds derived findings and correlated evidence. **Raw logs remain in the
Log Archive account** under Object Lock. An auditor may want the raw record;
know which store answers which question.

See `diagrams/evidence-flow.mermaid`.

---

## Log retention

| Stage | Storage | Duration |
|---|---|---|
| Hot | CloudWatch Logs | 90 days |
| Warm | S3 Standard / Glacier Instant Retrieval | To ~1 year |
| Cold | S3 Glacier Deep Archive | To retention floor |

Operational value is extracted well within 90 days; compliance value is served
by the archive and by Truveon.

**Retention floor is derived, not asked.** It is set per tenant from the union of
applicable frameworks — HIPAA's 6-year documentation requirement, PCI's 1-year
with 3 months immediately available, plus any contractual term. A single
per-tenant value applied uniformly is cheaper to defend than a granular scheme;
over-retaining in Deep Archive costs roughly 1% of hot storage.

**Security logs are not tenant-controlled.** CloudTrail, Config history and
GuardDuty findings land in Log Archive with Object Lock in compliance mode and
carry the platform retention floor from day one, independent of tenant tier.