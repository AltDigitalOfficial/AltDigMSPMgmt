# Deferred decisions

Decisions that were seen, deliberately not made, and parked against a trigger.
They are not backlog items, which are work that has been decided, and not
scale-watch items ([docs/scale-watch.md](docs/scale-watch.md)), which are
correct now and wrong past a threshold. Each entry here still needs somebody to
*choose*.

**Every entry has a trigger, not a date.** When the trigger fires, the decision
is made then, with the facts available then. An entry without a trigger is a
wish and does not belong here.

When a decision is made, mark the entry **Decided**, point at the record that
now holds it (an ADR, a D-record or a backlog item), and leave the entry in
place.

Format: `DD-NNN · title`, then **Trigger**, **Context**, **Options as seen**,
**Leaning** (if any) and **Owner**.

---

## DD-001 · Where product domains are registered

**Trigger:** the first product domain any tenant needs for uat or prod. For
Arc8, that is the Arc8 prod account existing (its quota case is open).

**Context:** ADR-015 leaves domain registration open to developers in dev, so
throwaway dev domains will be registered in dev accounts. A product domain
cannot follow them there. Whoever holds DeveloperDev would control prod's DNS,
and the domain would be bound to an account the platform treats as the least
protected. Registering it in a HIPAA-enforced account is also blocked:
`route53domains` is not on the HIPAA-eligible list, so SCP 09 denies it in uat
and prod. Route 53 hosted zones and records are eligible, so only the
registration has to live elsewhere.

**Options as seen:**
- The client's own registrar, which delegates environment subdomains to hosted
  zones in each account. No new AWS asset; the client keeps the domain if they
  leave.
- One registration account per tenant, outside Members' developer roles.
- The tenant's prod account, with an exception to SCP 09 for `route53domains`
  only.

**Leaning:** the client's registrar under service model A, and a
per-tenant registration account under B and C.
**Owner:** Jamie. Arc8's product domain name itself is Wayne's.

---

## DD-002 · Questionnaire section 7 (domains, DNS, certificates) is rebuilt

**Trigger:** the next customer onboarding, or the questionnaire's move to a
self-service webpage, whichever comes first.

**Context:** Arc8's answers show what section 7 lets through. 7.1 (domains)
was `unknown`. 7.2 (who holds the zone) was the client's name, while the client
still did not know who controlled its DNS. 7.3 was `altdigital-issues` under
service model A, where design 02 makes certificates the customer's. The form
accepted all three without comment.

**Options as seen:** replace 7.1–7.4 with:
- the apex domain, or "we need one";
- where it is registered and who can change its nameservers, as a pick list;
- the environment subdomains, derived (`dev.<apex>` and so on), not asked;
- the certificate issuer, derived from the service model, not asked;
- a validator that rejects contradictions (A with `altdigital-issues`) unless
  an override is stated.

**Owner:** whoever owns the questionnaire's move to self-service. The wording
test is the north star: could a partner answer it correctly on a webpage with
no AltDigital person present?

---

## DD-003 · Vesting creates a delegated hosted zone per environment

**Trigger:** the next customer whose apex domain is known at onboarding.

**Context:** Arc8's DNS was improvised: a developer went to register a domain
because nothing gave them a name. If the questionnaire knows the apex (DD-002),
vesting could create `<env>.<apex>` in each account and hand the delegation
records to whoever holds the parent.

**Options as seen:**
- Vesting creates the zone, tagged `platform-layer: application` because the
  application's records live in it. The tenant writes records.
- Vesting creates nothing; the developer guide says how to do it.
- Under model A, nothing. Under B and C, a platform-owned zone.

**Owner:** Jamie.

---

## DD-004 · DNS and ingress privileges by tier, above dev

**Trigger:** the first tenant that needs public DNS or a public entry point in
test or above. For Arc8, that is test.

**Context:** ADR-015 settles dev only: the team may do what it wants, within
the controls and visible in the digest. DeveloperTest is `ReadOnlyAccess` plus
restart, so DNS changes in test already need the deployment pipeline. Nobody
has decided whether the pipeline's role (`change/00-appdeployer.yaml`) may
register domains, create public zones or attach distributions.

**Options as seen:** record changes through the pipeline only; registration
never through any tenant role above dev; WAF on public entry points enforced
by the platform in prod (B-011), and reported only in test and uat.

**Owner:** Jamie.

---

## DD-005 · Dangling DNS records are detected

**Trigger:** the first public hosted zone in test or above, or the first
tenant whose dev zone is under a domain its customers know.

**Context:** a record that still points at a deleted load balancer, bucket or
distribution can be taken over by anyone who creates a resource with that name.
This is the classic subdomain takeover. It is most likely where developers
create and delete freely, which ADR-015 now allows in dev.

**Options as seen:** a Config or Guard check that resolves each alias and CNAME
target against the account's live resources; a digest line only; nothing until
a zone is under a customer-facing domain.

**Owner:** Jamie.

---

## DD-006 · Registrant contact for domains registered in tenant accounts

**Trigger:** the first `RegisterDomain` in any tenant account.

**Context:** the registrant contact receives renewal notices and the ICANN
verification email. If that email is not verified within 15 days, the registry
suspends the domain. When a developer registers a domain in dev, that contact
is the developer, personally. That is accepted for throwaways (ADR-015). For
anything that outlives the developer, the registrant should be the client
entity at a role mailbox.

**Options as seen:** a digest line showing the registrant contact's role
(not the address); a developer guide note; nothing.

**Owner:** Jamie.
