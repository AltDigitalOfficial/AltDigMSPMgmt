# ADR-016 — Internet exposure and expiry are read inside each account by a dedicated role, and expiry is a daily scan, not ACM's own events

**Date:** 2026-10-28
**Deciders:** Claude (session), unreviewed. Jamie asked for the exposure
section and for expiry monitoring to be built ("let's do the exposure, and
then the trail"; "let's build that out"). The mechanism below is Claude's.
**Status:** Proposed
**Tags:** digest, exposure, expiry, cross-account-read, alert-routing

---

## Context

ADR-015 makes developer freedom in dev conditional on what they build showing
up in the daily manager digest. On 2026-10-28 Arc8 dev had three CloudFront
distributions with no web ACL, in front of an instance with SSH open to
`0.0.0.0/0`, and its digest mentioned none of it [verified 2026-10-28].

Design 02 makes certificate expiry monitoring AltDigital's under every service
model. Nothing implemented it.

Facts that constrained the mechanism, all verified 2026-10-28:

- **The Config aggregator does not hold everything needed.** It records
  CloudFront distributions, load balancers, instances and security groups.
  Route 53 Domains is not a Config resource type, and an application load
  balancer's configuration item does not say whether a web ACL is attached.
- **Event forwarding and instrumentation run in us-east-2 only.** In Arc8 dev,
  `platform-event-forwarding` and `platform-instrumentation` have stack
  instances in us-east-2 and nowhere else. A certificate CloudFront uses must
  live in us-east-1, so ACM's "Certificate Approaching Expiration" events for
  exactly the most public certificates would never reach the router.
- **Route 53 Domains emits no expiry event at all.** Its renewal notices go by
  email to the registrant contact.
- **The routing tier map includes AltDigital's own accounts** (Audit, Log
  Archive, Tooling and others), and the facts gatherer reads every account in
  it.

## Decision

1. **A new read role, `PlatformExposureReader`, in every account** of the
   Sandbox, Members, Security and Infrastructure OUs, as the StackSet
   `platform-exposure-reader` (`reporting/00-exposure-reader.yaml`). It holds
   List and Describe calls only. It trusts the Audit account, narrowed by
   `aws:PrincipalArn` to exactly `PlatformDigestFacts` and
   `PlatformExpiryWatch`. It deliberately omits
   `route53domains:GetDomainDetail`, which returns the registrant's personal
   contact details, and any Route 53 record read.
2. **One module, `reporting/src/exposure.py`, reads and judges.** It reads
   per section and per region, records a failed section instead of stopping,
   and keeps reading separate from judging (`assess`, `expiring`).
3. **The facts gatherer reads exposure daily** and writes it into each
   account's facts document. The digest renders a *Reachable from the
   internet* section every day, including when it is empty. Expiring
   certificates and domains go under *Needs your attention*.
4. **Expiry warnings come from a daily scan**, `platform-expiry-watch` in Audit
   (`reporting/30-expiry-watch.yaml`, `reporting/src/expiry.py`). It sends one
   dossier per item to the alert bus as `expiry-warning`, under a stable name,
   and declares a ceiling the router may only lower: `digest` in dev,
   `digest_and_jira` in test and uat, and in prod `pagerduty` within 14 days,
   else `digest_and_jira`. The router gains a normaliser entry and nothing
   else.
5. **Thresholds:**
   - Certificates warn at 45 days, of every type.
   - Domains with auto-renew on warn at 21 days; with it off, at 45.
   - Anything within 14 days is urgent.
6. **No layer is stated** on an expiry warning. The router therefore routes it
   as infrastructure, to AltDigital, because design 02 makes detection ours.
7. **A section denied by an SCP counts as checked.** If the service cannot be
   used in that account, nothing of that kind can exist there. SCP 09 denies
   Route 53 Domains in HIPAA-enforced accounts, for instance.

## Alternatives considered

- **Read everything from the Config aggregator in Audit, with no
  cross-account role.** Rejected. It cannot see registered domains or web ACL
  associations on load balancers, which are two of the facts ADR-015 needs
  in front of a manager.
- **Widen `PlatformDigestReader` or `PlatformTelemetryReader`.** Rejected, on
  the precedent `telemetry/00-telemetry-reader.yaml` records. Each
  cross-account reader stays minimal and is named for what it reads, so its
  name keeps describing it.
