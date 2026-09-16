# Open Items

Status as of v0.1. Items are grouped by whether they block build work.

---

## Blocking — needed before or during early build

| # | Item | Owner | Notes |
|---|---|---|---|
| B1 | **Default primary region** — us-east-2 or us-west-2 | Jamie | Vesting parameter default; trivial to decide, blocks Phase 3 |
| B2 | **CVE remediation SLAs** per severity | Jamie | Committing beyond capability is worse than committing conservatively |
| B3 | **Restore duration warning/breach thresholds** | Jamie | Principle agreed (two tiers, differentiated routing); values not set |
| B4 | **Circuit breaker and rate limit values** | Jamie | Placeholder 3x/hour used in design |
| B5 | **Second baseline approver** (named) | Jamie | For periods of unavailability; needs occasional exercise |
| B6 | **Digest cadence** — daily or weekly | Jamie | Per-account parameter |
| B7 | **AltDigital CC address (e.g. platform@altdigital.ai)** for dev team digests | Jamie | Mentioned as "set up later" |
| B8 | **Jira: Cloud or Data Center** | Jamie | API capability for project configuration differs meaningfully; affects Phase 12.3 |
| B9 | **Golden Jira scheme design** | Jamie | Built by hand once, shared across all tenants. Blocks provisioning automation. |
| B10 | **Service account identities** for Jira, PagerDuty, Truveon | Jamie | Platform identities, not personal. Tokens in Secrets Manager. |
| B11 | **Blocking vs non-blocking task set** — confirm | Jamie | Draft in [13](design/13-client-provisioning.md); needs sign-off before first onboarding |
| B12 | **Dual-hat SoD action list** | Jamie | Which actions claim AltDigital/partner separation, and the rule preventing one person holding both roles — see [15](design/15-partner-model.md) |
| B13 | **SOC 2 carve-out scoping** | Jamie | Confirm AltDigital's report scope and what remains in each partner's scope |
| B14 | **Root mailbox is a monitored DL; plus-addressing verified** | Jamie | `msp-mgmt@altdigital.ai` — AWS sends root security notices there |
| B15 | **Account alias prefix confirmed** | Jamie | `ad-` assumed; aliases are globally unique across all AWS |

---

## Blocking — owned outside this design

| # | Item | Owner | Notes |
|---|---|---|---|
| X1 | **BAA notification window** per tenant | Art / Wayne | Design treats as a variable; content and negotiation theirs. Unset window must block vesting. |
| X2 | **Contract language — IP boundary and exit** | Art / Wayne | Design position stated in [10](design/10-commercial-model.md) |
| X3 | **Contract language — gross vs net percentage basis** | Art / Wayne | Must not read as cost-plus |
| X4 | **SLA contract clauses** | Art / Wayne | Design level only in this package, per instruction |
| X5 | **Cyber insurance notification window and contact** | Art / Wayne | Coverage often conditional on notice within a defined period |
| X6 | **External counsel contact** | Art / Wayne | Needed in the incident runbook |
| X7 | **Foundry → AltDigital IP licence agreement** | Art / Wayne | Must precede any client contract referencing platform IP; doc 10's exit position depends on it |
| X8 | **AltDigital → OEight subcontract and BAA** | Art / Wayne | AltDigital's notification window must be materially tighter than OEight's downstream window |
| X9 | **OEight's downstream SLA commitments** to Arc8 and Avergent | Art / Wayne | Back-to-back verification — AltDigital's must be at least as strong |

---

## Non-blocking — decide before first customer

| # | Item | Owner | Notes |
|---|---|---|---|
| N1 | **99.995% bonus tier: keep, drop, or reprice as multi-region premium** | Jamie | Not achievable on single-region multi-AZ with 4h RTO. Needs a commercial decision. |
| N2 | **Pricing differentiation by service model** | Jamie | Model A is materially less work than B |
| N3 | **Flat fee and percentage — final values** | Jamie | Indicative $1,000 / 15–20% used in design |
| N4 | **Panel of approved pen test firms** | Jamie | Plus pre-negotiated rules of engagement |
| N5 | **External SLA measurement vendor** | Jamie | Must be named in contract |
| N6 | **Shield Advanced** — per-tenant decision criteria | Jamie | Driven by exposure |
| N7 | **Maintenance window standard** | Jamie | Default offered at intake. Must fall inside the HOOP where one is set. |
| N8 | **Default HOOP templates** | Jamie | e.g. "weekday business hours" as a named option rather than free text at intake |
| N9 | **Re-vest lead time commitment** | Jamie | How long from dormant to usable — sets customer expectation for seasonal UAT |
| N10 | **Manual turnup self-service scope** | Jamie | Whether customer contacts can trigger turnup directly, or via AltDigital |

