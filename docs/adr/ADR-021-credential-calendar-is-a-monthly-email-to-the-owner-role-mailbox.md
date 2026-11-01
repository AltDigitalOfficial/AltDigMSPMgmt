# ADR-021 — The credential calendar is a monthly email to the owner role's mailbox, sent from Audit, outside the router

**Date:** 2026-11-01
**Deciders:** Jamie (a monthly notice, its wording, owner `platform-operations` reached at the root account's email, "somewhere in the Tooling or other shared account", 2026-11-01); Claude (session), unreviewed (Audit rather than Tooling, the same function, SNS email outside the router, a topic per owner, the address read from Organizations, the `platform-tool` tag, the else paths). Ratified by Jamie, 2026-11-01
**Status:** Accepted
**Tags:** secrets, reminder, notification, phase-15.8

---

## Context

ADR-020's reminder speaks only in a credential's last 30 days, through the
router, as digest entries and Jira tickets. It recorded no named owner, which
design 04 asks for.

Jamie, 2026-11-01:
- "somewhere in the Tooling or other shared account, let's create an
  Eventbridge notice to the root account (which is currently, but not
  forever, aliased to my account)";
- "monthly cycle - 'you have X months until <DATE>, on which date the API key
  for <TOOL> needs to be cycled'";
- "platform-operations, with an email address of the root account (again,
  aliased to mine, but not forever)".

The secrets and their clock tags live in Audit (ADR-019, ADR-020). SES in this
organisation is still in the sandbox (B-028/B-155). SNS email is already used
for the HIPAA tenant alerts, with the address kept out of templates.

## Decision

1. **Audit, in the existing function.** `platform-secret-age` gains a monthly
   mode, run by a second EventBridge rule on the 1st at 14:00 UTC. It reads
   the same tags as the daily run, and only tags.
2. **One email per owner role**, to SNS topic `platform-owner-<role>`. It lists
   every credential that role holds a cadence for, sorted by due date, one
   line each in Jamie's wording. A line names the secret too. Credentials
   inside 30 days say the daily reminders have started; overdue ones say so.
3. **`platform-owner=platform-operations`** on the five hand-issued tokens:
   Jira, the PagerDuty full and read-only keys, and both Truveon keys.
   **`platform-tool`** gives the name the email uses.
4. **The address is the management account's email, read from Organizations**
   at run time by `scripts/subscribe-secret-owner.sh`. It is never in the
   repository, a template or a stack parameter, and is never printed. When the
   root mailbox changes, re-running the script subscribes the new address. An
   old subscription is listed and left for deliberate removal.
5. **The else paths are digest entries:**
   - a cadence with no owner (it is in no calendar);
   - an owner whose topic is missing, or has no confirmed subscription. In
     that case nothing is published, because SNS accepts a publish to an
     unconfirmed subscriber and delivers nothing.
   A failed run trips the existing `platform-secret-age-errors` alarm.

## Alternatives considered

- **Tooling, as Jamie offered.** Rejected. The tags are in Audit, so Tooling
  would need a cross-account role able to list Audit's secrets, a new grant
  into the account that holds every platform credential, for no gain. Audit
  is a shared account and meets the ruling.
- **Through the router**, as a dossier with a new email routing. Rejected. The
  router routes events by tier and urgency; a calendar is neither, and cannot
  page. Adding an email channel to the router for one sender widens the
  component every alarm passes through. The hard rule is about paging and
  escalation; this path reaches no pager.
- **The address in `config/contacts.env`** (`CONTACT_OPERATIONS_EMAIL`).
  Rejected for this owner. Jamie named the root account's address, and
  Organizations is where that is authoritative. A copy would be stale on the
  day the root mailbox changes.
- **A NoEcho stack parameter for the address.** Rejected, for the reason
  `alerting/10-alert-topic.yaml` gives: the value is readable to anyone who
  can describe the stack.
- **One email per credential.** Rejected: at five credentials, five emails a
  month. One per owner reads as the calendar it is.

## Consequences

**Positive:**
- A re-issue is visible months ahead, in the root mailbox, without anyone
  remembering to look.
- Design 04's named owner exists, as a role, and the role decides the mailbox.
- Whoever holds the root mailbox inherits the calendar with no code change.

**Negative:**
- **The mailbox must confirm the subscription by hand**, and must do so again
  after any address change. Until then, the monthly digest entry is the only
  signal.
- **A second notification path outside the router.** It is narrow, one
  function, one topic prefix, and it is documented in the template, but it is
  a path an auditor will ask about.
- **A new owner role is code:** a topic in `security/90-secret-age.yaml`, and
  an address source in `scripts/subscribe-secret-owner.sh`. That is deliberate
  while there is one owner.
- **Email delivery is not proven by the run.** SNS reports a publish, not an
  arrival. A spam filter that drops it is invisible here.

## Related

- ADR-020 (the reminder and its tags), ADR-019 (secrets in Audit)
- `security/90-secret-age.yaml`, `scripts/subscribe-secret-owner.sh`,
  `scripts/set-secret-cadence.sh --owner --tool`
- Design 04, *Secrets*
- B-028/B-155 (SES sandbox)
