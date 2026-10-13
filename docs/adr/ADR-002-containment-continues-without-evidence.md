# ADR-002 — Containment continues to isolate and revoke when evidence preservation fails

**Date:** 2026-10-13
**Deciders:** Jamie
**Status:** Accepted
**Tags:** containment, incident-response, evidence, design-07

---

## Context

The containment state machine (`containment/10-containment.yaml`) runs
preserve, then isolate, then revoke, then blast radius. Until 2026-10-13 every
stage's Catch went straight to Finalise, so any failure ended containment where
it happened.

The first scheduled containment exercise (2026-10-13) showed what that costs.
`preserve` could not share its snapshots with Forensics, because B-139 had made
`aws/ebs` the default volume key and AWS refuses to share snapshots encrypted
with it. Containment stopped there, and the drill's compromised instance and
principal were left exactly as an attacker would want them (B-170).

Design 07 orders preservation first because evidence is perishable. It does not
say what to do when preservation fails.

## Decision

A preservation failure does not stop containment. A failed snapshot share is
recorded per snapshot, and any exception from the preserve stage is caught into
`$.preserve_error`. Either way, execution proceeds to the containment decision
and, where the decision is to contain, to isolate and revoke. The summary
records the failure ("Evidence preservation FAILED and containment continued
without it"), and `shared_with` names Forensics only when every snapshot
reached it.

Failures in isolate, revoke and blast radius still go to Finalise. Their
outcome is recorded as `containment-incomplete` with an instruction to finish
by hand.

## Alternatives considered

- **Stop and page (the previous behaviour, made explicit).** Keeps the order
  strictly evidence-first. Rejected: the attacker keeps network reach and
  credentials until a human acts, which at 3am can be hours. Isolation and
  revocation do not touch the disks, so continuing loses nothing that
  preservation could still capture.
- **Retry preservation before continuing.** Rejected as the primary answer: the
  failure that prompted this was deterministic (a key AWS will not share), so a
  retry only delays isolation. Transient Lambda errors are already retried by
  the state machine.

## Rationale

The two failure modes are not symmetric. Evidence left in the contained account
can still be deleted by the attacker. An unisolated instance and an unrevoked
principal let the attacker keep acting: deleting that evidence, and more besides.
Containing first protects the evidence better than waiting would. The CLAUDE.md
rule that security automation "isolates and preserves" stays satisfied:
isolation happens, and preservation is attempted and its failure recorded,
never silent (P5).

## Consequences

**Positive:** containment can no longer be disabled by a preservation fault,
whether that fault is a key policy, a missing permission or an AWS limit.
**Negative:** an incident can now finish contained with its evidence still only
in the compromised account. That is recorded in the summary and readout, but
nothing automatically retries moving it later. B-170 stage 2 is meant to make
this case rare.
**Enforcement:** the Preserve Catch in the state machine definition; the
per-snapshot handling in `contain.py` `preserve`; the outcome logic in
`finalise`. The monthly containment exercise exercises the path.

## Not decided here

- Whether a containment that completed without evidence should page
  differently from a clean one. Today both route by tier.
- How evidence is made shareable. That is B-170 stage 2 (Jamie, 2026-10-13: an
  account default CMK *and* re-encrypt-and-share in containment).

## Related

B-170, B-139, design 07, P5, `containment/10-containment.yaml`,
`containment/src/contain.py`.
