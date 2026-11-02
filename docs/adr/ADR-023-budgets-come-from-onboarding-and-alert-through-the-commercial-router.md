# ADR-023 — Budgets come from the onboarding questionnaire, per application and tier group, and alert through the commercial router

**Date:** 2026-11-01
**Deciders:** Jamie (budget amounts come from the onboarding questionnaire, asked as acceptable budgets for the pre-production and production tiers, 2026-11-01); Claude (session), unreviewed (the thresholds, one budget per application and tier group, the storage on the client record, the anomaly monitor and its 25% threshold, the payer topic and Audit bridge, keeping amounts out of the derivation and the repository, the digest notice for an unanswered budget)
**Status:** Proposed
**Tags:** cost, budgets, commercial, questionnaire, phase-15.9

---

## Context

15.9 step 6 asked for a budget per tenant account and an anomaly monitor per
tenant, alerting through the commercial router. A budget needs an amount.
Setting it is commercial work, outside this build's scope, and amounts are
financial data that stay out of conversations and the repository.

The questionnaire already asked "At what monthly spend would you want to be
alerted?" (13.2), but nothing carried the answer anywhere: the coverage
inventory listed it as "form only, not built".

Jamie, 2026-11-01: "let's have that come from the onboarding questionnaire -
something along the lines of 'what are the acceptable budgets for your
pre-production and production tiers?'"

Budgets over linked accounts and anomaly monitors over cost categories exist
only in the payer. The commercial router lives in Audit and takes any dossier
on `platform-alerts` that carries `commercial_event`.

## Decision

1. **Two questions, replacing 13.1 and 13.2.** 13.2a asks for the pre-production
   budget (dev, test and uat together). 13.2b asks for production, which may be
   `deferred` until production is vested. The schema's `cost` section is
   optional, so earlier questionnaires still validate.
2. **One AWS Budget per application and tier group**, named
   `platform.<partner>.<client>.<app>.<pre-production|production>`. Each covers
   that group's accounts, monthly, in US dollars. The name is a contract with
   the bridge.
3. **Three alerts on each:** actual ≥ 80%, actual ≥ 100%, forecast ≥ 100%.
   All are percentages of the client's own amount.
4. **Amounts live on the registry client record**, as
   `budget#<app>#pre_production` and `budget#<app>#production`. They are
   written by `registry.sh set-budget`, which touches nothing else.
   `put-client` always writes `state`, so recording a budget through it would
   demote a live client. Onboarding step 1 calls `set-budget` from the
   questionnaire.
5. **Amounts never enter `derive-vesting.py`'s output.** That output is
   archived under the evidence lock. It carries only `set`, `missing` or
   `deferred`.
6. **One anomaly monitor**: AWS-managed, dimensional, on the `Tenant` cost
   category, notifying immediately at ≥ 25% impact. A new tenant is covered
   with no change.
7. **The path:**
   - Budgets and the monitor publish to `platform-cost-alerts` in the payer
     (`finops/20-cost-alerts.yaml`), encrypted with a key that admits only the
     two services.
   - `platform-cost-alert-bridge` in Audit (`commercial/20-cost-alert-bridge.yaml`)
     subscribes across accounts and emits `budget-breach` or `cost-anomaly`
     dossiers.
   - The commercial router decides who hears it. It notifies and never pages.
8. **Unanswered is reported, not blocked.** Each `sync-budgets.sh` run puts a
   digest entry for every group that has accounts and no answer, and for a
   deferred production budget once production exists. Vesting warns and
   proceeds.

## Alternatives considered

- **Auto-adjusting budgets from trailing spend.** No human figure needed, but
  a new account has no history, and the client's own number is the commitment
  worth watching.
- **Fixed amounts set by the commercial owner in Parameter Store**, as the
  Macie ceiling is (F-7). Rejected by Jamie's ruling: the client states what
  is acceptable.
- **One budget per account.** The question asks per tier, and a client thinks
  of dev and test as one pre-production spend.
- **Budget amounts in the derivation or a questionnaire file kept in the
  repository.** Either would put client commercial data into an immutable
  archive or into git.
- **The bridge in the management account.** That account holds the
  Organization and billing constructs, not functions. The alert bus and both
  routers are in Audit.
- **Block vesting without a budget.** A missing budget is a commercial
  exposure, not a safety one. A digest entry on every sync keeps it visible
  without stopping a build.

## Consequences

**Positive:**
- 13.2 moves from "form only" to "builds". A self-service page can ask it.
- No amount appears in the repository, a template, a derivation or a script's
  output.
- An anomaly is caught per tenant even where no budget exists.

**Negative:**
- **The create path is unproven.** As of 2026-11-01 no client had answered,
  so `create-budget` has not run against AWS. The first `set-budget`, then
  `sync-budgets.sh`, is the test.
- **Arc8 has no answer.** Its dev and test accounts are reported in the
  digest until `registry.sh set-budget` records one.
- **Budget messages are matched by name in AWS's text.** If AWS changes its
  wording and the name disappears from both subject and body, the bridge
  reports an unrecognised message in the digest rather than a breach. It is
  late, but visible.
- **13.3 (who receives cost alerts) is still not built.** Alerts go to
  AltDigital's commercial channel, and the client hears from a person.
- **A budget is never deleted** when its accounts go. It is left in place and
  stops matching spend.

## Related

- 15.9 step 6, design 10 and design 12, D-028
- `finops/20-cost-alerts.yaml`, `commercial/20-cost-alert-bridge.yaml`,
  `scripts/sync-budgets.sh`, `scripts/registry.sh set-budget`,
  `scripts/test-cost-alert-bridge.py`
- `vesting/questionnaire.schema.json` (`cost`),
  `docs/client-onboarding-questionnaire.md` section 13,
  `docs/questionnaire-coverage.md`
