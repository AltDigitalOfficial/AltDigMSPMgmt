# Accepted Deviations from the Design Package

Where the build departs from `starter_docs/`, it is recorded here with the
reason and the condition for closing it. An undeclared difference is drift and
is treated as a finding — design principle P9 applied to the design package
itself.

---

## D-009 · Account alias prefix is `altdig-`, not `ad-`

**Design position** — [15-partner-model.md](../starter_docs/15-partner-model.md)
specifies `ad-<partner>-<client>-<env>`, justifying the prefix as: *"Account
aliases are globally unique across all of AWS; the prefix avoids collision and
makes ownership obvious in a support ticket."* Open item B15 records the prefix
as assumed rather than confirmed.

**Actual** — `altdig-<partner>-<client>[-<app>]-<env>`, with partner and client
slugs capped at 18 and application short codes at 10.

**Reason — the assumption was tested and failed.** Creating the Audit account
on 2026-09-16, `ad-security-audit` was rejected: already held by an unrelated
AWS customer. Two characters is not enough to make a generic name unique across
every AWS account in existence.

Member aliases were never really at risk — `ad-oeight-arc8-prod` carries
distinctive partner and client slugs. The exposure is concentrated in platform
accounts, whose names are ordinary words: audit, backup, shared, tooling.

**Why `altdig` rather than `altd`** — collision resistance comes from
distinctiveness, and `altd-security-audit` is only marginally less guessable
than `ad-security-audit`. `altdig` is implausible as anyone else's choice.

**Cost** — four characters against the 63-character alias limit, which is why
partner and client caps drop from 20 to 18 and application codes from 12 to 10:

```
altdig- + 18 partner + 1 + 18 client + 1 + 10 app + 1 + 4 env = 60 of 63
mspr+     18 partner + 1 + 18 client + 1 + 10 app + 1 + 1 tier = 55 of 64
```

The alias is now the binding constraint; the email has 9 characters of slack
because it carries no prefix at all. Against real values — `oeight` (6),
`avergent` (8), `arc8` (4) — the tighter caps are not a practical limit.

**Applied** — three live aliases renamed (`altdig-sandbox-canary`,
`altdig-security-logarchive`, `altdig-security-audit`), the SSM account registry
re-keyed, and `altdig-security-audit` confirmed available. No accounts had been
vested, so nothing downstream referenced the old names.

**Retained** — the numeric fallback in `create-platform-account.sh`, which
appends the last four digits of the account id on collision. With `altdig` it
should never fire; if it does, that is a signal worth investigating rather than
a routine outcome.

**Close when** — design doc 15 and open item B15 are updated to match, or the
decision is reversed.

---

## D-010 · Per-application Object Lock mode is not achievable with a shared archive

**Decision** — Log archive default is **COMPLIANCE, 2190 days** (6 years),
matching HIPAA's documentation retention expectation and the union of current
tenant frameworks (Jamie, 2026-09-16).

**Requirement raised alongside it** — lock mode "needs to be an app-by-app
decision, but our default should be COMPLIANCE".

**Why that cannot be done as stated.** Object Lock mode is a property of the
*bucket's default retention configuration*, and per-object mode can only be set
by the writer at PutObject time. CloudTrail and Config write to the archive
themselves; there is no hook to vary the lock header per tenant or per
application. So with one shared bucket, every object inherits one mode.

**And the direction is one-way.** GOVERNANCE objects can later be upgraded to
COMPLIANCE via `s3:PutObjectRetention`. COMPLIANCE can never be downgraded, by
anyone, including the account root. A COMPLIANCE default therefore forecloses
per-app variation for everything already written under it.

