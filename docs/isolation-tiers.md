# Isolation Tiers and the Dedicated-Infrastructure Question

**Written for:** the AltDigital team having this conversation with a client —
Jamie on the technical answer, Tina on the commercial one, Art where a framework
is being cited. It is not a client-facing document as written; the argument is
here, the tone is not.

---

## The pattern this exists for

A prospect says they require dedicated infrastructure. Sometimes a framework
genuinely requires it. More often it is a procurement reflex, or a memory of a
datacentre where "shared" meant a shared operating system.

Without a prepared answer, two bad outcomes:

- the deal is conceded on and the platform absorbs a 5–25× cost increase that
  was never priced, or
- the objection is dismissed and the prospect concludes AltDigital does not take
  isolation seriously

The position below is that **shared is already strongly isolated**, that the
dedicated option exists and is priced, and that the choice is the client's to
make with the numbers in front of them.

---

## The default answer: what a client already gets

Say this before discussing anything dedicated, because it is usually enough.

**Every client gets their own AWS accounts.** Three or four of them, one per
environment. This is not a shared tenancy with logical separation — it is the
strongest isolation boundary AWS offers, stronger than a VPC and stronger than
instance tenancy. A credential compromise in one account reaches nothing in
another, because there is no trust relationship to traverse.

**There is no network path between clients.** No VPC peering, no Transit
Gateway, no shared services VPC. Design doc 02 forbids it. If one application
calls another it does so over the public internet against a published API,
exactly as it would call any third-party SaaS. Most platforms cannot say this;
the usual pattern is a shared inspection VPC, which is cheaper and creates
exactly the path being denied here.

**Compute is already hardware-isolated.** The AWS Nitro System enforces
separation in hardware rather than in a hypervisor. There is no general-purpose
hypervisor to compromise, and no operator access path — AWS staff cannot read
instance memory. This is the substantive change since the contact-centre era,
when "shared" did mean a shared OS kernel and the concern was well founded.

**Encryption keys are per account.** Storage, database, secrets and logs keys
are created in each account with per-account policies. No client's data is
encrypted with a key another client's account can reach.

If the objection survives all of that, it is usually either a real contractual
clause or a procurement checkbox. Both are legitimate; neither is a security
argument, and it is worth establishing which one you are dealing with.

---

## Egress tiers — the choice that actually exists

Per account per month, us-east-2, before data processing.

| Tier | What it is | single-AZ | multi-AZ |
|---|---|---|---|
| **isolated** | No NAT. VPC endpoints reach AWS services; nothing reaches the internet. | **$29** | **$58** |
| **controlled** | NAT, restricted routing, logged. **The default.** | $62 | $124 |
| **dns-filtered** | NAT plus Resolver DNS Firewall. Blocks at name resolution. | ~$70 | ~$132 |
| **locked** | Network Firewall, L7 allow-list. **Not built** — B-010. | $350 | $701 |

Two things worth noticing.

**`isolated` is cheaper *and* stronger than the default.** If an application is
genuinely internal-only — which is how design doc 02 describes the locked
profile's intended audience — there is no list to maintain, no rule to drift,
and no appliance to pay for. It is the right answer more often than it gets
chosen, because "no internet access" sounds like a limitation rather than a
control.

**`dns-filtered` is most of the value at about 2% of the price.** It blocks at
resolution, which catches the large majority of command-and-control and
exfiltration paths, because almost everything resolves a name first. Be honest
about what it does not do: no traffic inspection, no TLS SNI filtering, and
nothing against a connection to a hard-coded IP. It is not equivalent to
Network Firewall and should not be sold as though it were.

### Why `locked` is so expensive here specifically

Network Firewall is normally made affordable by a shared inspection VPC — one
firewall, many accounts, cost amortised across them. **This platform forbids
that**, because the shared VPC is exactly the cross-account path the isolation
story depends on not existing.

So the isolation guarantee has a price, and this is it: every appliance-shaped
control must be replicated per account. That will recur for anything similar —
IDS, forward proxy, inline DLP. Worth saying out loud to a client who asks why
it costs what it does, because it is a consequence of a decision made in their
favour.

---

## Questionnaire addition: dedicated infrastructure

Extends section 5 of the onboarding questionnaire. Section 6 already asks about
Dedicated Hosts, but for **licensing** (BYOL Windows and SQL Server). This is
the separate, security-motivated question, and conflating the two produces
confusing answers.

| # | Field | Type | Consumes |
|---|---|---|---|
| 5.9 | **Dedicated infrastructure required beyond account-level isolation?** Default **no**. | [A] | Egress tier, EC2 tenancy, pricing |
| 5.9a | If yes: what specifically requires it — contract clause, framework control, or procurement policy? | [A] | Determines whether it is negotiable |
| 5.9b | If a framework: which control reference? | [A] | Art — verify the control actually says this |
| 5.10 | Egress tier: isolated / controlled / dns-filtered / locked | [A] | `EgressProfile` |
| 5.11 | EC2 tenancy: shared / dedicated-instance / dedicated-host | [A] | Launch templates, pricing |

**5.9b earns its place.** Frameworks are cited far more often than they are
read. HIPAA, PCI-DSS and SOC 2 all require isolation of cardholder or protected
data; **none of them require dedicated physical hardware**, and PCI-DSS
explicitly contemplates shared hosting with appropriate controls. If a client
cites a control, ask which one. Often the honest answer is procurement policy —
which is a legitimate reason to buy dedicated capacity, but it is a commercial
decision rather than a compliance one, and it should be priced as such rather
than absorbed.

### EC2 tenancy, for completeness

| Tenancy | Premium | What it actually buys |
|---|---|---|
| shared | baseline | Nitro hardware isolation. The default. |
| dedicated-instance | ~10% on instance hours, plus ~$2/hour per region | No other AWS customer on the same host |
| dedicated-host | Priced per host, not per instance | A specific physical server, visible socket and core counts. Usually chosen for BYOL licensing, not security. |

The security delta between shared and dedicated-instance on Nitro is small and
largely theoretical — it addresses side-channel classes that Nitro was designed
to close. Say so plainly, then let the client decide. A client who buys
dedicated tenancy having heard that is a satisfied client; one who discovers it
later is not.

---

## How to run the conversation

1. **Lead with what they already get.** Own accounts, no network path, Nitro,
   per-account keys. This resolves most objections.
2. **Find out what is driving it.** Contract clause, framework control, or
   procurement policy — the answer changes everything downstream.
3. **If a framework is cited, ask for the control reference.** Art can confirm
   whether it says what the client believes it says. Usually it does not.
4. **Offer the tiers with the numbers.** `isolated` is often both cheaper and
   more restrictive than what they were asking for, which reframes the
   conversation productively.
5. **If they still want dedicated, sell it.** It is a legitimate product. Price
   it explicitly, put it in the contract, and make sure the uplift reflects that
   AltDigital carries no additional margin on a pass-through cost.

**Do not absorb it.** A $1,442/month firewall bill against a $216–288 uplift is
a loss dressed as a concession.
