# ADR-015 — In dev, a tenant team may build anything that leaves the platform's controls intact; the platform reports what it built, and does not prevent or mitigate it

**Date:** 2026-10-28
**Deciders:** Jamie
**Status:** Accepted — partly built. The ruling is in force: nothing was
restricted. Condition (b) is half met. What is reachable from the internet is
in the manager digest from 2026-10-28 (B-176, ADR-016; Arc8 dev's dry run
showed it [verified 2026-10-28]). The team's activity is not yet (B-171
Stage B, blocked on `security/50-trail-query.yaml`, which is not deployed in
Log Archive [verified 2026-10-28]).
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

## Amendment — 2026-10-29

**The Context above is wrong about the registration.** The developer did not
stop short. CloudTrail in Arc8 dev shows `RegisterDomain` for `arc8-mvp.click`
on 2026-10-20 at 14:18 UTC and again on 2026-10-28 at 19:19 UTC. The first is
outside the 10-24 to 10-28 window that was checked. The second came after the
check ran [verified 2026-10-29]. Both calls were accepted and both operations
went to `FAILED` within one second with AWS's generic message, *"We can't
finish registering your domain. Contact AWS Support."* No domain is registered
and no hosted zone exists [verified 2026-10-29]. The failure is on AWS's side,
not the platform's: the API call was authorised, and nothing in DeveloperDev or
the SCPs refers to it. AWS gives no reason. A per-account hold on domain
registration is the usual cause and has not been confirmed. Dev has no paid
support plan, so only an account and billing case can resolve it.

**Ruling.** Jamie, 2026-10-29: the developer may register `arc8-mvp.click` in
Arc8 dev, as this ADR already permits. It is a **temporary** domain, to be used
until AltDigital controls OEight's DNS. Getting that control is in progress.
The platform does not take the registration over, and it does not open the
support case on the team's behalf.

**Alternative rejected:** register the domain centrally, in an account
AltDigital controls or with whoever ends up holding OEight's DNS, and delegate a
subdomain per environment (`dev.`, `test.`, later prod) to each account's
hosted zone. This was recommended in the session because it matches what the
developer asked for, one name per environment, and because nothing has to move
when prod arrives. It lost because the name is a throwaway that is replaced once
OEight's DNS is under control, so a central home for it would be built for a
domain with no future.

**Consequences added:**
- `arc8-mvp.click` will be owned by the dev account and by whatever registrant
  contact the developer enters, as the Negative consequences above already
  accept. If that contact does not verify within 15 days the domain is
  suspended, which for a temporary dev name is the team's problem, not the
  platform's.
- **"Temporary" is not enforced.** The registration auto-renews unless someone
  turns that off, and nothing removes the domain when OEight's DNS arrives.
  The revival trigger is AltDigital gaining control of OEight's DNS. At that
  point the team moves its records to the OEight-controlled name, and
  `arc8-mvp.click` is either left to lapse or kept deliberately.
- Once registered, the domain is covered by the daily expiry scan (ADR-016).
