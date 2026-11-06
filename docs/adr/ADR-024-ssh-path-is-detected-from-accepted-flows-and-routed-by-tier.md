# ADR-024 — An SSH path is detected from accepted inbound flows, in Log Archive, and the router alone decides who hears it

**Date:** 2026-11-06
**Deciders:** Claude (session), unreviewed (the signal, where it runs, no declared routing, the blind-detector rule, the table's home)
**Status:** Proposed
**Tags:** detection, flow-logs, ssh, routing, athena, b-101

---

## Context

Design 11: *"There is no SSH path in this architecture. Inbound on 22
appearing in flow logs is a finding in its own right … and is alarmed
directly."* B-101 deferred it until an Athena workgroup existed for another
purpose. Two now do: the trail query in Log Archive (ADR-008) and FinOps.

Flow logs have delivered from all four platform VPCs since 2026-10-31 (B-012),
to `altdig-flow-logs-<log archive>`, in a fixed 29-field text format
(`baseline/40-network.yaml`).

Arc8 dev has port 22 open to `0.0.0.0/0` by ruling (ADR-015), and that ruling
requires the exposure in the manager digest. The first query found it: one
instance, 985 accepted flows from 211 source addresses over a day and a half
[verified 2026-11-06].

## Decision

1. **The signal is `ACCEPT`, ingress, TCP, destination port 22.** REJECT is
   excluded.
2. **It runs in Log Archive**, where the flow logs are. Athena reads them in
   place through workgroup `platform-audit`. A table `platform_audit.flowlogs`
   is added to `security/50-trail-query.yaml`, so the account list it projects
   is refreshed by the same `deploy-trail-query.sh` run that every new account
   already triggers.
3. **Hourly, over a 70-minute window ending 10 minutes back**
   (`security/55-ssh-path.yaml`). One dossier per account and target
   (instance, or interface where there is none). The title and resource are
   stable, so the router's dedup key is too.
4. **The dossier declares no routing class.** It goes cross-account to Audit's
   `platform-alerts` as source `platform.network`, which is registered in the
   router's dossier rules. The router reads the subject account's tier: digest
   and Jira in dev and test, a page in uat and prod.
5. **A blind detector is a finding.** Every live interface writes a record each
   interval, NODATA when idle. So a window with no records at all is reported
   as the detector being blind, not as a quiet estate. That finding, and a
   failed run, each declare `digest_and_jira`, a downgrade from the page Log
   Archive's prod tier would give. If even that cannot be sent, the function's
   errors alarm reaches the router through `platform-forward-marked-alarms`.

## Alternatives considered

- **Include REJECT.** That would show who is trying, but every public
  interface is scanned constantly. A rejected connection is the security group
  doing its job, and the finding would never clear.
- **The detector in Audit**, beside the router. It would need a cross-account
  role into Log Archive to query, or the flow logs readable from Audit. Design
  04 keeps raw logs in one place. A cross-account `PutEvents` under the bus's
  existing organization grant is the smaller hole.
- **A Glue crawler or `MSCK REPAIR`** instead of projection. Each is a
  scheduled job that can silently stop. Projection computes partitions at
  query time, as the trail table already does.
- **Declare `digest` for dev**, so an ADR-015 exposure reaches the digest
  without opening an AltDigital Jira ticket. The emitter would need to know
  the tier, which only the router reads. Two copies of that fact are how B-117
  happened. Left to the router.
- **Configuration instead of traffic**, through a Config rule on security groups
  open to 22. That is a different control, already partly held by cfn-guard's
  `SG_NO_OPEN_INGRESS_EXCEPT_443` at template time. Design 11 asks for the
  traffic.

## Consequences

**Positive:**
- Design 11's SSH detection exists, 46 days after B-101 deferred it.
- ADR-015 condition (b) gains its first exposure line: Arc8 dev's open port 22
  now reaches the digest every day it carries traffic.
- `platform_audit.flowlogs` is queryable for investigations, and the saved
  query `inbound-ssh-accepted` gives an auditor the same answer over 30 days.

**Negative:**
- **Arc8 dev opens an AltDigital Jira ticket.** The router's dev default is
  `digest_and_jira`, so the deliberate exposure becomes one deduplicated ticket
  as well as the digest line. ADR-015 says the risk is the tenant's to
  mitigate; a ticket in AltDigital's queue sits awkwardly with that. Changing
  it is a router-side decision for dev dossiers, not this emitter's.
- **Detection is up to 80 minutes late.** That is fine for a path that should
  never exist. It is not a response to an intrusion in progress, which
  GuardDuty covers.
- **Hourly cost.** Each run scans about 2 MB. Negligible at four VPCs, and it
  grows with traffic.
- **The window check is estate-wide.** One VPC's delivery stopping while the
  others continue is not seen here. Log custody records each account's hourly
  arrival (B-134); nothing yet alarms on one account going to zero.
- **Positional columns.** A field inserted into the network baseline's
  `LogFormat`, rather than appended, would shift every column silently. Both
  templates say so.

## Related

- B-101, B-012, B-134; design 11, design 04
- ADR-008 (trail query), ADR-015 (dev exposure), ADR-018 (flow-log retention)
- `security/50-trail-query.yaml`, `security/55-ssh-path.yaml`,
  `alerting/20-router.yaml`, `scripts/test-ssh-path.py`
