# Onboarding Questionnaire

**Purpose:** This is not a document that gets filed. It is the **input to account
vesting automation**. Every answer has a downstream consumer; if nothing acts on
an answer, the question does not belong here.

It is also an audit artifact in its own right — it records *why* each account is
configured the way it is, and provides a natural re-review trigger when answers
change.

**Legend**
- **[A]** Asked of the customer
- **[D]** Derived and shown for confirmation — not typed
- **[P]** Placeholder — owned elsewhere

---

> **Prerequisite: partner onboarding.** A client can only be onboarded beneath an
> existing, active partner. Partner onboarding is a separate one-time process —
> see [15 — Partner Model](../design/15-partner-model.md). The provisioning saga
> blocks if the partner does not exist, is not active, or has a null
> notification window.

## 1. Identity and commercial

| # | Field | Type | Consumes |
|---|---|---|---|
| 1.0 | **Partner** (MSP of record) | [A] | Partner OU placement, account naming, billing roll-up, notification chain |
| 1.0a | Partner relationship confirmed active | [D] | Blocking precondition |
| 1.0b | Is the partner also the application owner? | [A] | Responsibility matrix — partner appears in two columns |
| 1.1 | Client (member organisation) name | [A] | Client OU, account naming, Cost Categories, Truveon tenant |
| 1.2 | Application name | [A] | Account naming, tagging |
| 1.3 | Application short code | [A] | Account naming, `Application` tag |
| 1.4 | **Service model: A / B / C** | [A] | Patching ownership, SLA scope, DNS+cert ownership, pricing |
| 1.5 | Named technical contact (email) | [A] | Instrumentation digest recipient |
| 1.6 | Escalation contact | [A] | Incident notification |
| 1.7 | Named change approver (customer side) | [A] | Change reconciliation routing |
| 1.8 | Billing contact | [A] | Invoicing |

---

## 2. Compliance scope

| # | Field | Type | Consumes |
|---|---|---|---|
| 2.1 | Frameworks in scope: HIPAA / PCI-DSS / SOC 2 / other | [A] | Conformance packs, retention floor, pen test cadence |
| 2.2 | **Does any data relate to non-US individuals?** | [A] | Residency gate — note this asks about *data subjects*, not customer location |
| 2.3 | Highest data classification held | [A] | Egress profile default, monitoring tier, KMS requirements |
| 2.4 | Is cardholder data stored, processed or transmitted? | [A] | PCI CDE scoping |
| 2.5 | Is PHI stored, processed or transmitted? | [A] | HIPAA scoping, BAA requirement |
| 2.6 | BAA notification window — **partner → client** | **[P]** | Incident clock, downstream leg — *owned by Art / Wayne* |
| 2.6a | BAA notification window — **AltDigital → partner** | [D] | From partner onboarding. Must be tighter than 2.6. Validated, not asked. |
| 2.7 | Conformance packs to apply | [D] | Derived from 2.1 |
| 2.8 | Log retention floor | [D] | Derived from 2.1 — union of applicable frameworks |

---

## 3. Account and network

| # | Field | Type | Consumes |
|---|---|---|---|
| 3.1 | Primary region | [D] | Default applies unless 2.2 forces otherwise |
| 3.2 | **Egress profile: locked / controlled / public-facing** | [A] | Network Firewall / NAT configuration |
| 3.3 | Public internet exposure: inbound / outbound / both / neither | [A] | WAF, CloudFront, Shield decisions |
| 3.4 | Inbound API access required from another tenant? | [A] | Documented as arm's-length public API; no private connectivity is provisioned |
| 3.5 | Outbound integrations requiring allow-listing | [A] | Network Firewall domain list |

---

## 3a. Environments

| # | Field | Type | Consumes |
|---|---|---|---|
| 3a.1 | **UAT environment required?** (default: no) | [A] | Whether a fourth account is vested. Opt-in — raises AWS cost. |
| 3a.2 | If UAT: release cadence — continuous / monthly / quarterly / ad hoc | [A] | Drives HOOP vs seasonal teardown recommendation |
| 3a.3 | If UAT: expected usage pattern — year-round / seasonal | [A] | `dormant` lifecycle eligibility |
| 3a.4 | **HOOP for dev** — days and hours, or always-on | [A] | Scheduler configuration |
| 3a.5 | **HOOP for test** — days and hours, or always-on | [A] | Scheduler configuration |
| 3a.6 | **HOOP for UAT** — days and hours, or always-on | [A] | Scheduler configuration. Note: saving is modest; users test on their own schedule. |
| 3a.7 | Time zone for all HOOPs | [A] | Scheduler configuration |
| 3a.8 | Who may trigger manual turnup outside the HOOP? | [A] | Permission set scope; customer self-service |
| 3a.9 | UAT compliance posture | [D] | **Always production-equivalent for data handling.** Not asked — UAT is in scope by design. |

> UAT holds real data because real users bring it. It is provisioned in
> compliance scope from the outset, which is why 3a.9 is derived rather than
> asked. See [14 — Environment Tiers](../design/14-environment-tiers.md).

---

## 4. Resilience