**What per-app variation would actually require** — separate log destinations
per tenant, each with its own bucket-level default. That changes the
topology design doc 02 describes ("Log Archive — immutable log destination,
Object Lock"), and it has real consequences:

- The CloudTrail *organization* trail writes to one destination. Per-tenant
  destinations mean per-tenant trails, which is a different and more expensive
  arrangement, and loses the single org-wide trail an auditor expects to see
- Cross-tenant correlation during an incident gets harder — design doc 07's
  "whether the same indicators appear in other accounts" check assumes one place
  to look
- Per-tenant buckets multiply the Object Lock configurations that must be right

**Status** — Deployed as COMPLIANCE/2190 on the shared archive, per the stated
default. Per-app variation is **not built** and is recorded here rather than
silently dropped.

**Decide before the first tenant with a conflicting requirement.** The likely
trigger is a client who will not accept six years of undeletable logs, or one
whose contract requires a shorter destruction schedule than the platform floor.
That is a contract question as much as a technical one (open item X2, Art and
Wayne).

**Related, and accepted the same day:** stdout data leakage in dev and test.
Prompt 2.1 already requires CloudWatch Logs data protection policies applied at
*account* level so they cover log groups created after deployment, masking card
numbers, US SSNs and credential patterns. Jamie's instruction to prioritise dev
and test specifically is carried into the Phase 2 baseline. This matters more
under COMPLIANCE: anything leaked into the archive cannot be removed for six
years, so masking at the source is the only control that actually works.

---

## D-012 · Four StackSets remain in the management account

**Design position** — [02-platform-architecture.md](../starter_docs/02-platform-architecture.md):
*"The Organization management account holds nothing but the Organization
itself. No workloads, no pipelines."* A StackSet administrator is a pipeline.

**Actual** — `platform-config`, `platform-kms-secrets`,
`platform-log-data-protection` and `platform-network` were created from the
management account, before `altdig-infra-tooling` existed. Delegation is now in
place (B-003), so new StackSets can be created from Platform Tooling. These
four cannot be moved there.

**Why not** — a StackSet is owned by the account that created it and there is
no transfer operation. Moving one means deleting it and recreating it
elsewhere, and deleting a StackSet deletes its stack instances, which removes
the baseline from every account it covers.

**And the retained resources make it worse than a brief gap.** Several baseline
resources carry `DeletionPolicy: Retain` precisely so that a stack deletion
cannot destroy them — KMS keys, aliases, log destinations. Delete the stack
instance and those survive, orphaned; recreate it and the new stack tries to
create an alias that already exists. The recreate fails partway, in an account
that currently has no baseline. The property that protects the resources is
exactly what makes the migration hazardous.

**The tempting argument, and why it does not hold.** Only the canary account
carries these stack instances today, so the cost of migrating rises with every
account vested — which reads as "do it now while it is cheap". But cheap is not
the same as safe: the orphaned-resource collision exists at one account just as
it does at forty. What changes with scale is the blast radius, not the
mechanism.

**Risk while open** — the management account runs four pipelines it should not,
so a compromise there reaches the baseline of every member account. That is
already true of the account by nature: it holds the Organization, and no SCP
constrains it. The StackSets widen an exposure rather than creating a new one.

**Close when** — one of:
- a migration is written that imports the retained resources into the new
  stacks rather than recreating them (`--resources-to-import`), and is
  exercised against the canary before anything else, or
- the four are retired and replaced by Platform-Tooling-owned StackSets during
  a baseline version bump that was going to reapply everything anyway.

The second is the cheaper path and should be taken at the next major baseline
change, rather than as a migration in its own right.

---

## D-011 · Platform services are single-region — CLOSED

**Design position** — [02-platform-architecture.md](../starter_docs/02-platform-architecture.md):
*"Platform services (Log Archive, Audit, Truveon evidence store) are
multi-region regardless of tenant tier. A regional event must not blind us
across every tenant simultaneously."*

**Actual** — everything is in `us-east-2` only. Verified:
`get-bucket-replication` on the log archive returns
`ReplicationConfigurationNotFoundError`. No cross-region replication on any
bucket; the Audit account's aggregation is regional.

**Decision** — Not deliberate. This was an omission, surfaced by a question
about what is backed up rather than by any check.

**Risk while open** — a `us-east-2` event removes the audit trail for every
tenant at once, which is the exact scenario the requirement names. This is
about **availability during a regional event, not durability**: S3 is
eleven-nines durable within a region and the data is not at risk of loss. But
"we cannot read our evidence today" is a poor answer during an incident, and a
worse one if the incident is the regional event.

**Why it also matters for immutability** — S3 Replication **can** write to an
Object Lock bucket, where AWS Config cannot (B-006). Replicating the Config
archive into a locked destination in a second region closes the immutability
gap and the regional gap in one change. That makes replication better value
than it first appears.

**Cost** — replication charges per GB transferred plus storage in the
destination, so roughly double the storage cost of whatever is replicated, plus
transfer. Real but modest at current volumes, and it scales with evidence
volume rather than account count.

**Status** — Closed 2026-09-17. Cross-region replication from `us-east-2` to
`us-west-2` on all three archive buckets, via
[security/20-archive-replicas.yaml](../security/20-archive-replicas.yaml) for
the destinations and the `ReplicationConfiguration` blocks in
[security/10-log-archive.yaml](../security/10-log-archive.yaml) for the rules.

**Verified, not assumed** — a CloudTrail object written at 20:31:23Z reached
`altdig-log-archive-replica-868150784436` with `ObjectLockMode=COMPLIANCE` and
`ObjectLockRetainUntilDate=2032-09-15`, inside one ten-second poll. The same
held for the Config archive. Both checks are in
[verification-sweep.md](verification-sweep.md).

**What nearly went unnoticed** — a replication rule is not retroactive. At the
moment the rule was created the log archive held 922 objects and the Config
archive 46, none of which the rule would ever touch. `get-bucket-replication`
would have returned a valid configuration, the console would have shown
replication enabled, and the replica would have contained objects — with nine
months of evidence still single-region and nothing anywhere saying so.

The backfill runs through S3 Batch Replication
([scripts/backfill-replication.sh](../scripts/backfill-replication.sh)), whose
manifest is filtered to replication status `NONE` or `FAILED` so it is safe to
re-run. **Re-run it after any change to a replication rule**, since the same
silent gap opens every time one is edited.

**Still regional** — the Audit account's Config aggregator and Security Hub
findings. The evidence in S3 survives a regional event; the ability to query it
through those consoles does not. Narrower than the original deviation and
recorded as its own item rather than left inside a closed one.

**Note on scope** — this is separate from tenant workload backup (design doc 05,
phase 5), which does not exist at all yet. There is no tenant data to protect
today; that changes with the first vested application.

---

## D-001 · Root mailbox is a personal alias, not a monitored distribution list

**Design position** — [15-partner-model.md](../starter_docs/15-partner-model.md),
open item B14: *"The base mailbox must be a monitored distribution list, not a
personal account — AWS sends root-level security and billing notices there."*

**Actual** — **Two** addresses, both currently aliases onto
`jamie@altdigital.ai`:

| Address | Used for |
|---|---|
| `msp-mgmt@altdigital.ai` | Organization management account root |
| `mspr@altdigital.ai` | Every member account root, plus-addressed |

**Decision** — Accepted for now (Jamie, 2026-09-16). Proceed with bootstrap.
Both become shared mailboxes later.

**The split is deliberate and should be preserved.** The management account
root cannot be constrained by any SCP or RCP and governs every account beneath
it; a member account root governs one account. Different blast radius, so they
should end up as different shared mailboxes with different access lists rather
than one mailbox for both.

**Risk while open**
- AWS root security and billing notices reach one personal mailbox
- No shared archive, so the notice history is not independently evidenced
- Single point of failure on one person's availability and tenure
- Every member account root address plus-addresses off `mspr@`, so the exposure
  grows with each account vested
- Compounds with [D-006](#d-006--root-mfa-is-a-virtual-authenticator-not-a-hardware-token):
  AWS account recovery runs through the root email, so password, MFA and
  recovery path all currently terminate with one person

**Mitigation applied** — The three alternate contacts (billing, operations,
security) are set to three different people. That does not close the deviation
but it stops the root mailbox being the only path for AWS notices.

**Close when** — Both `msp-mgmt@altdigital.ai` and `mspr@altdigital.ai` are
shared mailboxes. `msp-mgmt@` should include at minimum Jamie, Wayne and Art;
`mspr@` can be broader, since member account root mail is lower-value and
higher-volume.

**Deadline** — Before first client go-live. The go-live gate in design doc 13
already blocks on contract and BAA items; this belongs in the same set.

---

## D-002 · us-west-1 removed from the allowed region list

**Design position** — [02-platform-architecture.md](../starter_docs/02-platform-architecture.md):
*"US regions only. us-east-1, us-east-2, us-west-1, us-west-2."*

**Actual** — `PLATFORM_ALLOWED_REGIONS=us-east-1,us-east-2,us-west-2`.

**Reason** — us-west-1 exposes only two availability zones to most accounts and
lags materially on service availability. A two-AZ region is a poor foundation
for the multi-AZ production baseline the design commits to, and a region where
a needed service is absent turns into a per-tenant exception.

**Decision** — Narrowed (Claude, proposed; Jamie, accepted 2026-09-16).

**Consequence** — The region-restriction SCP in phase 10.2 will deny us-west-1.
If a tenant ever requires it, that is a deliberate exception with a stated
reason, not a default.

**Close when** — Design doc 02 is updated to match, or the decision is reversed.

---

## D-003 · Resource Control Policies enabled beyond the design package

**Design position** — The package predates RCPs and specifies SCPs only.

**Actual** — `RESOURCE_CONTROL_POLICY` is enabled as a policy type at bootstrap.

**Reason** — RCPs evaluate on the resource side regardless of which principal
acts. That is a materially stronger instrument than an SCP for two load-bearing
assumptions the design flags as needing periodic verification:

- **V1** — a member account with full local administrator cannot remove
  detective controls
- **V3** — the containment role cannot be blocked by a compromised member
  account

Enabling the policy type costs nothing and attaches no policy. It makes the
option available to phase 1.2 rather than requiring a root-level change later.

**Decision** — Additive, no risk while no policy is attached.

**Close when** — Phase 1.2 decides whether to use RCPs, and design doc 04 is
updated either way.

---

## D-004 · Partner precondition warns where the design requires it to block — CLOSED

**Design position** — [13-client-provisioning.md](../starter_docs/13-client-provisioning.md):
partner must exist, be `active`, and have a non-null notification window.
*"This must block, not warn."*

**Actual** — `scripts/create-client-ou.sh` blocks on partner OU existence, and
**warns** on the other three conditions.

**Reason** — `active` state and both notification windows live in the platform
registry, which is phase 3.2 and does not exist yet. There is nothing to query.

**Risk while open** — A client OU can be created beneath a partner whose
contract or BAA is incomplete. The incident notification chain would have no
defensible path.

**Mitigation** — No accounts can be vested yet, so an OU beneath an incomplete
partner is inert. The script prints exactly which conditions are unchecked.

**Status** — Closed 2026-09-17. The registry landed (phase 3.2) in the
Platform Tooling account, and `create-client-ou.sh` now calls
`scripts/registry.sh check-partner`, which **blocks**.

All four conditions are enforced, including the one most easily lost:
AltDigital's notification window must be strictly **tighter** than the
partner's, not merely present. Equal windows are refused. The reasoning is in
the error text rather than only in a design document — the partner owes their
client notification within N hours, so telling the partner at N leaves them no
time to act and the chain cannot be met. An equal window fails identically to
a longer one while looking perfectly reasonable in a table.

**Verified by the failure path, not the success path.** Running
`create-client-ou.sh --partner oeight --slug arc8 --dry-run` now refuses,
because OEight has no registry record. That is correct and is the first thing
the gate has ever stopped.

**The check is not duplicated.** `create-client-ou.sh` delegates to
`registry.sh check-partner` rather than reimplementing the rule, so there is
one definition of "may this partner have clients". A second copy would drift
from the first, and the symptom would be a notification chain that cannot be
met rather than a wrong number on a screen.

**Consequence for OEight** — recording the partner needs the **actual
contractual notification windows from the Arc8 contract**. Those numbers were
deliberately not invented to make a demonstration work. Until someone reads
them out of the contract, no client OU can be created beneath OEight — which
is the gate behaving exactly as designed, and is now visible rather than
buried in a warning nobody reads.

---

## D-008 · Delayed test message — CLOSED

**Observed** — 2026-09-16. A test to `mspr+ad-test-test-d@altdigital.ai` from one
external sender appeared not to arrive, while the same address received
successfully from a second sender. With no NDR, the reasonable reading was that
the message had been accepted and silently filtered — quarantine, spam
classification or a transport rule — which would have been serious, because
member account root addresses are where AWS sends root password reset and
account verification.

**Resolved same day.** The message arrived; it was **delayed, not dropped**.
No filtering, no quarantine, no transport rule.

**Worth keeping for the lesson, not the incident.** "No bounce and not yet
delivered" does not distinguish *dropped* from *slow*, and both readings were
available from the same evidence. The diagnosis was reasonable and wrong. Mail
delay across external senders is ordinary and the absence of an NDR says
nothing about it within the first few minutes.

**What would have settled it in two minutes** — Exchange admin center → Mail
flow → **Message trace**, which reports Delivered, Filtered as spam,
Quarantined or never accepted, with timestamps. That remains the right first
move for any future suspicion, in preference to inferring from silence.

**Status** — Closed 2026-09-16. Plus-addressed delivery to `mspr@` is verified
from both internal and external senders. Independently corroborated by the AWS
welcome message for `ad-sandbox-canary`, sent to
`mspr+sandbox-canary@altdigital.ai` on account creation — genuine AWS mail over
the real path, which is the strongest confirmation available.

**Standing guidance retained** — if a mail exception is ever needed for AWS
notifications, do not allow-list `amazon.com` or `amazonaws.com` by domain.
Spoofed AWS notifications are a common phishing lure and a domain allow-list
bypasses the checks that catch them. Scope any exception to DKIM-authenticated
`amazonses.com`.

---

## D-006 · Root MFA is a virtual authenticator, not a hardware token

**Design position** — [03-identity-and-access.md](../starter_docs/03-identity-and-access.md):
break-glass access uses *"hardware MFA tokens, credentials split and stored
physically."* The management account root user is the most extreme instance of
that requirement — it cannot be constrained by any SCP or RCP.

**Actual** — A virtual TOTP authenticator app on a personal mobile device.

**Decision** — Accepted deliberately, not as a stopgap (Jamie, 2026-09-16).
Hardware tokens are not being ordered.

**Risk while open**
- Not phishing-resistant. A TOTP code is valid on whatever page asks for it,
  including a convincing fake console. A FIDO2 key verifies the origin domain
  before signing and would refuse
- The factor lives on a general-purpose device that runs arbitrary apps and is
  exposed to mobile malware and SIM-swap-adjacent attacks
- **Compounds with [D-001](#d-001--root-mailbox-is-a-personal-alias-not-a-monitored-distribution-list).**
  AWS root recovery runs through the root email and registered phone. With the
  root mailbox aliased to a personal account, the password, the MFA device and
  the recovery path all terminate with one person

**Mitigations — applied**
- The TOTP seed is deliberately **not** stored in the password manager that
  holds the root password. Storing it there would put both factors behind one
  vault and make the arrangement single-factor in practice

**Mitigations — OUTSTANDING as of 2026-09-16**
- **Only one MFA device is registered on root.** Loss or failure of that single
  phone means root recovery runs through the AWS account-recovery process,
  which depends on the root email and registered phone — and the root email is
  itself deviation D-001. This is currently the sharpest edge in the bootstrap
- A second factor is planned on Art's phone. That placement is deliberate and
  better than a second device held by the same person: password in Jamie's
  vault plus factor on Art's device means **no single individual can sign in as
  root alone**, which is the split-credential property design doc 03 requires
  of break-glass. Pending Art's availability

**Target end state** — Password held by Jamie; MFA factors registered on Art's
and Wayne's devices. That gives split credentials *and* removes single-device
loss, and maps onto the existing key-deletion quorum membership (any two of
Jamie, Wayne, Art) rather than inventing a second, inconsistent grouping.

**Close when** — Either a FIDO2 security key is registered on root and the
virtual devices removed, or design doc 03 is amended to permit virtual MFA for
the management account root and state why.

**Review** — At the same point as D-001, before first client go-live. The two
should be assessed together; individually each is tolerable, and the
combination is what carries the real exposure.

> Note: this deviation covers the **management account root user only**. It does
> not extend to the break-glass emergency accounts in design doc 03, which are a
> separate control and still specify hardware tokens with split credentials.
> Nor does it extend to member account root users — those are addressed by
> centralised root access management in a later phase.

---

## D-007 · Standing `PlatformBootstrapAdmin` grant with no JIT elevation

**Design position** — [03-identity-and-access.md](../starter_docs/03-identity-and-access.md):
*"No standing administrative access."* `PlatformAdmin` is JIT-only — a stated
reason, a time box, an automatic expiry, and an elevation event that flows to
Truveon as evidence.

**Actual** — A permission set named `PlatformBootstrapAdmin` grants standing
`AdministratorAccess` on the management account to one person.

**Reason** — The platform cannot be built without administrative access, and
none of the JIT machinery exists yet: no elevation request path, no approval
workflow, no Truveon to receive the evidence. The alternative was an IAM user
with static access keys, which is strictly worse — a long-lived credential that
cannot be centrally revoked and does not expire.

**Why the name differs** — Deliberately *not* called `PlatformAdmin`. Reusing
the design's name for a standing grant would quietly redefine a control that the
design describes as JIT-only, and an auditor reading both would reasonably
conclude the JIT requirement had been met. A different name keeps the gap
visible.

**Risk while open**
- Standing administrative access to the Organization management account
- No elevation record, so there is no evidence trail distinguishing routine work
  from privileged action
- Scope is limited to account `738815759702` — no member accounts exist yet, so
  the blast radius is the Organization itself rather than tenant data

**Mitigations applied**
- Federated via Identity Center, not an IAM user. Centrally revocable, no static
  keys, MFA inherited from the identity source
- One-hour session duration
- Assigned to the management account only, not at the root or an OU

**Close when** — JIT elevation exists (`PlatformAdmin`, `SecurityResponder`) with
time-boxing and an elevation record. `PlatformBootstrapAdmin` is then deleted,
not merely unassigned.

**Deadline** — Before the first member account is vested. A standing admin grant
over an Organization containing no tenant data is a very different proposition
from one containing regulated client workloads.

---

## D-009 · `CommercialReadOnly` is assigned to the management account

**Design position** — [12-commercial-access.md](../starter_docs/12-commercial-access.md):
cost and billing data is Organization-level, which creates tension with keeping
the management account nearly empty. *"Do not resolve that by granting access to
the management account."* The design's answer is a CUR export to S3 in a
dedicated reporting account plus delegated Cost Explorer access, consumed from
the aggregation layer.

**Actual** — The `CommercialReadOnly` permission set is assigned to
`738815759702`, the Organization management account.

**Reason** — The aggregation layer does not exist. The Infrastructure OU holds
no accounts, there is no reporting account, no CUR export and no registered
delegated administrator for any billing service (verified 2026-09-16:
`list-delegated-administrators` returns empty). The management account is
currently the only place cost data exists, so the choice was between this and
no commercial access at all.

**Decision** — Accepted (Jamie, 2026-09-16), with the scope below.

**A correction to the design worth recording.** Closing this deviation will not
remove the management account grant entirely, because part of it cannot move.
Delegated administration covers Cost Explorer, Budgets, CUR / Data Exports, cost
anomaly detection and Cost Optimization Hub. It does **not** cover invoices,
payment instruments, credits or tax — those are payer-account data and are
readable only in the management account. Design doc 12 lists "Billing console,
invoices" under Granted, so the design already requires something that its own
placement rule forbids. When the aggregation layer lands, the correct end state
is a split: the analytical surface moves, and a much narrower invoice-and-payer
grant stays here.

**Risk while open**
- A standing federated grant on the management account for a non-engineering
  function. That is the account with no SCP above it
- The grant reveals the account and OU tree — effectively the client roster —
  because Cost Explorer is unreadable without account names
- Growing exposure: today the only member account with spend is
  `ad-sandbox-canary`, so there is close to nothing to see. That stops being
  true at first vesting
- Compounds with [D-007](#d-007--standing-platformbootstrapadmin-grant-with-no-jit-elevation):
  two standing permission sets now exist on the management account

**Mitigations applied**
- The permission set's inline policy carries a `NotAction` deny that permits
  only billing service prefixes. Everything else — CloudWatch, logs, Config,
  Security Hub, GuardDuty, S3, EC2, KMS, IAM, Identity Center, Organizations
  writes — is denied outright, and stays denied for services AWS has not
  launched yet. This matters more than usual precisely because SCPs do not
  apply to the management account, so the permission set is the whole boundary
- A second deny removes every write action inside the billing domain,
  enumerated by verb prefix per service, including tagging — Cost Categories
  and cost allocation tags are the billing model, so a tag change is a billing
  change
- Assigned to exactly one account. No member account access of any kind
- Assigned to a group, never to a user. Membership is the only lever
- Four-hour session, and read-only throughout
- Verified against IAM Access Analyzer (`validate-policy`, no findings) and
  `cfn-guard`

**Close when** — The reporting account exists, CUR / Data Exports is landing
there, and a billing delegated administrator is registered. At that point the
analytical half of this grant moves to the reporting account and what remains
here is narrowed to invoices and payer data.

**Deadline** — Before the first member account is vested, the same gate as
D-007. A billing-only grant over an Organization whose entire spend is one
sandbox account is a very different proposition from one covering live client
workloads.

---

## D-010 · Identity Center users and groups are created outside Entra — CLOSES ITSELF

**Design position** — [03-identity-and-access.md](../starter_docs/03-identity-and-access.md):
Entra ID is the source of truth for staff identity, SAML for authentication,
SCIM for provisioning, so joiners and leavers flow automatically.

**Actual** — The identity source is still the built-in Identity Center
directory. `scripts/deploy-commercial-access.sh` creates the group and its
members directly in that directory from `config/identity.env`.

**Reason** — Entra federation is listed under "Later" in the bootstrap runbook
and has not been done. Access was needed before it.

**Known cost, accepted deliberately** — Switching the identity source to Entra
**deletes every directory user and group and every assignment made to them**.
Permission sets survive. The runbook's own advice was to do the cutover while
the user count was one; it is now two, and this deviation is the record of
having gone the other way.

**Why the damage is bounded** — The permission set and its policy are
CloudFormation and survive untouched. The group id is a template *parameter*
rather than an `AWS::IdentityStore::Group` resource, specifically so that the
same template works either side of the cutover: before it the deploy script
creates the group, after it SCIM does, and only the parameter value differs.
Redeploying after the cutover is one script run, not a rewrite.

**Close when** — The identity source is Entra, the `CommercialReadOnly` group
is provisioned by SCIM from an Entra group of the same name, and
`config/identity.env` is deleted. Two sources of truth for who has access is
worse than either one alone.

**Deadline** — Before the team grows past a handful of directory users. Every
user added before the cutover is another one to recreate after it, and the
recreate is silent — access simply stops working.

---

## D-005 · Design package layout differs from the prompts document

**Design position** — [claude-code-prompts.md](../starter_docs/claude-code-prompts.md)
assumes `design/`, `intake/`, `prompts/`, `diagrams/`, `truveon/`. The README's
internal links assume the same.

**Actual** — Everything is flat in `starter_docs/`, and eight design documents
plus four diagrams and the Truveon functional assumptions are absent entirely.

**Reason** — The source material was delivered flat. Nothing has been moved,
because reorganising someone else's documents unasked is its own kind of drift.

**Consequence** — Cross-references inside the design documents do not resolve,
and any prompt citing a missing document cannot be followed as written.
Notably, `01-design-principles.md` is absent while P1, P4, P9 and P10 are cited
throughout — P4 is called *"the most important constraint"* in prompt 6.1.

**Close when** — Either the missing documents are supplied and the tree is
reorganised to match, or the prompts document is updated to reference
`starter_docs/` as it stands.
