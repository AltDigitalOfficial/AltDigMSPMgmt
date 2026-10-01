# Questionnaire coverage

**Written for:** whoever turns
[client-onboarding-questionnaire.md](client-onboarding-questionnaire.md) into
the partner onboarding webpage, and whoever decides what that page may offer.

**The goal it measures against** (Jamie, 2026-09-30): the questionnaire should
be complete and self-explanatory enough to become a webpage partners fill in
themselves, and onboarding an application space — AWS, Jira, Truveon and
PagerDuty — should then run without Jamie. "Without Jamie" is not "without a
human": the irreversible steps still need an approver. See
[What needs an approver](#what-needs-an-approver).

**Built from** the form, `vesting/questionnaire.schema.json`, and a trace of
which code actually reads each field, on 2026-09-30 at `4e4cd36`; rows
updated as gaps close. Re-run the
trace when a consumer is added. A row saying *nothing reads it* was true at
that commit.

---

## How to read the status column

| Status | Meaning | On a self-service page |
|---|---|---|
| **builds** | The answer changes what is created | Ask it. It works. |
| **checks** | Validated or derived, shown on the review screen, but builds nothing yet | Ask it, and say it is recorded rather than acted on |
| **stored** | In the schema. Nothing reads it | Either wire a consumer or label it "context" |
| **form only** | Asked in the form, absent from the schema. Lost at transcription | Add it to the schema, or drop it |
| **typed by hand** | A consumer exists, but an operator types the value in again somewhere else | The page's answer must flow there, or someone has to re-type it |
| **not built** | The answer asks for a capability that does not exist | Do not offer it as available |

A row can carry two statuses: *stored · not built* means nothing reads the
answer, and nothing could act on it if something did.

---

## Summary

114 rows: the form's 112 numbered questions, plus the operating-hours table
(5a) and the developer roster (3b). Counted by primary status:

| | Count | |
|---|---|---|
| builds | 22 | Identity and naming, service model, frameworks and retention, UAT, egress, the technical contact, change approver and Jira project, both rosters, the Truveon questions, the acknowledgements |
| checks | 15 | Halts, derivations, warnings — the review screen |
| stored | 28 | Captured and unread |
| form only | 49 | Most of sections 2, 7, 13 and 14 |

Sixteen rows also ask for something **not built**, and six have a consumer
that makes someone **type the value again**.

**Two in three of the things a partner is asked (77 of 114) have no effect
on what gets built.** It was 81 when this inventory was first written.
Some of it is legitimately conversation. But a self-service page cannot tell a
partner which half, and a partner who spends an hour on section 11 and then
finds nothing reads it will not trust the next form.

---

## The findings that matter most

In order of how badly they would hurt a self-service onboarding.

### 1. The contacts went nowhere — partly fixed 2026-09-30

Every named contact was captured and none was delivered. Onboarding now
writes the technical contact, development manager, change approver and Jira
project onto the client record, so the digest and change reconciliation have
recipients from the first run. Still open:

- **Development manager (3.6).** The form says this person receives the
  digest. The code sends it to the technical contact. `dev_manager` is now
  written, and still nothing reads it.
- **Security contact (3.5)** is now two fields, written to the registry: the
  breach notification contact of record, which must be someone who can act for
  the organisation, and a security operations contact, who may be a delegate.
  Nothing sends to either yet.
- Escalation (3.2), billing (3.4) and customer escalation for PagerDuty are
  stored and unread.

### 2. Several consumers exist but read something an operator types

| Answer | Where it is re-typed |
|---|---|
| Operating hours and timezone (5a, 5.2) | `HoopConfig` at `publish-scheduling.sh` |
| Response-time threshold (11.4) | `AcceptableResponseMs` at `publish-canary.sh` |
| Endpoints and success condition (11.2, 11.3) | canary parameters |
| CI/CD (12.1) | AppDeployer OIDC parameters |

The page cannot remove Jamie until these read the manifest.

### 3. The page would sell things that do not exist

Multi-region (8.3), RTO and RPO beyond the defaults (8.1, 8.2), dedicated
infrastructure (9.6), a dedicated key (9.1), bring-your-own key (9.3),
inbound internet and WAF (6.1), IP allow-listing (6.5), VPN or Direct Connect
(6.4), certificates and DNS (7.x), certificate expiry monitoring — which the
form promises under **all three** service models — cost alerts (13.2),
business-event alarms (11.10), and patching (5.6, 5.7, B-141).

On a form with AltDigital in the room these become a conversation. On a page
they become an order.

### 4. The service model never reached billing — fixed 2026-09-30

1.7 went to Truveon and nowhere else, and the registry used a vocabulary
(`managed`, `co-managed`, `advisory`) nothing else did. Now A, B and C are
canonical end to end: vesting records the model and compliance scope on every
account, the ServiceModel Cost Category carries it, and the cost report bills
each tenant at its own model. It previously took one model from the
invocation event for the whole run. `scripts/test-cost-report.py`.

### 5. The breach notification window is asked of the wrong party

4.7 asks the client how long they have to notify their customer. The schema
field (`baa_window_partner_to_client_hours`) is described as owned by Art and
Wayne and taken from the contract, not from the client. The first real answer
was "30 days" to a question asking for hours. It is then validated and handed
to Truveon, but never stored where the incident clock reads it (B-059).

### 6. Sections the schema never received

Section 2 (the application) is entirely missing from the schema, including
2.3, which the form marks **Vest**. So are 7 (DNS and certificates), 13
(cost) and 14 (support). 2.7 (compute) is the trigger for B-141 and is
recorded nowhere a later session could see it.

---

## What needs an approver

These are the rows where a wrong answer is permanent, or where the question
calls for judgement a partner cannot be expected to have. On the webpage they
are answered by the partner and **approved by any AltDigital partner** before
vesting. Everything else can flow straight through.

| # | Why |
|---|---|
| 1.3, 1.5 | Become permanent account names and root email addresses |
| 4.1, 4.3, 4.4 | Set the Truveon evidence retention floor, which cannot be shortened for evidence already kept, and which controls apply. The AWS archive is six years for every tenant regardless (D-024) |
| 4.5, 4.6 | Halt vesting. The page should say so as they are answered, not after submission |
| 4.7 | A contractual figure — AltDigital supplies it from the contract |
| 5.1 | UAT is a fourth account; Locked |
| 6.6 | Egress override of the classification default; recorded with an owner |
| 9.2, 9.3 | Locked, and BYOK can make data unrecoverable |
| 9.6–9.8 | Usually cited from a framework that does not require it; Art confirms |
| 16.2 | The Truveon identifier is permanent |

Plus two things no single answer shows: a **related-party** relationship
between the tenant and AltDigital, and **one person holding every role**.
Both surfaced on the first real questionnaire; neither is a question yet.

---

## Every question

**Answered by:** *P* the partner or client can answer on the page · *A*
AltDigital supplies or proposes it · *P→A* partner answers, AltDigital
approves.

### 1. Identity and ownership

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 1.1 | Partner | `identity.partner` | OU, registry, account names, every tenant lookup | P (their login) | builds |
| 1.2 | Client name | `identity.client_name` | registry, Truveon | P | builds |
| 1.3 | Client short code | `identity.client_slug` | account alias, root email, OU | P→A | builds |
| 1.4 | Application name | `identity.application_name` | Truveon only | P | builds |
| 1.5 | Application short code | `identity.application_code` | account alias, root email | P→A | builds |
| 1.6 | Partner owns the IP? | `identity.partner_is_application_owner` | registry `ip-owner` | P | builds |
| 1.7 | Service model | `identity.service_model` | Truveon; registry account records → ServiceModel Cost Category → the rate each tenant is billed at | P | builds |

### 2. The application

None of section 2 is in the schema. Most of it is legitimately conversation;
the rows that are not are marked.

| # | Question | Status | Note |
|---|---|---|---|
| 2.1–2.2 | What it does, who uses it | form only | Context |
| 2.3 | Current state | form only | Form marks it **Vest**; nothing captures it |
| 2.4–2.6 | Migration source, language, packaging | form only | Context |
| 2.7 | Compute | form only | The trigger for B-141. Should be captured |
| 2.8 | Data stores | form only | Would drive backup plan choice |
| 2.9–2.11 | Data volume, load, load shape | form only | Context; 2.11 bears on HOOP |
| 2.12 | Outbound dependencies | form only | Overlaps 6.2; should feed the egress allow-list |
| 2.13 | Inbound integrations | form only | Bears on 6.1 |
| 2.14–2.15 | User authentication, IdP | form only | Application-level, not platform |
| 2.16–2.17 | Data to migrate | form only | Context |
| 2.18–2.19 | Failure modes, runbooks | form only | Context |

### 3. People and access

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 3.1 | Technical contact | `identity.technical_contact` | registry `tech_contact` → daily digest recipient | P | builds |
| 3.2 | Escalation contact | `identity.escalation_contact` | nothing | P | stored |
| 3.3 | Change approver | `identity.change_approver` | registry `change_approver` → tenant facts → change reconciliation routing | P | builds |
| 3.4 | Billing contact | `identity.billing_contact` | nothing | P | stored |
| 3.5 | Breach notification contact | `identity.breach_notification_contact` | registry `breach_contact`; nothing reads it yet (the incident clock records, it does not send) | P | stored |
| 3.5a | Security operations contact | `identity.security_contact` | registry `security_contact`; nothing reads it yet | P | stored |
| 3.6 | Development manager | `security.developer_manager` | written to registry `dev_manager`; nothing reads it. The digest goes to 3.1 | P | stored |
| 3b | Developer roster | roster sidecar | `derive-roster.py`, `grant-developer-access.sh` | P | builds |
| 3.7 | Console access | `security.customer_staff_need_access`? | nothing; the roster decides in practice | P | stored |
| 3.8 | Who may turn on outside hours | `environments.manual_turnup_authorised` | nothing. The manual turnup path does not check it | P | stored |
| 3.9 | Leaver process | — | — | P | form only |

### 4. Compliance and data

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 4.1 | Frameworks | `compliance.frameworks` | conformance packs, Truveon retention floor and frameworks. Not the AWS archive, which is 2190 days for all | P→A | builds |
| 4.2 | If other, which | — | — | P | form only |
| 4.3 | Data classification | `compliance.data_classification` | egress default | P→A | checks |
| 4.4 | PHI | `compliance.phi` | HIPAA consistency, Truveon | P→A | checks |
| 4.5 | Cardholder data | `compliance.cardholder_data` | halts vesting (D-025) | P→A | checks |
| 4.6 | Non-US data subjects | `compliance.non_us_data_subjects` | halts vesting | P→A | checks |
| 4.7 | Breach notification window | `compliance.baa_window_partner_to_client_hours` | validated, Truveon. Not the incident clock (B-059) | A | checks |
| 4.8 | Personal data held | — | — | P | form only |
| 4.9 | Auditor | — | — | P | form only |

### 5. Environments and scheduling

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 5.1 | UAT | `environments.uat_required` | accounts created | P→A | builds |
| 5.2 | Default timezone | `environments.timezone` | derivation. Scheduler's is re-typed | P | checks · typed by hand |
| 5.3 | Release cadence | `environments.release_cadence` | validation only | P | checks |
| 5.4 | Usage pattern | `environments.usage_pattern` | nothing | P | stored |
| 5a | Operating hours | `environments.hoop.*` | derivation. Scheduler's are re-typed | P | checks · typed by hand |
| 5.5 | Freeze periods | — | — | P | form only |
| 5.6 | Production maintenance window | `resilience.maintenance_windows.prod` | derivation. Nothing patches | P | checks · not built |
| 5.7 | Non-production maintenance window | `resilience.maintenance_windows.nonprod` | derivation. Nothing patches or wakes (B-141) | P | checks · not built |

### 6. Network and connectivity

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 6.1 | Inbound / outbound | `network.internet_exposure` | nothing. No ingress scaffolding (B-011) | P | stored · not built |
| 6.2 | Outbound destinations | `network.outbound_allowlist` | nothing. The DNS Firewall list is hand-written | P | stored |
| 6.3 | Another client's API | `network.inbound_cross_tenant_api` | nothing | P | stored |
| 6.4 | Connectivity to own network | — | — | P | form only · not built |
| 6.5 | IP allow-listing | — | — | P | form only · not built |
| 6.6 | Egress profile | `network.egress_profile` | network baseline | A proposes, P→A | builds |

### 7. Domains, DNS and certificates

| # | Question | Status | Note |
|---|---|---|---|
| 7.1 | Domain names | form only | Needed by the canary; re-typed there |
| 7.2 | DNS zone holder | form only | |
| 7.3 | Certificates | form only · not built | Contradicts model A when AltDigital issues |
| 7.4 | Wildcard / EV | form only | |
| — | Expiry monitoring | not built | Promised under all three models; nothing monitors certificate expiry |

### 8. Resilience and recovery

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 8.1 | RTO | `resilience.rto_hours` | nothing. Backup policy is fixed at design 05's defaults | P | stored · not built beyond default |
| 8.2 | RPO | `resilience.rpo_hours` | nothing | P | stored · not built beyond default |
| 8.3 | SLA tier | `resilience.sla_tier` | nothing. Multi-region not built | P | stored · not built |
| 8.4 | Backup retention | `resilience.backup_retention_days` | nothing. Copy vault is fixed at 35 days | P | stored |
| 8.5 | Datasets with different needs | — | — | P | form only |
| 8.6 | Restore ever tested | — | — | P | form only |

### 9. Security

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 9.1 | Dedicated key | `security.dedicated_kms_key` | nothing | P | stored · not built |
| 9.2 | FIPS / CloudHSM | `security.fips_or_cloudhsm` | nothing. Marked Locked | P→A | stored · not built |
| 9.3 | Own key material | `security.external_key_material` | warning only. No key is created | P→A | checks · not built |
| 9.4 | Migrating secrets | `security.migrating_secrets` | nothing | P | stored |
| 9.5 | Staff access to prod data | `security.customer_staff_need_access`? | nothing. Mapping to 3.7 or 9.5 is ambiguous | P | stored |
| 9.6 | Dedicated infrastructure | `security.dedicated_infrastructure_required` | warning only. No tenancy mechanism | P→A | checks · not built |
| 9.7–9.8 | Driver, control reference | `security.dedicated_infrastructure_driver`, `_control_ref` | presence checked only | P→A | checks |
| 9.9 | No regulated data in lower environments | `security.no_regulated_data_in_lower_envs_ack` | schema `const` — cannot submit without | P | builds (gate) |

### 10. Licensing

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 10.1 | Licences to bring | `licensing.customer_owned_licences` | derives `dedicated_host_required`. Nothing reads that | P | checks · not built |
| 10.2 | Entitlement evidence | `licensing.entitlement_evidence_provided` | nothing. A Jira go-live task asks again | P | stored |
| 10.3 | Microsoft licensing via | `licensing.microsoft_licensing_via` | nothing | P | stored |

### 11. Monitoring and service definition

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 11.1 | What "working" means | `monitoring.working_definition` | nothing. A Jira go-live task asks again | P | stored |
| 11.2 | Endpoints | `monitoring.endpoints` | nothing. Canary parameters are re-typed | P | stored · typed by hand |
| 11.3 | Success condition | `monitoring.success_condition` | nothing | P | stored · typed by hand |
| 11.4 | Unacceptable response time | `monitoring.acceptable_response_ms` | nothing. `AcceptableResponseMs` is re-typed | P | stored · typed by hand |
| 11.5 | Journey needs login | `monitoring.journey_requires_auth` | nothing | P | stored |
| 11.6 | Test account | — | — | P | form only |
| 11.7 | Existing test suite | `monitoring.existing_test_suite` | nothing | P | stored |
| 11.8–11.9 | Structured logs, metrics | — | — | P | form only |
| 11.10 | Business events to alert on | — | — | P | form only · not built |

### 12. Change management

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 12.1 | CI/CD tooling | `change.cicd_tooling` | nothing. AppDeployer OIDC parameters are re-typed | P | stored · typed by hand |
| 12.2 | Deployment frequency | `change.deployment_frequency` | nothing | P | stored |
| 12.3 | Existing change process | — | — | P | form only |
| 12.4 | Ticketing: system, product, site, project | `change.ticketing` | registry → tenant facts → change reconciliation, on the client's own Atlassian site where given. Creating the project and the client's service account is still manual | P | builds |
| 12.5 | Production change approver | — | — (duplicates 3.3) | P | form only |
| 12.6 | Business test plan | — | — | P | form only |

### 13. Cost

| # | Question | Status | Note |
|---|---|---|---|
| 13.1 | Budget expectation | form only | |
| 13.2 | Alert threshold | form only · not built | No AWS Budgets exist anywhere in the repository |
| 13.3 | Cost report recipients | form only | |
| 13.4 | Trade availability for cost | form only | |

### 14. Support and escalation

| # | Question | Status | Note |
|---|---|---|---|
| 14.1 | Business hours | form only | Free text, and a timezone outside the section 5 treatment |
| 14.2 | Out-of-hours support | form only | Derivation pages prod out of hours regardless |
| 14.3 | How to raise issues | form only | |
| 14.4 | Who may declare an incident | form only | |

### 15. Exit

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 15.1 | IP boundary | `exit.ip_boundary_ack` | schema `const` | P | builds (gate) |
| 15.2 | Governance ends on exit | `exit.post_exit_governance_loss_ack` | schema `const` | P | builds (gate) |
| 15.3 | Retain evidence after exit | `exit.truveon_retention_desired` | nothing | P | stored |
| 15.4 | Data return format | — | — | P | form only |

### 16. Truveon

| # | Question | Schema field | Read by | Answered by | Status |
|---|---|---|---|---|---|
| 16.1 | Existing workspace | `truveon.existing_tenant` | `truveon-tenant-handoff.sh` | P | builds |
| 16.2 | Workspace name | `truveon.tenant_name`, `tenant_slug` | handoff | P→A | builds |
| 16.3 | Legal structure | `truveon.legal_entities` | handoff | P | builds |
| 16.4 | Invite now or at walkthrough | `truveon.send_invites` | handoff | P | builds |
| 16.5 | Truveon roster | Truveon roster sidecar | handoff | P | builds |

Tenant creation itself is a person running Truveon's CLI from a prepared
pack. That is deliberate (Truveon D-26: no create-tenant API), and at today's
volume it is not the bottleneck.

---

## Not questions, but on the path to "without Jamie"

Found while tracing, and none of them is fixed by a better form:

- **Jira** — the platform credential cannot create projects (B-108), and the
  go-live checklist names Jamie as owner of three blocking tasks: the
  responsibility matrix, the critical journey where AltDigital builds the
  application, and go-live approval.
- **PagerDuty** — applied by Terraform from a working copy, twice per tenant.
  `onboard-client.sh` explains why that cannot live in a Lambda.
- **The provisioning saga** is written and not deployed; four of its ten
  steps raise `StepNotImplemented` by design. `onboard-client.sh` is the
  workstation-side stand-in.
- **The partner registry record** needs contractual notification windows read
  out of a contract before any client can sit beneath the partner.
- **The account quota** (B-140) is ten, and each tenant takes three or four.
- **Personal data storage.** `completed_qs/` is a working directory, not a
  system of record. A webpage that collects rosters and contacts needs real
  storage with access control and retention.
