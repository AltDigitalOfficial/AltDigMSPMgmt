# 15 — Partner Model

Supersedes the two-party assumption running through docs 01–14. Everything else
in the design holds; this document inserts a layer above the client.

---

## The four-entity chain

```
Foundry            IP holding company. Owns the platform intellectual property.
  │                Licenses it to AltDigital.
  ▼
AltDigital         AWS account owner and payer. Operates the platform.
  │                Holds the SOC 2. Employs the people. Owns Truveon.
  ▼
Partner            MSP of record to the client. Sources the deal, holds the
  │                client relationship and contract. First partner: OEight.
  ▼
Client             Owns the application and the data. E.g. Arc8, Avergent.
```

**AltDigital is a subcontractor to the partner.** The partner is the MSP their
client contracts with; AltDigital operates the infrastructure beneath.

### Why the layer exists rather than being collapsed

The first partner, OEight, is closely held — the same individuals are in both
entities. It would be simpler in the short term to treat OEight as AltDigital.

It is not built that way because the second partner will not be closely held.
Zones, OnStak, or a direct client each need the same shape, and retrofitting a
partner layer after two clients are live is considerably more expensive than
reserving it now.

---

## Organizational structure

```
Members OU
├── oeight/                          ← partner OU
│   ├── arc8/                        ← client OU
│   │   ├── ad-oeight-arc8-dev
│   │   ├── ad-oeight-arc8-test
│   │   ├── ad-oeight-arc8-uat       (opt-in)
│   │   └── ad-oeight-arc8-prod
│   └── avergent/
│       ├── ad-oeight-avergent-dev
│       ├── ad-oeight-avergent-test
│       ├── ad-oeight-avergent-uat   (opt-in)
│       └── ad-oeight-avergent-prod
├── zones/                           ← reserved
├── onstak/                          ← reserved
└── direct/                          ← AltDigital's own clients, no partner
```

Two OU levels — **partner**, then **client** — with accounts inside. Well within
the five-level nesting limit, leaving headroom.

### Why partner and client are OUs, not just name prefixes

| Benefit | Detail |
|---|---|
| **Per-partner SCPs** | A partner with specific contractual constraints gets them at OU level |
| **Per-client SCPs** | Rare but occasionally needed — a client in a regulated niche |
| **StackSet targeting** | Deploy to a partner, a client, or the whole Members OU |
| **Consolidated billing boundary** | Roll-up by partner and by client falls out structurally |
| **Blast radius** | A misapplied policy scoped to one partner, not all |

A flat namespace with a naming convention provides none of these.

### The `direct/` branch

Reserved from the outset. AltDigital landing its own clients is expected, and
without a reserved shape the first one gets bolted on awkwardly. Accounts there
are named `ad-direct-<client>-<env>`.

---

## Naming

```
ad-<partner>-<client>-<env>
ad-<partner>-<client>-<app>-<env>     where a client has multiple applications
```

| Rule | Reason |
|---|---|
| Lowercase alphanumeric and hyphens only | AWS account **aliases** permit nothing else |
| `ad-` prefix | Account aliases are globally unique across all of AWS; the prefix avoids collision and makes ownership obvious in a support ticket |
| No dots | `oeight.io` becomes `oeight`; the full domain is a display attribute in the registry |
| `prod` not `production`; `uat` not `UAT` | Consistency with the tier names used throughout the design |
| Application segment omitted for single-app clients | Most clients have one application; inserting a redundant segment adds noise |

**Account name** in Organizations may be more human-readable
(`OEight — Arc8 — Production`). The alias is the machine-safe identifier.

### Email addressing

Every AWS account requires a unique root email address.

```
msp-mgmt@altdigital.ai                              ← Organization management account
msp-mgmt+ad-oeight-arc8-prod@altdigital.ai          ← member accounts
```

**Requirements:**

- The base mailbox must be a **monitored distribution list**, not a personal
  account — AWS sends root-level security and billing notices there
- Confirm the mail provider delivers plus-addressed mail to the base mailbox
- Root credentials for every account are secured with hardware MFA and are never
  used for routine operations

---

## Identity: one directory, dual aliases

The four principals are employed by AltDigital and also hold OEight addresses.
**This does not mean two identity systems.**

- **One Entra ID tenant**, AltDigital's, holding one identity per person
- `@oeight.io` addresses exist as **secondary SMTP aliases** on the same identity
- New hires receive one account and, where relevant, two addresses
- AWS federation, Identity Center permission sets, JIT elevation and access
  reviews all operate against the single identity

**There are no separate partner staff to federate.** OEight has no in-house IT
and does not intend to add any; if it grows technical resource, that will be an
AltDigital hire who also receives an OEight address.