- **Use ACM's native expiry events, by extending event forwarding (and
  instrumentation) to us-east-1.** Rejected for now. It means two new StackSet
  regions for every account. Domains would still need a scan, so the platform
  would have two expiry mechanisms with two sets of thresholds. It would also
  lose "Renewal Action Required" no sooner than the scan finds a managed
  certificate inside 45 days.
- **A 06a metric alarm on ACM's `DaysToExpiry`, one per certificate.**
  Rejected. Instrumentation is us-east-2 only, which has the same region
  problem. It would still need a scan for domains. And per-certificate alarms
  multiply where one daily read suffices.
- **Emit expiry warnings from the facts gatherer.** Rejected. The gatherer is
  a reader whose failure degrades an email. An emitter's failure has to alarm
  in its own right. Coupling the two would make a facts outage silence expiry
  warnings for prod.
- **Gather exposure in the digest in Tooling.** Rejected. The digest is not an
  emitter either, and putting the read beside the facts keeps one gatherer per
  email (ADR-004).
- **Deploy the role to Members only.** Rejected. The gatherer reads every
  account in the tier map, so eight shared accounts would fail every day, and
  the incomplete-run alarm would fire permanently on a deployment choice.
- **Give expiry warnings the application layer under service model A.**
  Rejected. Design 02 separates detection, which is AltDigital's everywhere,
  from remediation, which follows the model. The party detecting is the party
  told first.

## Rationale

ADR-015's condition (b) is only as good as what the digest can see, and the
platform cannot see inside an account from Audit alone. A narrow, named,
list-only role is the smallest grant that answers the question, and it follows
the pattern the telemetry scan already established. The single judgment module
means the email and the pager cannot disagree about what is expiring. Routing
through the router with a declared ceiling keeps the hard rule: an emitter may
downgrade its own event and never escalate it.

## Consequences

**Positive:**
- Arc8 dev's distributions, open SSH port and any domain registration attempt,
  including a failed one with the registrar's message, appear in the manager
  digest daily.
- Expiry monitoring exists for certificates in every allowed region and for
  domains.
- AltDigital's own accounts are read too, which the platform digest (B-172)
  can use.

**Negative:**
- **Reads are sequential.** About thirty list calls per account across three
  regions. Fine at nine accounts, but the facts function's 900-second ceiling
  is reached somewhere past sixty, and the read then has to fan out. The
  trigger is a facts run taking more than five minutes.
- **The same facts are read twice a day,** once by the gatherer and once by
  the scan. It costs a few hundred list calls a day, and it keeps the two
  failure paths separate.
- **What it does not check is listed, not covered:** Lambda function URLs,
  public S3 buckets and websites, ECS tasks with public IPs, App Runner,
  Elastic Beanstalk, Lightsail and Global Accelerator. The digest says so on
  every run.
- **The 21-day domain threshold is an inference.** It assumes Route 53 has
  renewed an auto-renewing domain by then. AWS does not document that point
  for every TLD.
- **A tenant's expiring prod certificate pages AltDigital, not the tenant.**
  That is deliberate (detection is ours), and it means AltDigital relays it.
- **In dev, an expiry warning routed `digest` is also enqueued to the review
  queue,** like every digest-class event, until B-051 is decided.
- **"Who made it" is still missing** from exposure lines until Stage B of the
  manager digest reads the org trail (B-171).

**Enforcement:**
- `platform-digest-facts-incomplete` fires when any account's exposure could
  not be read.
- `platform-expiry-watch-incomplete` fires when the scan did not read every
  account in full.
- `scripts/test-exposure.py` holds Arc8 dev's 2026-10-28 surface as a
  fixture, along with the thresholds and the router's handling.

## Not decided here

- Whether the platform digest (B-172) shows exposure counts across the estate.
  The estate document carries them now.
- Whether a web ACL becomes a platform requirement in prod (B-011, DD-004).
- Native ACM events in us-east-1, if event forwarding ever goes multi-region
  for another reason.

## Related

ADR-015, ADR-003, ADR-004; design 02 *DNS and certificates*, *Ingress*;
B-176, B-171, B-172, B-011, B-051; `deferred_decisions.md` DD-004, DD-005;
`telemetry/00-telemetry-reader.yaml` (the role precedent).
