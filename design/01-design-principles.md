# 01 — Design Principles

These are the rules the rest of the design obeys. Where a later decision appears
to conflict with one of these, the principle wins or the principle changes —
not both.

---

## P1 — Isolation is structural, not policy-based

Separation between member applications is enforced by the AWS account boundary,
not by IAM policy or network segmentation within a shared account. The same
boundary carries through to Truveon, where each member organisation has its own
tenant.

*Why:* an auditor can verify an account boundary in seconds. Verifying that a
shared environment is correctly segmented takes days and is never fully
convincing.

*Consequence:* account count grows fast. Account vesting must be automated
before the second application onboards.

---

## P2 — The platform guarantees the account, not the application

Applications arrive in whatever state they arrive in — a spreadsheet with a
Python script, an Azure proof of concept, a mature containerised service. We do
not dictate technology.

What is constant is what the *account* guarantees: logging, detective controls,
encryption, backup, patch visibility, monitoring, incident response.

*Consequence:* controls must be detective and generic rather than preventive and
application-specific.

---

## P3 — Detection is always ours; remediation follows the service model

Regardless of who owns patching, DNS, certificates or application code, the
detection layer is always AltDigital's and always running. What varies by
service model is who is obligated to act, and on what clock.

*Why:* this is what makes Model A defensible at audit. We can always produce
evidence that a condition existed, that the owner was notified, and when.

---

## P4 — The test path and the real path are the same code

Any recovery, restore or containment procedure is a single artifact with a mode
parameter. Test mode and production mode differ in target and teardown, never in
sequence or logic.

*Why:* a recovery procedure exercised only during an incident is a procedure
that has never been tested.

*Applies to:* backup restore, security containment, failover, account vesting.

---

## P5 — Automation reports; humans review

The target operating state is that the platform instruments, remediates and
documents itself, and produces a periodic readout of what it did. Humans should
receive a report, not a task list.

Where automation cannot complete an action, it produces an exception record —
never silence.

---

## P6 — Automation never destroys

Automated remediation may create, modify, isolate, scale, restart or revoke. It
may never terminate, delete, deregister or purge.

*Why:* the cost of an incorrect destructive action is unbounded; the cost of an
incorrect additive action is an alert.

*Particularly:* security containment preserves before it isolates, and never
cleans up.

---

## P7 — Every automated action is a change

Automated remediation and platform-applied configuration generate change records
on the same footing as human-initiated changes. Automation must not become a
route around change management.

---

## P8 — Evidence is generated, not asserted

Compliance evidence is produced as a by-product of operations — restore
durations, instrumentation actions, notification timestamps, approval
snapshots — and flows to Truveon continuously.

*Consequence:* completeness of the evidence pipeline is itself a control.
A silent pipeline failure must be detectable as a gap, not indistinguishable
from a clean month.

---

## P9 — Divergence is allowed but must be declared

Member accounts initialise from a global standard for monitoring, alerting and
remediation. They are not required to stay faithful to it.

Local variation is permitted only as an explicit, recorded override with a
stated reason and owner. This separates *deliberately different* from *drifted*.

---

## P10 — The blast radius of the platform itself is the biggest risk

The platform pipeline can modify every member account. The containment role must
work even against a fully compromised member account. The evidence store must
survive a regional event.

*Consequence:* staged rollout with bake periods, never a single push to all;
platform roles that member accounts cannot deny or modify; multi-region evidence
storage.

---

## P11 — Independence where we grade our own work

Penetration testing is always performed by a third party, never by AltDigital —
including, and especially, where AltDigital built the application.

The same logic applies to SLA measurement: the authoritative availability
measurement is external.