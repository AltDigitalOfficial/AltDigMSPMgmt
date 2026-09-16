# 02 — Platform Architecture

## Organization structure

A single AWS Organization with all features enabled. Accounts are placed in OUs;
OU placement is what drives baseline inheritance, SCPs and StackSet targeting.

```
Root
├── Security
│   ├── Log Archive          (immutable log destination, Object Lock)
│   ├── Audit                (Security Hub, Config, GuardDuty aggregation)
│   └── Forensics            (incident preservation, isolated)
├── Infrastructure
│   ├── Platform Tooling     (CloudFormation pipeline, StackSet admin)
│   ├── Shared Services      (Truveon ingestion endpoint, platform automation)
│   └── Network              (reserved — centralised egress if adopted)
├── Members
│   ├── <partner>/                    ← partner OU
│   │   ├── <client>/                 ← client OU
│   │   │   ├── ad-<partner>-<client>-dev
│   │   │   ├── ad-<partner>-<client>-test
│   │   │   ├── ad-<partner>-<client>-uat    (opt-in)
│   │   │   └── ad-<partner>-<client>-prod
│   │   └── <client2>/ ...
│   └── direct/                       ← AltDigital's own clients, no partner
└── Sandbox                  (platform canary account for baseline rollout)
```

The **Organization management account** holds nothing but the Organization
itself. No workloads, no pipelines, minimal access. Delegated administration is
used for Config, Security Hub, GuardDuty, Backup, IAM Identity Center and
CloudFormation StackSets.

**Truveon** runs in its own account outside the Members OU. It is consumed over
the public internet like any SaaS product, and funded by a flat per-tenant fee.

**Account naming** is `ad-<partner>-<client>-<env>`, with an application segment
inserted only where a client has more than one application. Full naming rules,
email addressing and the rationale for partner and client being OUs rather than
name prefixes are in [15 — Partner Model](15-partner-model.md).

**The Organization management account root address is
`msp-mgmt@altdigital.ai`** — a monitored distribution list, not a personal
mailbox. Member accounts use plus-addressing from the same base.

---

## Account per application per environment

Each member application receives **three accounts by default** — dev, test,
prod — and optionally a **fourth, UAT**, which is opt-in.

| | dev | test | uat *(opt-in)* | prod |
|---|---|---|---|---|
| Availability zones | Single | Single | Single | Multi-AZ |
| Compliance scope | Out | Out | **In** | In |
| Off-hours scheduling (HOOPs) | Yes | Yes | Conservative | No |
| Guardrails & detective controls | Full | Full | Full | Full |
| Logging & evidence to Truveon | Full | Full | Full | Full |
| Monitoring depth | Light | Light | Moderate | Pervasive |
| Backup | Reduced | Reduced | Full policy | Full policy |
| Canaries | No | Optional | Yes | Yes |

Governance does not vary by environment. Resilience, monitoring depth and cost
do. Sensitive data appears in non-production environments more often than anyone
admits, so detective controls are uniform.

**UAT splits the two axes:** production-equivalent data handling, non-production
resilience. Real users bring real data, so UAT is in compliance scope while
carrying no SLA commitment.

Full detail, including HOOPs and seasonal lifecycle, in
[14 — Environment Tiers and Scheduling](14-environment-tiers.md).

---

## Region strategy

- **US regions only.** us-east-1, us-east-2, us-west-1, us-west-2.
- **Default primary: us-east-2** (or us-west-2). Mature, lower cost than
  us-east-1, and avoids concentration with AWS global control planes.
- Region is a **vesting parameter** with a default, not a per-account debate.
- Non-US data residency is out of scope, but the intake asks about **data
  subjects**, not customer location — a US company can hold EU or UK personal
  data, and GDPR attaches to the subject.
- Multi-region is a **premium SLA tier**, not the baseline. It is not compatible
  with the standard 4h RTO / single-region posture, and is priced separately.

**Platform services** (Log Archive, Audit, Truveon evidence store) are
multi-region regardless of tenant tier. A regional event must not blind us
across every tenant simultaneously.

---

## Networking

### Between member accounts — nothing

There is no Transit Gateway peering, no VPC peering and no private connectivity
between member accounts. If one application calls another, it does so over the
public internet against a published API endpoint, exactly as it would call any
third-party SaaS.

This keeps the isolation story identical to the account story.

### Support access — no bastions, no VPN

Engineer access to instances is via **Systems Manager Session Manager**. No
inbound ports, no SSH key management, no jump hosts. Sessions are logged and
recordable to the Log Archive account, and authorisation flows through the same
Identity Center roles and JIT elevation as everything else.

### Egress — profile per account

Egress posture is a property of the account, selected at vesting:

| Profile | Description | Typical use |
|---|---|---|
| **Locked** | Explicit domain allow-list via Network Firewall; default deny | Internal-only apps, high-sensitivity data |
| **Controlled** | NAT with restricted routing, logged, broad but not open | Default |
| **Public-facing** | Permissive egress, ingress via ALB/CloudFront/WAF | Public web applications |

The value is being able to tell an auditor "this account runs profile X, here is
what that means" rather than reasoning about route tables individually.

### Ingress

Public-facing accounts terminate at CloudFront and/or ALB with AWS WAF. Shield
Standard by default; Shield Advanced is a per-tenant decision driven by exposure
and priced accordingly.

---

## DNS and certificates

Ownership follows OS ownership (see Responsibility Matrix):

- **Model A** — customer owns Route 53 zones and ACM certificates
- **Models B and C** — AltDigital owns both

**Expiry monitoring is AltDigital's under all three models.** An expired
certificate is an availability event and lands against the SLA regardless of who
owned the renewal. Same pattern as patching: detection always ours, remediation
follows the model.

---

## Account vesting

Account creation is a pipeline run, not a project. Inputs come from the
onboarding questionnaire; the pipeline:

1. Creates the account via Organizations / Control Tower Account Factory
2. Places it in the correct OU (which triggers SCP and StackSet inheritance)
3. Applies the baseline StackSet — logging, Config, GuardDuty, KMS keys,
   backup policy, egress profile, alarm framework
4. Registers the account in the platform registry with its derived parameters
5. Registers the corresponding Truveon tenant and verifies first evidence receipt
6. Emits a vesting record as evidence

Three or four accounts are vested per application as a single unit, depending on whether UAT was elected.

**This is one step within the wider onboarding saga**, which also provisions
Jira, PagerDuty, Truveon and billing, creates the human task set, and gates
go-live on a verification sweep. See
[13 — Client Provisioning](13-client-provisioning.md).

See `diagrams/account-vesting.mermaid` and `diagrams/provisioning-saga.mermaid`.
