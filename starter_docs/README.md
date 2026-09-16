# AltDigital Managed Platform — Design Package

**Status:** Draft for review · v0.2 · September 2026
**Operating entity:** AltDigital · `altdigital.ai`
**IP holder:** Foundry · **First partner:** OEight · `oeight.io`
**Owner:** Jamie — jamie@altdigital.ai
**Approvers (baseline):** Jamie (sole approver, interim)
**Key-deletion quorum:** Any two of Jamie, Wayne, Art

### Principals

| Name | Identity | Role in this design |
|---|---|---|
| Jamie | jamie@altdigital.ai | Platform owner; baseline approver; key-deletion quorum |
| Wayne | wayne@altdigital.ai | Key-deletion quorum; BAA and contract terms |
| Art | art@altdigital.ai | Key-deletion quorum; BAA and contract terms |
| Tina | tina@altdigital.ai | Commercials and billing; client-facing event recipient. No operational responsibilities. |

All four are also principals of **OEight** and hold `@oeight.io` addresses as
secondary aliases on the same Entra identity. There is one directory, not two —
see [15 — Partner Model](design/15-partner-model.md).

> **Note:** Tina is deliberately not in the key-deletion quorum (any two of
> Jamie, Wayne, Art) — that is an operational control and her role carries no
> operational responsibility. See [12 — Commercial Access](design/12-commercial-access.md).

---

## What this is

The design for a repeatable, segregated AWS hosting platform supporting multiple
independent member applications under HIPAA, PCI-DSS and SOC 2, with automated
Tier 1 operations and human Tier 2.

This package is **design level**. It deliberately stops short of contract
language, final threshold values, and implementation detail. Those follow once
the design is agreed.

---

## Reading order

| # | Document | What it settles |
|---|---|---|
| 01 | [Design Principles](design/01-design-principles.md) | The rules everything else obeys |
| 02 | [Platform Architecture](design/02-platform-architecture.md) | Accounts, OUs, networking, regions |
| 03 | [Identity & Access](design/03-identity-and-access.md) | Entra → Identity Center, JIT, break-glass |
| 04 | [Security Controls & Evidence](design/04-security-controls-and-evidence.md) | Detective controls, KMS, secrets, Truveon flow |
| 05 | [Resilience](design/05-resilience.md) | Backup, restore, unified recovery path |
| 06 | [Observability](design/06-observability.md) | Auto-instrumentation, canaries, SLOs |
| 07 | [Response Automation](design/07-response-automation.md) | Tiered runbooks, security containment |
| 08 | [Change & Release](design/08-change-and-release.md) | Deployment role, reconciliation, IaC pipeline |
| 09 | [Vulnerability & Patching](design/09-vulnerability-and-patching.md) | Service models, CVE SLAs, pen testing |
| 10 | [Commercial Model](design/10-commercial-model.md) | Cost allocation, tagging, SLA, exit |
| 11 | [Developer Access](design/11-developer-access.md) | Three-layer model, bypasses, telemetry-gap detection, prod data in dev |
| 12 | [Commercial Access](design/12-commercial-access.md) | Billing-only access; client-conversation event class |
| 13 | [Client Provisioning](design/13-client-provisioning.md) | End-to-end onboarding saga across AWS, Jira, PagerDuty, Truveon |
| 14 | [Environment Tiers & Scheduling](design/14-environment-tiers.md) | Four tiers incl. opt-in UAT, HOOPs, seasonal lifecycle |
| 15 | [Partner Model](design/15-partner-model.md) | Four-entity chain, partner/client OUs, three-party responsibility, SOC 2 carve-out |

**Intake artifacts**
- [Onboarding Questionnaire](intake/onboarding-questionnaire.md)
- [Responsibility Matrix](intake/responsibility-matrix.md)

**Truveon**
- [Functional Assumptions](truveon/functional-assumptions.md) — what this design assumes Truveon does, for gap analysis against what exists

**Build artifacts**
- [Claude Code Prompts](prompts/claude-code-prompts.md) — sequenced build instructions

**Diagrams** — `diagrams/` (Mermaid)
- `account-topology` · `account-vesting` · `provisioning-saga` · `evidence-flow` · `alarm-response` · `incident-containment`

**Tracking**
- [Open Items](open-items.md) — what's undecided and who owns it

---

## Conventions used in this package

- **Member account** — an AWS account hosting one environment of one member application

- **Foundry** — IP holding company; owns the platform intellectual property
- **AltDigital** — AWS account owner and payer; operates the platform; holds the SOC 2
- **Partner** — MSP of record to the client. First partner: OEight
- **Client** — owns the application and data. E.g. Arc8, Avergent
- **Platform** — AltDigital-operated shared infrastructure, licensed from Foundry
- **Truveon** — AltDigital's GRC platform; system of record for audit evidence
- **Service model A / B / C** — the three support tiers (see Responsibility Matrix)

---

## Known naming note

Earlier working notes contain misspellings of **Truveon** (Truavion, Truvyon,
Trivion). This package uses the correct spelling throughout. If content is
lifted from older sources, check for variants.

Earlier drafts of this package also described **OEight** as the operating entity.
That was corrected in v0.2: AltDigital operates the platform, OEight is the first
partner.
