# ADR-015 — In dev, a tenant team may build anything that leaves the platform's controls intact; the platform reports what it built, and does not prevent or mitigate it

**Date:** 2026-10-28
**Deciders:** Jamie
**Status:** Accepted — not built. The ruling is in force: nothing was
restricted. What is pending is condition (b): the manager digest does not yet
report either the team's activity (B-171 Stage B, blocked on
`security/50-trail-query.yaml`, which is not deployed in Log Archive
[verified 2026-10-28]) or what is reachable from the internet.
**Tags:** developer-access, digest, dns, ingress, tenant-responsibility

---

## Context

On 2026-10-26 an Arc8 developer, signed in through DeveloperDev in Arc8 dev,
opened Route 53 Domains, priced the domain `arc8-mvp.click` and checked whether
it was available. They stopped short of registering it. CloudTrail shows
`ListDomains`, `ListOperations`, `ListPrices`, `CheckDomainAvailability` and
`GetDomainSuggestions`, all successful, and no `RegisterDomain`,
`CreateHostedZone` or `ChangeResourceRecordSets` in dev or test between
2026-10-24 and 2026-10-28 [verified 2026-10-28].

**Nothing would have stopped the registration.** DeveloperDev is
`PowerUserAccess` plus an inline policy (`identity/10-developer-access.yaml`),
which allows all of `route53domains:*` and `route53:*`. No SCP attached to
Members restricts either service, and `06-region-restriction.json` exempts both
because they are global.

The account was already public. It had three CloudFront distributions, created
through DeveloperDev on 2026-10-21 and 2026-10-26. Each uses the default
certificate and has no web ACL. Their origin is one EC2 instance, addressed by
its public DNS name, which sits in `platform-public-a` behind an internet
gateway. The instance's web ports admit only the CloudFront origin-facing prefix
list, but port 22 is open to `0.0.0.0/0` [verified 2026-10-28]. The domain was
wanted, in part, so the team could stop using the random `*.cloudfront.net`
names.

The obvious response was to close the gap: deny domain registration to every
developer role, and make public ingress in dev wait for B-011's WAF scaffolding.

## Decision

Jamie, 2026-10-28:

1. **In dev, the tenant's team may do what they want, under two conditions:**
   (a) it does not disable or weaken an AltDigital platform control, and
   (b) it appears in the daily manager digest.
2. **The registration gap stays open.** Domain registration, hosted zones,
   public records and public entry points remain available to DeveloperDev.
3. **Public-access risk in dev is the tenant's to mitigate.** The platform's
   job is to put the risk in front of the tenant's development manager. It
   does not enforce a mitigation, and it does not decide which mitigation is
   enough. The dev manager owns making the team do something about it.

Condition (a) is what the existing protective SCPs already hold (01–03 v2, 07,
08 on Members). Condition (b) is what this ADR makes owed: a dev permission
that the digest cannot report is a gap against this decision, even when no
control is weakened.

## Alternatives considered

- **Deny `route53domains` register, transfer and delete actions to every
  `Developer*` permission set.** Recommended in the session. Rejected by Jamie.
  It treats one action as uniquely dangerous in an account where PowerUser can
  already open the account to the internet in several other ways. It also puts
  AltDigital in the position of judging each tenant's development choices, which
  does not scale across tenants.
- **Make WAF (or B-011's full ingress scaffolding) a precondition for any public
  entry point in dev.** Rejected for the same reason. An AltDigital operator
  cannot hold an opinion about every tenant's dev exposure. The tenant's dev
  manager can, for one tenant.
- **Platform-owned DNS: AltDigital creates and controls every hosted zone, and
  developers request records.** Rejected for Arc8, which is service model A
  (customer owns DNS and certificates, design 02). Whether vesting should create
  per-environment delegated zones for the next customer is deferred
  (`deferred_decisions.md`).
- **Leave it as it is and say nothing.** Rejected. Without condition (b),
  "the team may do what they want" becomes "nobody knows what the team did".
  That breaks the dev manager's ability to mitigate, which is the whole of (3).

## Rationale

The platform's controls protect the platform: detective controls, evidence,
identity, monitoring. Those are not negotiable in any tier, which is condition
(a). What a tenant builds in its own dev account is the tenant's risk to carry,
and the person who can weigh it is the tenant's development manager. The
platform's contribution is visibility, delivered to that person every day, which
is condition (b). It matches design 11's manager digest and ADR-003's split of
audiences.

This also fits the hard rule that an emitter may downgrade its own event and
never escalate one. A public entry point in dev is digest-class, because the
router's tier routing says dev goes to the digest. No component decides on its
own that the exposure deserves a page.

## Consequences

**Positive:**
- Developers are not blocked in dev, and no new denial is added to the
  guardrail friction the digest already has to explain.
- Responsibility for dev exposure sits with the party who can act on it.

**Negative:**
- **Until (b) is built, the ruling is enforced by nobody.** Today the digest
  shows alarms, instrumentation exceptions, missing layer tags and router
  findings. It does not show a domain registered, a distribution created or a
  port opened to the world. Arc8 dev is public now, and its digest does not say
  so.
- **A domain registered in a dev account is owned by that account.** It
  auto-renews, bills to the organization's payer, and names as registrant
  whoever typed the contact form. Registrar renewal and ICANN verification
  emails go to that person. If the address is not verified within 15 days, the
  registry suspends the domain. All of that is accepted for dev throwaways.
  Where product domains are registered is deferred.
- **Public exposure in dev is safe for PHI only if dev holds no PHI.** Keeping
  PHI out of dev is a tenant obligation. The platform does not verify it.

**Enforcement:**
- (a): the protective SCPs and the guardrail harness (`scripts/test-guardrails.sh`).
- (b): pending. Stage B of the manager digest (B-171) for activity, and a
  state-based "reachable from the internet" section for exposure. Neither is
  built.
- (3): manual. The dev manager reads the digest.

## Not decided here

- **Test, uat and prod.** This ADR covers dev only. In test and above, change
  goes through the deployment pipeline (design 08), and whether the platform
  enforces ingress protection in prod (B-011) is open.
- **Where product domains are registered.** Arc8's product domain is deferred
  to prod. The general rule for the next customer is in `deferred_decisions.md`.
- **Whether the team's activity in dev must be itemised, or only counted**
  (design §7.2 caps).

## Related

- B-011 (public ingress scaffolding), B-171 (manager digest; Stage B), B-155
- ADR-003 (two digests), ADR-004 (facts gathered in Audit from the org trail),
  ADR-008 (org trail through Athena)
- design 02 *DNS and certificates*, design 11 *Manager digest*,
  `docs/digest-design.md` §3 and §7
- `deferred_decisions.md`