> This changes if a future partner has its own engineers. That partner would
> need a federated path into its own clients' accounts, scoped to its partner
> OU and never across partners. Reserved as a pattern; not built.

### Partner business users

OEight's CEO and sales function need visibility into their book — clients,
spend, SLA posture, onboarding status — and **no AWS access at all**.

This is the `CommercialReadOnly` shape from [12](12-commercial-access.md),
scoped to the partner OU rather than to AltDigital's whole portfolio. See
[Billing roll-up](#billing-rolls-up-twice).

---

## Segregation of duties for dual-hatted staff

Where a control requires one entity to request and another to approve, **the same
individual acting for both entities is not two-party control**, and an auditor
will say so.

The existing key-deletion quorum (any two of Jamie, Wayne, Art) is an
**intra-AltDigital** control and is unaffected — all three act as AltDigital.

What needs defining is the set of actions where an inter-entity separation is
claimed. For each:

- Which role may not be held simultaneously by one person on that action
- Which named individual acts for which entity on that action
- Enforcement in the quorum verification logic, not only in policy

**Recommended minimum:** where AltDigital requests and the partner approves (or
the reverse), the requesting and approving individuals must be different people
regardless of which entity they are acting for. This is simple to enforce and
removes the question entirely.

> **Open item.** The specific action list needs defining. See
> [open-items.md](../open-items.md).

---

## SOC 2 structure — carve-out

**AltDigital carries its own SOC 2.** Partners rely on it as a subservice
organisation using the **carve-out method** — the partner's scope excludes
AltDigital's controls, and the partner's auditor relies on AltDigital's report.

### Why carve-out rather than inclusive

**It scales.** One AltDigital report serves OEight, Zones, OnStak and any future
partner. The inclusive method would mean re-auditing the same control
environment for every partner added.

**It is commercially load-bearing.** "We hold a SOC 2 covering infrastructure
operations" is what makes AltDigital sellable as a subcontractor to partners who
are not closely held. Without it, every new partner must either take AltDigital
on trust or audit it themselves.

### What sits where

| Entity | Scope |
|---|---|
| **AWS** | Subservice organisation to AltDigital. Its SOC 2, its CUECs. |
| **AltDigital** | Infrastructure operations, platform controls, evidence pipeline, incident response, change management. **Holds the reference SOC 2.** |
| **Partner** | Client relationship, contract and BAA management, notification to the client, vendor management of AltDigital. Small control set; everything technical carves out. |
| **Client** | Application-layer controls per the responsibility matrix. |

### The CUEC chain is three deep

Complementary User Entity Controls stack:

```
AWS assumes AltDigital operates certain controls
  └── AltDigital assumes the partner operates certain controls
        └── The partner assumes the client operates certain controls
```

Each link must be explicit, owned and evidenced. Under Model A the client's list
is long. This is the formal expression of the responsibility matrix, and it is
what an auditor actually tests.

### Partner framework needs vary

OEight requires **SOC 2 and HIPAA** — it is MSP of record and also the
application provider for one of its two clients.

> **Note the dual role.** Within OEight's own scope there are two distinct
> control sets: service-delivery controls covering both clients, and
> application-owner controls covering the one application OEight built. These
> should not be collapsed, or an auditor cannot separate what OEight does as a
> service provider from what it does as an application owner.
>
> For the responsibility matrix this means OEight appears in **two columns** for
> that application. Not structurally difficult, but it must be explicit so
> nobody assumes the MSP column and the app-owner column are different parties.

---

## Three-party responsibility

The A/B/C service models still apply. What changes is that ownership now splits
across three parties rather than two.

The matrix gains a third column: **AltDigital / Partner / Client**.

The interesting cases are where the partner holds a responsibility toward their
client that AltDigital holds toward the partner — for example, under Model A the
partner is responsible to the client for OS patching, while AltDigital is
responsible to the partner for detection and reporting of patch state.

**The client's view is of the partner, not of AltDigital.** The partner's
contract with the client is what the client sees; AltDigital's obligations flow
to the partner.

See [Responsibility Matrix](../intake/responsibility-matrix.md).

---

## Back-to-back service levels

**AltDigital's commitment to the partner must be at least as strong as the
partner's commitment to their client** — otherwise the partner absorbs the gap
and will eventually decline to.

Practical consequences:

| Requirement | Detail |
|---|---|
| Know the downstream commitment | Partner onboarding captures what the partner promises their clients, before AltDigital signs |
| Matching or tighter availability target | AltDigital's number is ≥ the partner's |
| **Exclusion evidence must be presentable downstream** | The change reconciliation proving customer-caused unavailability has to satisfy the *client*, not just the partner — because the partner will need to show it onward |
| Credits flow through | If AltDigital owes the partner a credit, the partner likely owes their client one. The calculations should be consistent. |
| Measurement source is shared | The external measurement vendor is authoritative for both layers, avoiding two versions of the truth |

---

## Notification clocks run in series

Under HIPAA, AltDigital is a **subcontractor business associate** — explicitly
covered by the rules, with obligations flowing to the business associate above
it rather than directly to the covered entity.

```
discovery_time
  │
  ├── AltDigital → Partner        (AltDigital's BAA window — must be tightest)
  │        │
  │        └── Partner → Client   (the partner's BAA window)
  │                 │
  │                 └── Client → individuals / HHS   (statutory, ≤60 days)
```

**AltDigital's window must be materially tighter than the partner's**, or the
chain cannot complete in time. If the partner owes their client 72 hours,
AltDigital owing the partner 72 hours leaves the partner zero time.

### Design consequences

- The notification window is **per partner** and **per client**, not a single
  tenant attribute
- Both windows run simultaneously from the same immutable `discovery_time`
- The incident view must show the **chain**, not just the next deadline
- An unset window at *either* level blocks go-live
- AltDigital notifies the partner; **the partner notifies the client**. The
  distinction between *informed* and *notifier* from [12](12-commercial-access.md)
  now applies at two levels.

---

## Billing rolls up twice

| Roll-up | Consumer | Purpose |
|---|---|---|
| **By client** | Partner | Their invoicing to Arc8, Avergent |
| **By partner** | AltDigital | AltDigital's invoicing to OEight |

Cost Categories handle both dimensions from the account ID, as they already do
for tenant and environment — see [10](10-commercial-model.md).

**New consumer:** the partner needs a view of their own book. That is the
`CommercialReadOnly` permission set scoped to a partner OU, plus partner-level
reporting.

### Pricing operates at two levels

AltDigital's flat-fee-plus-percentage applies to the partner. The partner sets
their own pricing to their clients, which is their commercial decision and not
AltDigital's concern — except that the partner needs per-client cost data
accurate enough to price from.

---

## Intellectual property chain

Doc [10](10-commercial-model.md) states that the client owns their application
and data while the operating entity owns the platform. With four entities the
chain is:

```
Foundry owns the platform IP
  └── licenses to AltDigital, which operates it
        └── AltDigital's obligations to the partner are contractual, not ownership
              └── The client owns their application and data
```

**The exit clause must say the platform IP is licensed, not owned by the
operating entity**, or it contracts away rights whose ownership chain is not
documented.

> **Prerequisite.** The Foundry → AltDigital licence agreement should be in place
> before the first client contract references platform IP. Owned by Art and
> Wayne; flagged here because the exit position in doc 10 depends on it.

---

## Partner onboarding

A **separate, one-time process**, distinct from client onboarding. A partner must
exist before any client can be onboarded beneath it.

### Partner onboarding captures

| Item | Consumes |
|---|---|
| Partner legal entity, trading name, domain | OU naming, registry |
| Contract executed | Go-live gate |
| **BAA with AltDigital**, notification window | Incident clock — must be tighter than the partner's downstream window |
| **Downstream SLA commitments** the partner makes to clients | Back-to-back verification |
| AltDigital's SLA tier to this partner | Commercial |
| Pricing — flat fee and percentage basis | Billing |
| Frameworks the partner requires | SOC 2 carve-out reliance, evidence needs |
| Named contacts — commercial, escalation, change approver | Routing |
| Partner business users requiring commercial access | `CommercialReadOnly` scoped to partner OU |
| Whether the partner has technical staff requiring AWS access | Federation path — not built for OEight; reserved |
| Truveon tenant for the partner | Evidence, per their own framework needs |

### Creates

- Partner OU under Members
- Partner-level SCPs, if any
- Cost Category dimension for the partner
- Truveon tenant for the partner
- Jira project or component for the partner relationship
- Commercial access group in Entra, scoped to the partner OU
- Registry entry in `partner` state

**Client onboarding then happens beneath an existing partner**, using the
questionnaire in [intake](../intake/onboarding-questionnaire.md).

---

## Provisioning saga: partner precondition

The saga in [13](13-client-provisioning.md) gains a precondition and a failure
mode.

**Before step 1:** verify the partner exists, is in `active` state, and has a
non-null notification window. If not, the client onboarding cannot begin.

**New failure mode:** a client onboarded under a partner whose own contract or
BAA is incomplete. This must block, not warn — an account live beneath an
incomplete partner relationship has no defensible notification path.

Partner state is checked at client go-live as well as at client provisioning
start, because a partner relationship can lapse between the two.
