# ADR-020 — Hand-issued credentials get a reminder, clocked from a tag, not from the secret's dates

**Date:** 2026-11-01
**Deciders:** Jamie (a scheduled reminder in place of rotation, 2026-10-31); Claude (session), unreviewed (the tag clock, 365 days for the four tokens, `none` for routing keys, the reminder days, never paging). Ratified by Jamie, 2026-11-01
**Status:** Accepted
**Tags:** secrets, rotation, reminder, phase-15.8

---

## Context

Design 04 says that where rotation is not supported, the defensible position
is "a documented rotation cadence with a named owner, plus detection for
secrets exceeding their maximum age".

The PagerDuty REST token and the Jira token are issued by a person in each
vendor's console, and neither vendor has an API that would let a rotation
Lambda mint a new one. The Truveon ingest keys are issued outside AWS too.

Jamie, 2026-10-31: "we need to set a scheduled task to remind me when keys
need to be rotated".

Three things ruled out the obvious measures:
- **`LastRotatedDate`** is null for a secret that Secrets Manager has never
  rotated.
- **`LastChangedDate`** moves on any write, including a write of the same
  value. The ADR-019 cut-over rewrote every secret on 2026-11-01, so measured
  that way every token would read as one day old.
- **The managed rule `SECRETSMANAGER_SECRET_PERIODIC_ROTATION`** does not
  document which date it reads, or what it reports for a secret without
  rotation configured.

B-071 already watches the Jira token's hard expiry through reconciliation.

## Decision

1. **The clock is two tags on the secret.**
   - `platform-issued` is the date the credential was minted.
     `store_secret_everywhere` stamps it, but only when the value actually
     changed. `scripts/set-secret-cadence.sh` sets it by hand.
   - `platform-max-age-days` is the cadence in days, or `none`.
   - `platform-owner` is optional, and names a role, never a person.
2. **[security/90-secret-age.yaml](../../security/90-secret-age.yaml)** runs
   daily in Audit. It reads tags only: its role has no `GetSecretValue`, and
   it is not in the key policy. It raises:
   - **a reminder** (`digest_and_jira`) at 30, 14, 7 and 1 days before the
     due date;
   - **an overdue notice** (`digest_and_jira`) on the due day, then weekly;
   - **a hygiene notice** (`digest`), on Mondays only, for a secret with no
     cadence tag, no issue date, or an unreadable tag. Nothing is silently
     unwatched.

   It never pages. A due credential is a task, and emitters only downgrade
   (D-028). Dossiers come from `platform.secrets`, which the router now lists.
3. **Initial cadences:**

   | Secrets | Cadence | Issue date |
   |---|---|---|
   | Jira token | 365 days | 2026-09-19, so the due date matches Atlassian's one-year cap (B-071) |
   | PagerDuty REST token | 365 days | the survey's original date |
   | Truveon ingest keys (2) | 365 days | the survey's original dates |
   | Routing keys: platform, low-urgency, commercial, 4 tenant | `none` | — |

   Routing keys get `none` because they can only open incidents, and are
   replaced on compromise or staff change rather than on a calendar.
   `sync-pagerduty-tenants.sh` tags each new tenant key `none` as it creates
   it.

## Alternatives considered

- **The managed rule `SECRETSMANAGER_SECRET_PERIODIC_ROTATION`** (the 15.8
  prompt's suggestion). Rejected: its date field is undocumented, and the
  cut-over would have reset whatever it reads.
- **Reminders in `alerting/40-cadence.yaml`.** Rejected (plan finding 7). That
  is a schedule-liveness sweep, and token logic does not belong in it.
- **A reminder every day inside a window.** Rejected: 30 tickets for one
  credential. Exact days give 5, and the errors alarm covers a missed run.
- **Making every secret opt in, silently.** Rejected: untagged secrets would
  never be reported. The weekly hygiene notice is the else.

## Consequences

**Positive:**
- Every platform secret is either on a documented cadence or explicitly
  exempt. The tag is the documentation, and it is readable in the console.
- A new token restarts its own clock through the scripts that store it.

**Negative:**
- **Secrets written outside `store_secret_everywhere` must be tagged by
  hand.** These are the platform routing keys from
  `sync-pagerduty-secrets.sh`, and anything created in the console. The
  Monday notice is what catches them.
- **No named owner is recorded yet.** Design 04 asks for one. The tag exists,
  but nothing sets it.
- **365 days is a proposal.** It matches the Jira cap and gives one annual
  re-issue ritual, but it is not anyone's policy until ratified.

## Related

- ADR-019 (secrets in Audit, the cut-over that reset `LastChangedDate`)
- B-071 (Jira expiry), B-070 (Jira identity)
- Design 04, *Secrets*