| # | Field | Type | Consumes |
|---|---|---|---|
| 4.1 | RTO | [A] | Default 4 hours; drives restore test thresholds |
| 4.2 | RPO | [A] | Default 2 hours; drives backup frequency and PITR |
| 4.3 | SLA tier: standard / multi-region premium | [A] | Architecture and pricing |
| 4.4 | Backup retention beyond platform default? | [A] | Backup plan |
| 4.5 | Agreed maintenance window | [A] | Patch Manager, SLA exclusion. **Must fall inside the HOOP** for scheduled environments, or patching silently never runs. |

---

## 5. Security

| # | Field | Type | Consumes |
|---|---|---|---|
| 5.1 | Dedicated KMS key required beyond the standard set? | [A] | Key provisioning |
| 5.2 | FIPS 140-2 or CloudHSM requirement? | [A] | Key architecture |
| 5.3 | External key material / BYOK? | [A] | Key architecture |
| 5.4 | Bringing existing secrets from another vault? | [A] | Secrets migration plan |
| 5.5 | Key-deletion quorum confirmed | [D] | Jamie / Wayne / Art — any two |
| 5.6 | Customer staff requiring account access? | [A] | Federated permission set scope — see [11](../design/11-developer-access.md) |
| 5.7 | **Acknowledgement: no regulated data in dev/test** | [A] | Scope-change position; responsibility matrix |
| 5.8 | Developer manager (digest recipient) | [A] | Dev team digest — services enabled, roles created, guardrail failures |

> Note: 5.1–5.3 are the only encryption questions. Encryption itself is not
> optional and is not asked about.

---

## 6. Licensing

| # | Field | Type | Consumes |
|---|---|---|---|
| 6.1 | Customer-owned licences? (SQL Server, Windows, other) | [A] | Dedicated Host requirement, account architecture |
| 6.2 | Evidence of entitlement provided? | [A] | Compliance record |
| 6.3 | Microsoft licensing via AltDigital or customer? | [A] | Billing line item |
| 6.4 | Dedicated Host / Dedicated Instance required? | [D] | Derived from 6.1 |

---

## 7. Monitoring and service definition

| # | Field | Type | Consumes |
|---|---|---|---|
| 7.1 | **What does "working" look like? What would a user notice first if it broke?** | [A] | Canary definition **and** SLA measurement |
| 7.2 | Endpoint(s) for synthetic monitoring | [A] | Canary configuration |
| 7.3 | Success condition | [A] | Canary assertion |
| 7.4 | Acceptable response time | [A] | Canary timeout **and** SLA pass condition |
| 7.5 | Existing test suite to reuse? (Playwright or other) | [A] | Fargate canary runner vs native Synthetics |
| 7.6 | Authentication required for the journey? | [A] | Canary credential handling via Secrets Manager |
| 7.7 | Monitoring tier | [D] | Derived from environment |

> **7.1 is the only question a customer can genuinely fail to answer well**, and
> it drives two systems. Treat it as a conversation, not a form field. Where
> AltDigital is building the application, AltDigital defines it.

---

## 8. Change management

| # | Field | Type | Consumes |
|---|---|---|---|
| 8.1 | CI/CD tooling in use | [A] | OIDC federation configuration for `AppDeployer` |
| 8.2 | Deployment frequency expectation | [A] | Reconciliation tuning, SLA exclusion context |
| 8.3 | Jira project for change records | [D] | Reconciliation target |
| 8.4 | PagerDuty escalation contacts (customer side, if any) | [A] | Escalation policy |

---

## 9. Exit

| # | Field | Type | Consumes |
|---|---|---|---|
| 9.1 | Acknowledgement of the IP boundary | [A] | Contract reference |
| 9.2 | Acknowledgement of post-exit governance loss | [A] | Contract reference |
| 9.3 | Truveon tenant retention on exit — desired? | [A] | Billing arrangement |

---

## Derived outputs

On completion, the vesting pipeline produces:

- Three accounts — `ad-<partner>-<client>-{dev,test,prod}` — in the client OU
  beneath the partner OU, plus `-uat` where elected
- Baseline StackSet applied, parameterised by the answers above
- Conformance packs per §2
- KMS key set per §5
- Backup policy per §4
- Egress profile per §3
- Canary definition per §7 (production and UAT)
- HOOP schedules per §3a, with turndown/turnup validation
- `AppDeployer` role with OIDC trust per §8
- Truveon tenant registered and first evidence receipt verified
- Platform registry entry with all derived parameters
- A vesting evidence record
- Jira project (shared scheme), change workflow, onboarding epic and task set
- PagerDuty service, escalation policy, routing keys, commercial business service
- Cost Category mapping, budget, fee configuration
- Verification sweep result and go-live gate

See [13 — Client Provisioning](../design/13-client-provisioning.md) for the full
orchestration.

---

## Review triggers

The completed questionnaire is re-reviewed when:

- Service model changes
- Compliance scope changes
- UAT added or removed
- Release cadence changes materially (affects HOOP vs teardown recommendation)
- Data classification changes
- Contacts change (or a digest bounces)
- Annually, regardless
