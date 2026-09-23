# Client Onboarding Questionnaire

**Completed jointly by AltDigital and the client.** Most of it is a
conversation rather than a form — several questions are ones we expect to work
out together, and a few are ones nobody can answer until the application has
been running for a while.

You do not need to answer everything to get started. See
[Answering "we don't know yet"](#answering-we-dont-know-yet).

---

## Before you start

### Where the completed copy lives

**Do not commit a completed copy of this form to a source repository.** It will
contain names, email addresses and phone numbers.

Completed questionnaires are held by AltDigital outside the platform
repository. The contact details are transferred into two access-controlled
files (`config/contacts.env` and `config/identity.env`) and nowhere else.

### What this produces

Your answers become a **tenant manifest** — a single machine-readable record of
what your environment is and why. That manifest drives the AWS build directly:
which accounts are created, which security policies apply, how long evidence is
retained, when non-production environments are switched off.

The value of that is auditability in both directions. When somebody asks *why
does this account have this backup schedule*, the answer is a line in your
manifest rather than somebody's memory.

### Answering "we don't know yet"

Every question is marked with **when we need it**:

| Marker | Meaning |
|---|---|
| **Vest** | Needed before we can create your AWS accounts. Mostly irreversible things. |
| **Dev** | Needed before your development environment is usable. |
| **Prod** | Needed before production. You can defer it now. |
| **Optional** | Useful context. Nothing breaks if it is left blank. |

And with **whether it can change later**:

| Marker | Meaning |
|---|---|
| **Locked** | Fixed when your accounts are created. Changing it later means rebuilding. |
| **Reversible** | Can be changed with a configuration change and a redeploy. |

Anything marked **Prod** or **Optional** can be answered *"deferred"*. Record it
in [Appendix A — Deferral register](#appendix-a--deferral-register) with what
needs to happen before you can answer. A deferral with a trigger is a plan. A
blank is a surprise later.

**The questions you genuinely cannot defer are the irreversible ones** — Section
1 and Section 4. Those decide your account names and your evidence retention
period, and retention in particular becomes a legal commitment nobody can undo,
including AWS. Please read Section 4 carefully.

### How to answer

| Notation | Meaning |
|---|---|
| **Pick one** | Choose exactly one of the listed values |
| **Pick any** | Choose zero or more of the listed values |
| **Yes / No** | |
| **Number** | |
| **Free text** | Prose. Detail helps; we would rather have a paragraph than a word |
| **List** | One entry per line |

---

## 1. Identity and ownership

Everything here is **Locked**. These values become AWS account names and root
mailbox addresses, which cannot be changed once an account exists.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 1.1 | Partner this client sits under | Pick one — AltDigital will supply the list | Vest | Locked |
| 1.2 | Client organisation name | Free text | Vest | Locked |
| 1.3 | Client short code | Free text, max 18 chars, lowercase letters/numbers/hyphens | Vest | Locked |
| 1.4 | Application name | Free text | Vest | Reversible |
| 1.5 | Application short code | Free text, max 10 chars, lowercase letters/numbers/hyphens | Vest | Locked |
| 1.6 | Does the partner own the application IP? | Yes / No | Vest | Reversible |
| 1.7 | Service model | **Pick one:** `A` / `B` / `C` | Vest | Reversible |

**On 1.7 — the service model decides who patches what.** It is the single
biggest determinant of both price and of who is accountable when something is
out of date.

| | **A — Infrastructure** | **B — Managed Platform** | **C — Fully Managed** |
|---|---|---|---|
| AWS account, network, encryption, backup | AltDigital | AltDigital | AltDigital |
| Detective controls, logging, evidence | AltDigital | AltDigital | AltDigital |
| OS patching | **You** | AltDigital | AltDigital |
| Container runtime / base images | **You** | AltDigital | AltDigital |
| Middleware, runtime | **You** | **You** | AltDigital |
| Application code and dependencies | **You** | **You** | AltDigital |
| DNS, certificates | **You** | AltDigital | AltDigital |
| SLA scope | Infra + exclusions | Infra + exclusions | End-to-end |
| Relative price | Lowest | Standard | Highest |

Most clients want **B**. **C** only works where AltDigital built the
application. **A** gives you the most control and carries the most audit risk,
because you own patching but we still have to evidence it.

---

## 2. The application

This section is about the software itself. Answer for **where it is now** and,
where they differ, **where you want it to be**. Both are useful — the gap
between them is usually the interesting part.

Nothing here is Locked. It shapes what we build and how we monitor it.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 2.1 | What does the application do? | Free text — a paragraph, in plain language | Dev | Reversible |
| 2.2 | Who uses it? | Free text — internal staff, customers, partners, machines | Dev | Reversible |
| 2.3 | Current state | **Pick one:** `greenfield` / `migrating-from-elsewhere` / `already-on-aws` / `rewrite-in-progress` | Vest | Reversible |
| 2.4 | If migrating, from where? | Free text — on-prem, another cloud, another AWS account | Dev | Reversible |
| 2.5 | Primary language and framework | Free text | Dev | Reversible |
| 2.6 | How is it packaged? | **Pick any:** `containers` / `vm-images` / `serverless-functions` / `static-site` / `installed-on-os` / `not-yet-decided` | Dev | Reversible |
| 2.7 | AWS compute you expect to need | **Pick any:** `ecs-fargate` / `ecs-ec2` / `eks` / `ec2` / `lambda` / `batch` / `not-yet-decided` | Dev | Reversible |
| 2.8 | Data stores you expect to need | **Pick any:** `rds-postgres` / `rds-sqlserver` / `rds-mysql` / `aurora` / `dynamodb` / `elasticache` / `opensearch` / `s3-only` / `not-yet-decided` | Dev | Reversible |
| 2.9 | Roughly how much data, at production scale? | Free text — GB or TB, and growth rate if known | Prod | Reversible |
| 2.10 | Expected load at production scale | Free text — users, requests/second, batch volumes, whatever fits | Prod | Reversible |
| 2.11 | Is load steady or spiky? | **Pick one:** `steady` / `business-hours` / `seasonal-peaks` / `unpredictable` | Prod | Reversible |
| 2.12 | Outbound dependencies | List — external APIs, SaaS, payment providers, anything the app calls out to | Dev | Reversible |
| 2.13 | Inbound integrations | List — who or what calls this application | Dev | Reversible |
| 2.14 | How do users authenticate? | **Pick one:** `no-auth` / `own-user-database` / `corporate-sso` / `social-login` / `machine-to-machine-only` / `not-yet-decided` | Dev | Reversible |
| 2.15 | If SSO, which identity provider? | Free text | Dev | Reversible |
| 2.16 | Is there existing data to migrate? | Yes / No | Dev | Reversible |
| 2.17 | If yes, roughly how much, and from where? | Free text | Dev | Reversible |
| 2.18 | Known failure modes | Free text — how does it break today, what wakes people up | Prod | Reversible |
| 2.19 | Existing runbooks or operating procedures | Yes / No — and please share them if so | Prod | Reversible |

**On 2.12 — outbound dependencies matter more than they look.** Your network
egress profile (Section 6) is derived partly from how your data is classified,
and a restrictive profile will block calls to services nobody mentioned. Listing
them now avoids a dev environment that looks broken for a reason that takes a
day to find.

---

## 3. People and access

We need to know who gets access to what, and who hears about it.

**These answers contain personal data.** They are transferred into
access-controlled configuration and are not stored with the rest of the
questionnaire.

### 3a. Named roles

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 3.1 | Technical contact | Name, email, phone | Vest | Reversible |
| 3.2 | Escalation contact — who we call out of hours | Name, email, phone | Prod | Reversible |
| 3.3 | Change approver — who authorises changes on your side | Name, email | Dev | Reversible |
| 3.4 | Billing contact | Name, email | Vest | Reversible |
| 3.5 | Security contact — who we notify of a security incident | Name, email, phone | Prod | Reversible |
| 3.6 | **Development manager** — receives the daily activity digest | Name, email | Dev | Reversible |

**On 3.6.** The development manager receives a daily summary of what happened in
your environments: deployments, alarms, changes to infrastructure, anything that
needed attention. It is one email per day, per environment group. It is the main
way a manager sees platform activity without needing an AWS login, and we would
rather it went to a person who will read it than to a shared mailbox nobody
watches.

Tell us if you want more than one recipient, or a different recipient per tier.

### 3b. Developer roster

Each person who needs AWS access needs their own identity. **We do not issue
shared logins, and we do not issue long-lived access keys to people.** Access is
through single sign-on with multi-factor authentication.

One row per person:

| Name | Email | Dev | Test | UAT | Prod | Notes |
|---|---|---|---|---|---|---|
| | | ☐ | ☐ | ☐ | ☐ | |
| | | ☐ | ☐ | ☐ | ☐ | |
| | | ☐ | ☐ | ☐ | ☐ | |

**When:** Dev · **Change later?** Reversible — people are added and removed
routinely.

Access levels differ by environment. Development access is broad; production
access is narrower and read-heavy by default, with changes going through the
deployment pipeline rather than the console. If somebody needs an exception,
tell us who and why rather than working around it.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 3.7 | Will any of your staff need direct AWS console access, as opposed to pipeline-only? | Yes / No | Dev | Reversible |
| 3.8 | Who may authorise turning a non-production environment on outside its schedule? | List — names or a named role | Dev | Reversible |
| 3.9 | Do you have a leaver process we should be told about? | Free text | Prod | Reversible |

**On 3.8.** Non-production environments switch off outside working hours to
control cost (Section 5). Somebody occasionally needs one back on at 9pm. We
need to know whose request counts as authorisation.

---

## 4. Compliance and data

**Read this section before answering it.** Two of these answers are effectively
permanent.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 4.1 | Which compliance frameworks apply? | **Pick any:** `hipaa` / `pci-dss` / `soc2` / `other` / `none` | Vest | **Locked** |
| 4.2 | If `other`, which? | Free text | Vest | Locked |
| 4.3 | Highest data classification the application handles | **Pick one:** `public` / `internal` / `confidential` / `regulated` | Vest | **Locked** |
| 4.4 | Does it handle protected health information (PHI)? | Yes / No | Vest | **Locked** |
| 4.5 | Does it handle cardholder data? | Yes / No | Vest | **Locked** |
| 4.6 | Are any data subjects outside the United States? | Yes / No | Vest | Reversible |
| 4.7 | If PHI: how long do you have to notify **your** customer of a breach? | Number, in hours | Prod | Reversible |
| 4.8 | What personal data does the application hold, and about whom? | Free text | Dev | Reversible |
| 4.9 | Who is your auditor, and when is your next audit? | Free text | Optional | Reversible |

### Why 4.1 and 4.3 cannot be changed later

Your compliance frameworks determine how long we must retain evidence — logs,
configuration history, access records. That retention is enforced using AWS
Object Lock in **compliance mode**, which means:

> For the full retention period, **nobody can delete those records**. Not you,
> not AltDigital, not AWS Support, not the account root user. There is no
> override, no appeal and no early release.

Current retention floors:

| Framework | Retention | Why |
|---|---|---|
| `hipaa` | **2,190 days** (6 years) | 45 CFR 164.316(b)(2) |
| `pci-dss` | 365 days | Requirement 10.7 |
| `soc2` | 365 days | Covers a typical audit period |

The highest applicable floor wins. It is also a storage cost commitment for the
same period, which you cannot reduce by deleting data.

**So do not select a framework defensively.** "We might need HIPAA one day" is a
six-year, irrevocable decision if it is answered yes today. If you are genuinely
unsure, say so — it is better to delay vesting by a week than to be wrong about
this.

**4.4 and 4.5 must agree with 4.1.** If you handle PHI, `hipaa` must be in your
framework list; if you handle cardholder data, `pci-dss` must be. The platform
refuses to build an environment where those disagree, because the alternative is
an environment that looks compliant and is not.

---

## 5. Environments and scheduling

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 5.1 | Do you need a UAT environment? | Yes / No | Vest | **Locked** |
| 5.2 | Primary timezone for scheduling | Free text — e.g. `America/Chicago` | Vest | Reversible |
| 5.3 | Release cadence | **Pick one:** `continuous` / `monthly` / `quarterly` / `ad-hoc` | Dev | Reversible |
| 5.4 | Usage pattern | **Pick one:** `year-round` / `seasonal` | Optional | Reversible |

### 5a. Operating hours per environment

Non-production environments are switched off outside their operating hours.
This is usually the largest single cost saving available, and it is yours, not
ours — you are not billed for compute that is switched off.

For each environment, either mark **always on** or give days and hours:

| Environment | Always on? | Days | Start | End |
|---|---|---|---|---|
| Development | ☐ | | | |
| Test | ☐ | | | |
| UAT (if used) | ☐ | | | |

**Production is always on and is not schedulable.**

**When:** Vest · **Change later?** Reversible

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 5.5 | Any freeze periods when changes must not happen? | Free text — e.g. retail peak, year-end close | Prod | Reversible |
| 5.6 | Preferred maintenance window | Free text — day and time range | Prod | Reversible |

---

## 6. Network and connectivity

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 6.1 | Does the application need inbound internet, outbound, both or neither? | **Pick one:** `inbound` / `outbound` / `both` / `neither` | Dev | Reversible |
| 6.2 | Which outbound destinations does it need? | List — hostnames or services | Dev | Reversible |
| 6.3 | Does it need to call an API belonging to another client of ours? | Yes / No | Dev | Reversible |
| 6.4 | Do you need connectivity back to your own network? | **Pick one:** `none` / `vpn` / `direct-connect` / `not-yet-decided` | Dev | Reversible |
| 6.5 | Do you have IP allow-listing requirements? | Free text | Prod | Reversible |
| 6.6 | Egress profile | **Pick one:** `isolated` / `controlled` / `dns-filtered` / `public-facing` — AltDigital will propose one | Dev | Reversible |

**On 6.6.** We derive a default from your data classification and will propose
it. A more restrictive profile is cheaper and safer; the cost of getting it
wrong is outbound calls failing in ways that look like application bugs. This is
why 6.2 matters.

---

## 7. Domains, DNS and certificates

Ownership follows your service model (1.7). Under Model A these are yours; under
B and C they are ours. **Expiry monitoring is AltDigital's under all three
models** — an expired certificate is an outage regardless of who renewed it.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 7.1 | What domain names will this application use? | List | Dev | Reversible |
| 7.2 | Who currently holds the DNS zone? | Free text | Dev | Reversible |
| 7.3 | Do you need certificates issued, or do you have your own? | **Pick one:** `altdigital-issues` / `we-provide` / `not-yet-decided` | Dev | Reversible |
| 7.4 | Any wildcard or extended-validation requirements? | Free text | Optional | Reversible |

---

## 8. Resilience and recovery

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 8.1 | Recovery time objective — how long may production be down? | Number, in hours | Prod | Reversible |
| 8.2 | Recovery point objective — how much data may be lost? | Number, in hours | Prod | Reversible |
| 8.3 | SLA tier | **Pick one:** `standard` / `multi-region-premium` | Prod | Reversible |
| 8.4 | Backup retention — how long should backups be kept? | Number, in days. Default is 35 | Prod | Reversible |
| 8.5 | Are there specific datasets with different recovery needs? | Free text | Prod | Reversible |
| 8.6 | Have you ever tested a restore of this application? | Yes / No — and what happened | Optional | Reversible |

**On 8.1 and 8.2.** These are commitments we design against, so please give the
number the business would actually accept rather than an aspirational one. An
RPO of zero is achievable and expensive; an RPO of four hours is usually honest.
These are safe to defer until production — a development environment does not
have an RPO.

**8.4 is separate from 4.1.** Backups are operational recovery and can be
shortened or lengthened. Evidence retention under Section 4 cannot.

---

## 9. Security

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 9.1 | Do you need a dedicated encryption key for this application? | Yes / No | Vest | Reversible |
| 9.2 | Do you have FIPS 140-2 or CloudHSM requirements? | Yes / No | Vest | **Locked** |
| 9.3 | Do you need to supply your own key material? | Yes / No | Vest | **Locked** |
| 9.4 | Are you migrating existing secrets or credentials in? | Yes / No | Dev | Reversible |
| 9.5 | Will your staff need direct access to production data? | Yes / No | Prod | Reversible |
| 9.6 | Do you require dedicated (single-tenant) infrastructure? | Yes / No | Vest | **Locked** |
| 9.7 | If yes to 9.6, what drives it? | **Pick one:** `contract-clause` / `framework-control` / `procurement-policy` | Vest | Reversible |
| 9.8 | If yes to 9.6, which clause or control? | Free text | Vest | Reversible |
| 9.9 | Do you accept that regulated data must not be placed in development, test or UAT environments? | Yes / No — this is an acknowledgement, not a preference | Vest | — |

**On 9.9.** Non-production environments have lighter controls, shorter
retention, and are switched off on a schedule. Putting real regulated data in
them is the most common way a compliant platform becomes non-compliant. If you
need production-like data for testing, tell us — masking and synthetic data are
solvable, quietly copying the production database is not.

---

## 10. Licensing

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 10.1 | Do you have existing licences to bring? | **Pick any:** `sql-server` / `windows-server` / `other` / `none` | Prod | Reversible |
| 10.2 | Can you provide entitlement evidence? | Yes / No | Prod | Reversible |
| 10.3 | Microsoft licensing purchased through | **Pick one:** `altdigital` / `customer` / `not-applicable` | Prod | Reversible |

Bring-your-own-licence on AWS has specific eligibility rules and usually
requires dedicated hosts, which changes the cost model. Worth raising early if
it applies.

---

## 11. Monitoring and service definition

**This is the most valuable section of the conversation and the hardest to
answer quickly.** It is also entirely deferrable — a development environment
does not need it.

We monitor whether your application is *working*, not merely whether the servers
are running. That requires you to tell us what working means.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 11.1 | In one sentence, what does "the application is working" mean to a user? | Free text | Prod | Reversible |
| 11.2 | Which URLs or endpoints should we check? | List | Prod | Reversible |
| 11.3 | For each, what does a successful response look like? | Free text — status code, expected content, expected behaviour | Prod | Reversible |
| 11.4 | What response time is unacceptable? | Number, in milliseconds | Prod | Reversible |
| 11.5 | Does checking the critical journey require logging in? | Yes / No | Prod | Reversible |
| 11.6 | If yes, can you provide a dedicated test account? | Yes / No | Prod | Reversible |
| 11.7 | Do you have an existing automated test suite we could reuse? | **Pick one:** `playwright` / `other` / `none` | Prod | Reversible |
| 11.8 | Does the application emit structured logs? | Yes / No | Dev | Reversible |
| 11.9 | Does it emit custom metrics or traces? | Free text | Dev | Reversible |
| 11.10 | Which business events would you want alerting on? | Free text — e.g. "no orders processed in an hour" | Prod | Reversible |

**On 11.10.** Infrastructure monitoring tells you a server is unhealthy.
Business-event monitoring tells you the application stopped doing its job while
every server looked fine. The second is usually what the business actually cares
about, and it is the thing we can only build if you tell us what to watch.

---

## 12. Change management

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 12.1 | What CI/CD tooling do you use, or plan to? | Free text | Dev | Reversible |
| 12.2 | Expected deployment frequency | **Pick one:** `multiple-daily` / `daily` / `weekly` / `monthly` / `rarely` | Dev | Reversible |
| 12.3 | Do you have an existing change process we should align to? | Free text | Prod | Reversible |
| 12.4 | Which Jira project should hold change records for this application? | Free text — or ask us to create one | Dev | Reversible |
| 12.5 | Who on your side approves production changes? | Name, email | Prod | Reversible |
| 12.6 | Do you require a business test plan before production changes? | Yes / No | Prod | Reversible |

**On 12.5.** Production change approval requires two different people — the
person requesting and the person approving cannot be the same individual, even
if they work for the same organisation. This is a control we enforce rather than
a preference, so please nominate someone distinct from whoever will be raising
the changes.

---

## 13. Cost

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 13.1 | Do you have a monthly budget expectation for this application? | Free text | Optional | Reversible |
| 13.2 | At what monthly spend would you want to be alerted? | Number, in dollars | Dev | Reversible |
| 13.3 | Who should receive cost alerts and reports? | Name, email | Dev | Reversible |
| 13.4 | Are you willing to trade availability for cost in non-production? | Yes / No | Dev | Reversible |

**On 13.2.** Cloud cost failures are usually sudden rather than gradual — a
misconfigured loop or a runaway process can multiply a bill in a day. A
threshold lets us catch it while it is still small.

---

## 14. Support and escalation

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 14.1 | What are your business hours, and in which timezone? | Free text | Prod | Reversible |
| 14.2 | Does this application need out-of-hours support? | Yes / No | Prod | Reversible |
| 14.3 | How should your team raise an issue with us? | **Pick one:** `email` / `jira-portal` / `teams` / `phone` | Dev | Reversible |
| 14.4 | Who is authorised to declare an incident on your side? | List | Prod | Reversible |

---

## 15. Exit

Unglamorous, and much easier to agree now than during a separation.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 15.1 | Do you acknowledge that AltDigital's platform tooling and IP remain AltDigital's on exit, while your application and data remain yours? | Yes / No | Vest | — |
| 15.2 | Do you acknowledge that governance capability — evidence collection, monitoring, automated controls — ends when the engagement ends? | Yes / No | Vest | — |
| 15.3 | Do you want compliance evidence retained and exportable after exit? | Yes / No | Prod | Reversible |
| 15.4 | In what format would you want your data returned? | Free text | Optional | Reversible |

---

## 16. Truveon — your compliance workspace

Truveon is where your compliance evidence lands: control results, security
findings, change records, and the platform's own proof that nothing was
dropped on the way. It is also where your compliance team works — frameworks,
risks, policies, audits.

**One Truveon workspace per organisation**, not per application. If you bring us
a second application later, it joins the workspace you already have. If an
application's intellectual property belongs to a different organisation — you
build it, they own it — that organisation is the client for it and has its own
workspace.

| # | Question | How to answer | When | Change later? |
|---|---|---|---|---|
| 16.1 | Do you already have a Truveon workspace? | Yes / No | Vest | — |
| 16.2 | What should the workspace be called? | A display name. We choose the short identifier with you; it cannot be changed once created. | Vest | Name reversible; identifier **permanent** |
| 16.3 | What is your legal structure? | Each legal entity in scope: legal name, jurisdiction, and whether it is a parent, subsidiary, branch or joint venture. Company facts only. | Dev | Reversible |
| 16.4 | Should people receive their Truveon invitation straight away, or during a walkthrough with us? | **Pick one:** `now` / `walkthrough` | Vest | — |
| 16.5 | Who needs Truveon access? | Roster below | Vest | Reversible |

**On 16.2.** The identifier becomes part of how your workspace is stored and
cannot be renamed afterwards. It is usually your organisation's short name.

**On 16.4.** We recommend the walkthrough. A login that arrives before anybody
has shown you what the workspace is for tends to be ignored.

### 16.5 Truveon roster

**This contains personal data** and is handled the same way as Section 3b.

These are usually different people from your developers: whoever owns
compliance, an executive sponsor, sometimes your auditor. **Exactly one person
is the owner** — Truveon's first administrator, who can then invite others. If
you already have a workspace (16.1), you already have an owner and this list
is the people to add.

| Name | Email | Title | Owner? | Access |
|---|---|---|---|---|
| | | | ☐ | ☐ administrator ☐ contributor ☐ viewer |
| | | | ☐ | ☐ administrator ☐ contributor ☐ viewer |
| | | | ☐ | ☐ administrator ☐ contributor ☐ viewer |

**When:** Vest · **Change later?** Reversible — people are added and removed
from within Truveon.

---

## Appendix A — Deferral register

Anything left open goes here. **A deferral with a trigger is a plan; a blank is
a surprise.**

| Question # | What we could not answer yet | What has to happen first | Who owns it | Needed by |
|---|---|---|---|---|
| | | | | |
| | | | | |
| | | | | |

---

## Appendix B — What happens next

1. **We derive your manifest.** Your answers produce a single record of what
   your environment is. We review the derived values with you before anything is
   created — including the irreversible ones, spelled out, so there is no
   surprise.

2. **We set up your Truveon workspace** (Section 16), before any account
   exists, so that evidence has somewhere to go from the first minute.

3. **We create your AWS accounts.** One per environment. This is the
   irreversible step: an AWS account cannot be deleted, only closed, and the
   root email address is consumed permanently.

4. **We apply the baseline.** Logging, encryption, detective controls, network,
   backup, monitoring. Identical in every account, derived from your manifest
   where your answers differ from the default.

5. **We grant access.** Your developers get single sign-on to the environments
   listed in Section 3b.

6. **Development begins.** Sections marked **Prod** can still be open at this
   point.

7. **Before production**, we close out the deferral register, verify the
   environment against your manifest, and confirm — with you — that it is ready.

---

## Appendix C — Questions for us

Things we would rather you asked now than discovered later.

- What is the SLA, and what is excluded from it?
- What happens if you miss it?
- Who can see our data, and how is that limited?
- How do we get our data out, and how fast?
- What does this cost, and how is it billed?
- What happens if we want to leave?
- Who is on call, and how quickly do they respond?
- How do you prove any of this to our auditor?

**Answer:** ask them. We would rather have the conversation.
