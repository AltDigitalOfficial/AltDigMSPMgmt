# Accepted Deviations from the Design Package

Where the build departs from `starter_docs/`, it is recorded here with the
reason and the condition for closing it. An undeclared difference is drift and
is treated as a finding — design principle P9 applied to the design package
itself.

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

## D-004 · Partner precondition warns where the design requires it to block

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

**Close when** — Phase 3.2 lands the registry and the script queries it. The
check must then move from warning to hard failure, and the same assertion must
appear in the phase 12.5 verification sweep — including that AltDigital's
notification window is *tighter* than the partner's, not merely present.

---

## D-008 · One test message silently dropped in transit

**Observed** — 2026-09-16, during plus-addressing verification. A test to
`mspr+ad-test-test-d@altdigital.ai` from one external sender never arrived.
The same address received successfully from a second external sender, and the
earlier `msp-mgmt+` tests succeeded from that same first sender.

**Significant because there was no NDR.** A bounce would have meant the message
was rejected at the edge — most likely proxy-address replication lag, which is
benign and self-resolving. No bounce means it was accepted and then dropped,
which is filtering: quarantine, spam classification, or a transport rule. None
of those notify the sender or the recipient.

**Decision** — Accepted and proceeding (Jamie, 2026-09-16). Not investigated.

**Risk while open** — Member account root addresses are where AWS sends the
root password reset, account verification and security notices. If Exchange
Online Protection is dropping mail to these addresses silently, the failure
surfaces on the day an account needs recovering, and the recovery path is the
thing that is broken. Plausible cause: repeated near-identical messages to
unusual plus-addressed recipients resemble directory-harvest probing, which EOP
has heuristics against — but that is a guess, not a finding.

**How to close, in ascending order of effort**
- Exchange admin center → Mail flow → **Message trace** on the recipient. This
  is authoritative and takes two minutes: it reports Delivered, Filtered as
  spam, Quarantined, or never accepted
- security.microsoft.com → Review → **Quarantine**. Quarantined mail never
  reaches Junk, so there is otherwise no way to see it
- The real end-to-end proof arrives free: AWS sends a welcome message to the
  root address at account creation. The first vested account confirms the whole
  path with genuine AWS mail rather than a hand-sent test

**If an exception is ever needed, do not allow-list `amazon.com` or
`amazonaws.com` by domain.** Spoofed AWS notifications are a common phishing
lure and a domain allow-list bypasses exactly the checks that catch them. Scope
any exception to DKIM-authenticated `amazonses.com`.

**Close when** — Message trace explains it, or the first vested account's AWS
welcome mail is confirmed received.

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