---

## Deferred by decision

| # | Item | Rationale |
|---|---|---|
| D1 | **Entra ID P2** | Not required while Entra covers AltDigital staff only. Revisit when managing member identities. |
| D2 | **Truveon internal architecture** (multi-region HA, active-passive vs active-active) | Moved to the Truveon project. This design requires only: externally consumed, ingestion buffers, not a single-region dependency. |
| D3 | **Centralised egress via a Network account** | OU reserved; not adopted. Per-account egress profiles are sufficient for now. |
| D4 | **Multi-region tenant workloads** | Premium tier, not baseline |
| D5 | **Per-tenant Jira workflow configuration** | One shared golden scheme instead. Automating per-project config has poor effort-to-value and creates a reconciliation nightmare. |
| D6 | **Tina in the key-deletion quorum** | Quorum is an operational control; her role carries no operational responsibility. Remains Jamie / Wayne / Art. |

---

## Design work remaining

| # | Item | Notes |
|---|---|---|
| R1 | Runbook library content beyond the first five | Framework matters more than volume; grow from real incidents |
| R2 | Service report template (monthly, per tenant) | Format only; contents defined in [06](design/06-observability.md) |
| R3 | Exit runbook with timeline | Design position agreed in [10](design/10-commercial-model.md); procedure not written |
| R4 | Tabletop exercise scenarios | Annual requirement; containment exercise covers part of it |
| R5 | Customer-facing federated access permission set | Scope constraints in [03](design/03-identity-and-access.md), detail in [11](design/11-developer-access.md) |
| R6 | Developer-facing documentation | "Here is everything you can see about how your account is monitored" — converts restriction into feature |
| R7 | Truveon provisioning API specification | Tenant create, framework config, retention, BAA window, evidence-receipt endpoint. Specify from platform side — see [13](design/13-client-provisioning.md) |
| R8 | Truveon per-tenant billing view | Commercial team needs it to reconcile the flat fee without access to evidence content — see [12](design/12-commercial-access.md) |
| R10 | Partner onboarding questionnaire | Distinct from client onboarding — field list drafted in [15](design/15-partner-model.md), not yet an instrument |
| R11 | OEight dual-role modelling | Partner is also application owner for one client; two control sets must not collapse. Truveon thread. |
| R9 | **Truveon gap analysis** | Compare [functional assumptions](truveon/functional-assumptions.md) against what Truveon actually does. 14 open questions and 10 load-bearing assumptions to verify. |

---

## Assumptions requiring periodic verification

These are load-bearing and break quietly. Each needs a scheduled test.

| # | Assumption | Test |
|---|---|---|
| V1 | Member account with full local admin cannot remove detective controls | Phase 1 guardrail harness |
| V2 | Member account cannot delete or alter its own backups | Scheduled, per [05](design/05-resilience.md) |
| V3 | Containment role cannot be blocked by a compromised member account | Phase 7 blast-radius verification |
| V4 | Restore test VPC is genuinely isolated from production | Scheduled, per [05](design/05-resilience.md) |
| V5 | Break-glass access path works | Scheduled, per [03](design/03-identity-and-access.md) |
| V6 | Evidence pipeline completeness (no silent gaps) | Continuous — sequence + heartbeat |
| V7 | Telemetry gap detection actually fires on a stopped agent | Scheduled, per [11](design/11-developer-access.md) |
| V8 | PagerDuty and Jira configuration has not drifted from code | Scheduled, per [13](design/13-client-provisioning.md) |
| V9 | Turnup actually restores a working environment (not just a running one) | Every scheduled turnup + monthly cold cycle, per [14](design/14-environment-tiers.md) |
| V10 | Dormant environments can be re-vested successfully | Scheduled, per [14](design/14-environment-tiers.md) |
| V11 | Maintenance windows fall inside HOOPs | Validated at configuration time and on change |
