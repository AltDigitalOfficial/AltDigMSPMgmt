# ADR-006 — An audit grant has a required end date, covers every account, and is removed only by an explicit flag

**Date:** 2026-10-08
**Recorded:** 2026-10-14
**Deciders:** Claude (session), unreviewed
**Status:** Proposed
**Tags:** identity, audit, access-review, lifecycle

---

## Context

ADR-005 makes `AuditEvidenceReader` its own permission set. Three things about
how it is granted were left to the build:

1. **Duration.** It reads archived CloudTrail and platform log contents, both
   personal data. That is right for an engagement and wrong as a standing
   grant.
2. **Which accounts.** The evidence sits in five accounts: management, Audit,
   Log Archive, Security Backup and Infra Tooling. But an assessor also has to
   walk a member account's configuration, for example the Config rules and
   denied calls in a tenant account.
3. **Removal.** Every other access script in the repository adds and never
   removes (`deploy-security-access.sh`, `deploy-commercial-access.sh`,
   `grant-developer-access.sh`). A member missing from config is reported and
   left in place, so that a stale config file cannot take someone's access away
   mid-incident.

## Decision

Implemented in [scripts/deploy-audit-access.sh](../../scripts/deploy-audit-access.sh):

- **`AUDIT_ENGAGEMENT_ENDS` (YYYY-MM-DD) is required to grant.** The script
  refuses to grant without it, or once it has passed, and warns above 120
  days. The date is published to `/platform/identity/audit-engagement/ends`.
- **The set is assigned to every ACTIVE account**, derived from Organizations
  on each run, as `SecurityReadOnly` is.
- **Removal is an explicit flag, `--end-engagement`.** It deletes every
  assignment of the set to the group, in every account where the set is
  provisioned, and records `ended <date>` in the parameter. The group, its
  members, the set and the query stack stay, so the next engagement needs only
  a config change and a re-run.
- In normal runs the script follows the house rule: a member missing from
  config is reported, not removed.

## Alternatives considered

- **Assign only to the five evidence-holding accounts.** This was proposed
  first, in auditor-guide §1.3 as originally written. Rejected: an assessor
  could not see a tenant account's configuration, which is part of testing
  the guardrails. Assigning everywhere is safe only because every Allow beyond
  the managed policies is scoped to a platform-named resource (ADR-005,
  ADR-007).
- **No end date; rely on someone removing the grant.** Rejected: that is a
  standing grant with good intentions, and it is the failure the end date
  exists to make visible.
- **Remove automatically when the date passes.** Not built.
  - Doing it inside the grant script fails because the script only runs when
    someone runs it.
  - A scheduled revoker would be the first piece of platform automation that
    takes access away.
  - Revoking an auditor's access mid-fieldwork because a date was mistyped is
    a worse failure than one extra day of read access.
  - Detection with a human decision is the better shape; see Consequences.
- **Treat removal as a side effect of an emptied member list.** Rejected for
  the house reason: removal should be a deliberate act with its own record.

## Rationale

The end date turns standing-versus-temporary from an intention into a
checked input. The flag keeps removal deliberate and recorded, consistent with
every other access script. It is the one place in the repository where a
script removes access, and it does so only when explicitly asked.

## Consequences

**Positive:**
- An engagement cannot be granted without an end date on record, and the
  remaining days are printed on every run.
- An extension is a config change, so it has a record.

**Negative:**
- **A lapsed engagement is invisible.** If nobody runs `--end-engagement`,
  access continues past the date, and nothing reports it until someone next
  runs the script. This breaks the "every decision model needs an else"
  discipline. The check needed is: read the published date and raise a
  finding through the router when it has passed while assignments still
  exist. It is not built.
- Open console sessions run to their 4-hour expiry after removal. Identity
  Center does not revoke sessions it has already issued.
- One group and one end date means **one engagement at a time**. Concurrent
  engagements, such as AltDigital's own SOC 2 and a tenant's HIPAA
  assessment, need a per-engagement shape. See ADR-005, *Not decided here*.

**Enforcement:**
- Script refusals: no date, a malformed date, or a past date.
- Ending an engagement is manual discipline only until the lapse check exists.

## Related

- ADR-005, ADR-007
- `scripts/deploy-security-access.sh` (the add-never-remove precedent)
- `config/identity.env.example` (`AUDITORS_*`, `AUDIT_ENGAGEMENT_ENDS`)
