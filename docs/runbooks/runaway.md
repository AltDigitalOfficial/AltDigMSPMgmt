# Runbook — runaway detection

**Alarms:** `platform-lambda-invocation-runaway`,
`platform-eventbridge-publish-runaway`, `platform-eventbridge-rule-runaway`

**Written for:** whoever is on call, at 3am, who did not build this.

---

## What has happened

Something is invoking itself. Not failing — *succeeding*, very fast, at
something it should not be doing at all.

The reason this needs a runbook is that every instinct from a normal page is
wrong here. There is no error to read, no dead letter to inspect, no stack
trace. The function is healthy. The rate is the fault.

## Why nothing else alarmed

This was learned the expensive way (B-116). A feedback loop produces:

| signal | during a runaway |
|---|---|
| Errors | zero — every invocation succeeds |
| Throttles | zero — nowhere near a concurrency limit |
| Duration | normal — the function does its small job correctly |
| DLQ depth | zero — nothing fails, so nothing is dead-lettered |

Every per-function alarm design 06a defines asks *"is this function working"*.
A runaway is a function working perfectly, extremely often.

## First: is it costing money fast?

Before diagnosing, get a rough rate. The three alarms fire at 500 per
five-minute period; the question is whether you are at 600 or 600,000.

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Invocations --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Sum
```

A Lambda loop on a small function costs cents per hour. The same loop wrapped
around a Bedrock call, a Textract job, a NAT gateway transfer or a
cross-region replication is three to four orders of magnitude more per
iteration. **Identify what the looping function calls before deciding how
fast to move.**

## Find it

```bash
# Which function
aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
  --metric-name Invocations --dimensions Name=FunctionName,Value=<name> \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Sum
```

In the CloudWatch console: *Metrics → AWS/Lambda → By Function Name →
Invocations*, last hour. The runaway is obvious by magnitude — it will be
hundreds of times every other line on the graph.

Then find what triggers it, and ask the question that matters:

> **Does the function's own output match the pattern of the rule that
> triggers it?**

```bash
aws events list-rules --event-bus-name platform-alerts \
  --query 'Rules[].[Name,EventPattern]' --output text
```

Compare each pattern against what its target *emits*. B-116 was a router that
stamped `commercial_event` onto its own output while the rule that invoked it
matched `detail.commercial_event exists`.

Other shapes worth checking, in rough order of likelihood:

- **Self-matching rule** — as above.
- **Retry storm** — a target failing and being retried with no backoff. This
  one *does* produce errors, so check the Errors metric to tell them apart.
- **Fan-out multiplication** — one event matching several rules whose targets
  each emit, so each cycle multiplies rather than repeats. Rate climbs
  geometrically rather than sitting flat.
- **Config/discovery loop** — a resource whose creation is discovered by a
  scanner that creates another resource. There is precedent: 06a's
  `resource_types_no_alarms` exists because the instrumentation Lambda once
  alarmed on the alarms it created.

## Stop it

**Disable the triggering rule. Do not delete the function.**

```bash
aws events disable-rule --name <rule> --event-bus-name platform-alerts
```

This halts the loop on the next cycle and preserves everything — the
function, its logs, the rule and its pattern — needed to understand what
happened. Deleting the function stops it equally well and destroys the
evidence, and it is not reversible in the way a disabled rule is.

If the loop is not on an EventBridge rule, reserved concurrency of zero stops
a Lambda without deleting it:

```bash
aws lambda put-function-concurrency --function-name <name> \
  --reserved-concurrent-executions 0
```

That is the blunt instrument. It also stops all legitimate traffic to that
function, so prefer the rule.

> Neither of these is automated, and deliberately so. CLAUDE.md: security and
> operational automation never terminates or deletes. A containment routine
> that disabled rules on a rate signal would eventually disable the platform
> during a legitimate burst, at the worst possible moment.

## Afterwards

1. **Record the cost.** Cost Explorer, filtered to the account, grouped by
   service, for the period. Even if small — the number is what justifies the
   next control.
2. **Fix it in two places** if it was a self-matching rule: narrow the rule's
   pattern *and* make the handler decline its own output. The rule is the
   cheaper stop, but it lives in a template somebody can widen later.
3. **Do not raise the threshold to make the alarm quiet.** If it fired on
   legitimate load, the estate has grown into the number — raise it
   deliberately, in `alerting/30-runaway.yaml`, and record the new normal in
   the commit message.

## Known limitation

The thresholds are fixed at 500 per five-minute period, chosen when normal
account-wide activity was low single digits. They will need raising as the
estate grows.

Anomaly detection was considered and rejected: it needs weeks of history, and
a band that widens on its own would have widened around B-116 given long
enough — adapting to a runaway is exactly the wrong behaviour for this
control.

**Revisit trigger:** tenant count, not a date. When routine operation reaches
a quarter of the threshold, raise it and re-measure.
