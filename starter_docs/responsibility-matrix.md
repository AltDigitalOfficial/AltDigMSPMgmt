# Responsibility Matrix

Completed per application at onboarding and attached to the service agreement.
Prevents compliance debt being inherited silently.

**AD** = AltDigital · **P** = Partner (MSP of record) · **C** = Client ·
**AD\*** = AltDigital detects, owner remediates

### Three parties, two contracts

```
AltDigital ──contract──▶ Partner ──contract──▶ Client
```

**The client's view is of the partner.** AltDigital's obligations flow to the
partner, and the partner's flow onward to the client. Where a row shows **AD**,
AltDigital performs it and the partner carries it to the client as their own
commitment.

Where the partner is also the application owner — as OEight is for one of its two
clients — the partner appears in **both** the P and C columns for that
application. This must be explicit so nobody assumes they are different parties.
See [15 — Partner Model](../design/15-partner-model.md).

### Reading the tables below

The A / B / C columns are **service models**, not parties. Each cell names the
responsible party under that model. Unless stated otherwise, a cell showing
**AD** means AltDigital performs the function and the partner is accountable to
the client for it.

---

## Infrastructure and platform

| Function | Model A | Model B | Model C |
|---|---|---|---|
| AWS account provisioning and lifecycle | AD | AD | AD |
| Organization guardrails / SCPs | AD | AD | AD |
| VPC, subnets, routing | AD | AD | AD |
| Egress control and firewall policy | AD | AD | AD |
| Load balancers, WAF, CloudFront | AD | AD | AD |
| DNS (Route 53) | **C** | AD | AD |
| TLS certificates (issuance, renewal) | **C** | AD | AD |
| Certificate expiry monitoring | **AD\*** | AD | AD |

---

## Security

| Function | Model A | Model B | Model C |
|---|---|---|---|
| CloudTrail, Config, GuardDuty, Security Hub | AD | AD | AD |
| KMS key provisioning and rotation | AD | AD | AD |
| Key deletion authority | AD (quorum) | AD (quorum) | AD (quorum) |
| Secrets Manager provisioning | AD | AD | AD |
| Secret content and rotation cadence | **C** | **C** | AD |
| IAM roles for application workloads | **C** | **C** | AD |
| Platform IAM roles | AD | AD | AD |
| Security incident detection | AD | AD | AD |
| Security incident containment | AD | AD | AD |
| Security incident investigation | AD + C | AD + C | AD |
| Breach notification to **partner** | AD | AD | AD |
| Breach notification to **client** | **P** | **P** | **P** |
| Breach notification to individuals / regulators | **C** | **C** | **C** |

> AltDigital is a **subcontractor Business Associate** under HIPAA. Its
> obligation is to notify the **partner** within AltDigital's BAA window. The
> partner notifies the client; the client notifies individuals and HHS. The
> clocks run in series and AltDigital's must be tightest — see
> [15 — Partner Model](../design/15-partner-model.md).

---

## Patching and vulnerability

| Function | Model A | Model B | Model C |
|---|---|---|---|
| CVE scanning (Inspector) | AD | AD | AD |
| Patch compliance reporting | AD | AD | AD |
| OS patching execution | **C** | AD | AD |
| Base / container image maintenance | **C** | AD | AD |
| Middleware and runtime patching | **C** | **C** | AD |
| Application dependency patching | **C** | **C** | AD |
| Golden AMI pipeline | n/a | AD | AD |
| CVE remediation SLA ownership | **C** | AD (OS) / C (app) | AD |

---

## Resilience

| Function | Model A | Model B | Model C |
|---|---|---|---|
| Backup policy and vault management | AD | AD | AD |
| Backup immutability (Vault Lock) | AD | AD | AD |
| Restore execution | AD | AD | AD |
| Monthly restore testing | AD | AD | AD |
| Application-level data validation after restore | **C** | **C** | AD |
| Multi-AZ architecture (production) | AD | AD | AD |
| HOOP scheduling — turndown and turnup execution | AD | AD | AD |
| HOOP schedule definition | **C** | **C** | **C** |
| Manual turnup outside HOOP | **C** or AD | **C** or AD | **C** or AD |
| Seasonal teardown and re-vest of UAT | AD | AD | AD |
| Application resilience to AZ failure | **C** | **C** | AD |

---

## Observability

| Function | Model A | Model B | Model C |
|---|---|---|---|
| Infrastructure alarms (auto-instrumented) | AD | AD | AD |
| CloudWatch agent deployment | **C** | AD | AD |
| Application logging (emission) | **C** | **C** | AD |
| Log aggregation, retention, archive | AD | AD | AD |
| Synthetic canaries (operation) | AD | AD | AD |
| Critical journey definition | **C** | **C** | AD |
| Tier 1 automated response | AD | AD | AD |
| Tier 2 human response — infrastructure | AD | AD | AD |
| Tier 2 human response — application | **C** | **C** | AD |

---

## Change and release

| Function | Model A | Model B | Model C |
|---|---|---|---|
| `AppDeployer` role provisioning | AD | AD | AD |
| Application deployment execution | **C** | **C** | AD |
| Change record creation | **C** | **C** | AD |
| Change approval | **C** | **C** | AD |
| Change reconciliation and reporting | AD | AD | AD |
| Platform baseline changes | AD | AD | AD |

---

## Compliance

| Function | Model A | Model B | Model C |
|---|---|---|---|
| Evidence collection to Truveon | AD | AD | AD |
| Control operation (infrastructure) | AD | AD | AD |
| Control operation (application) | **C** | **C** | AD |
| **Keeping regulated data out of dev/test accounts** | **C** | **C** | AD |
| Regulated data in **UAT** — expected, in scope by design | n/a | n/a | n/a |
| Penetration test coordination | AD | AD | AD |
| Penetration test execution | 3rd party | 3rd party | 3rd party |
| Pen test finding remediation | Per finding owner | Per finding owner | AD |
| Audit response — infrastructure | AD | AD | AD |
| Audit response — application | **C** | **C** | AD |
| Regulatory relationship | **C** | **C** | **C** |

> **Scope change, not policy violation.** Regulated data found in a dev or test
> account puts that account in audit scope and it will be treated as
> production-equivalent — full retention, full controls, full cost — until
> remediated and attested. See [11 — Developer Access](../design/11-developer-access.md).

---

## SLA

| | Model A | Model B | Model C |
|---|---|---|---|
| Availability commitment | 99.95% | 99.95% | 99.95% |
| Scope | End-to-end, customer-caused excluded | End-to-end, customer-caused excluded | End-to-end, total |
| Exclusion evidence | Change reconciliation | Change reconciliation | n/a |

---

## Partner-level responsibilities

Independent of service model. These sit with the partner in every case.

| Function | Party |
|---|---|
| Client contract and commercial relationship | **P** |
| Client BAA negotiation and terms | **P** |
| Breach notification to the client | **P** |
| Client-facing SLA commitment | **P** |
| Vendor management of AltDigital | **P** |
| Client invoicing | **P** |
| Contract and BAA with AltDigital | **P** + AD |
| Infrastructure operations, detection, evidence | AD |
| SOC 2 for infrastructure operations (carve-out basis) | AD |

---

## Signature block

| | Name | Date |
|---|---|---|
| AltDigital | | |
| Partner | | |
| Client | | |

*Reviewed annually and on any change to service model or compliance scope.*
