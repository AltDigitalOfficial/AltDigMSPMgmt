# Verification Sweep

Design doc 13 defines a verification sweep that runs before go-live and **fails
closed** — any failure blocks, rather than raising a ticket for later. It is
"what makes provisioning a control rather than a script".

This file is the working version of that list: the design's eight checks, plus
what the build has since learned must be checked. Phase 12.5 implements it as
code; until then it is the checklist, and the automated parts are noted.

The discipline that matters more than the list: **every check must exercise the
thing, not assert that configuration exists.** Send a real alarm. Make a real
change. Count real members. Configuration that looks right and does nothing is
the failure mode this whole platform is built against, and it has already
happened three times during the build.

---

## From design doc 13

| # | Check | Status |
|---|---|---|
| 1 | Truveon received evidence from all accounts; sequence clean | Not built — Truveon |
| 2 | A synthetic test alarm reached PagerDuty and paged the correct rotation | Not built — phase 5 |
| 3 | The canary is running and passing | Not built — phase 4 |
| 4 | A test change reconciled correctly against Jira | Not built — phase 8 |
| 5 | Backup policy attached; a first recovery point exists | Not built — phase 5 |
| 6 | Guardrail harness passes against these specific accounts | **Automated** — `scripts/test-guardrails.sh --account <id>` |
| 7 | Cost Category mapping resolves in the commercial by-customer view | Not built — phase 9.2 |
| 8 | BAA notification windows set and non-null at BOTH levels, AltDigital's tighter | Not built — needs the registry |

---

## Added by the build

### V-A · Detective service coverage

**Automated** — `scripts/verify-security-coverage.sh`, and run automatically by
`scripts/create-platform-account.sh` after every account creation.

Asserts, for every service in every allowed region:

```
members(service, region) == active accounts − 1
```

Minus one because the delegated administrator is not its own member.

**Why this is a gate and not a note.** Delegation, enablement and enrolment are
three separate steps that look identical from a console, and the first two
produce an Organization that appears monitored and is not. Observed directly
(B-008): with all four services enabled and `--auto-enable` set in every region,
the Audit account had **zero members** and the canary had **no GuardDuty
detector**. Nothing reported it.

Two specific traps the check catches:

- `--auto-enable` is prospective. It covers accounts joining in future and does
  nothing for accounts that already exist
- The management account must enable each service *itself* before it can be
  enrolled — otherwise the one account no SCP can constrain becomes the only
  account without detection

**Must also run on:** any account creation, any account moved between OUs, any
new region added to `PLATFORM_ALLOWED_REGIONS`, and on a schedule. A member
account can be removed from a service without the service reporting anything.

### V-B · Config recorder is actually recording

**Partly automated** — the baseline custom resource verifies it at deploy time
and fails the stack if `recording` does not become true.

CloudFormation creates a Config recorder but never starts one, and exposes no
property that does (B-006 context). A recorder that exists and is not recording
reads as configured in the console and captures nothing.

Assert `describe-configuration-recorder-status` returns `recording: true`, not
merely that a recorder exists.

### V-C · CloudTrail is delivering, not just enabled

**Manual.** `get-trail-status` must show a recent `LatestDeliveryTime` and an
empty `LatestDeliveryError`. `IsLogging: true` alone is insufficient — a trail
can be logging and failing delivery on a bucket policy, which is how the
organization trail first failed.

Confirm an object has actually landed under `AWSLogs/<orgId>/<accountId>/`.

### V-D · Guardrail exclusions have not become escape hatches

**Automated** — part of `scripts/test-guardrails.sh`.

The SCPs exclude `Platform*`, `OrganizationAccountAccessRole` and
`stacksets-exec-*` by name. Those exclusions are only safe because SCP 03 denies
*creating* anything named `Platform*`. If that statement is ever detached or
weakened, every other denial becomes bypassable by naming a role.

The harness tests this directly and it must stay in the sweep.

### V-E · Sensitive data masking is actually masking

**Manual.** Write a Luhn-valid card number and an SSN to a log group created
*after* the policy, then read the group back without `logs:Unmask`.

Must use a NON-CANONICAL test number. AWS excludes the textbook values —
`4111111111111111`, `5555555555554444`, `378282246310005`, and its own
documentation example keys — so testing with them produces a false negative
that looks exactly like a broken control (B-009). `4532015112830366` works.

Assert three things, not one:

1. the value reads as `****` without `logs:Unmask`
2. a finding appears in `/aws/platform/data-protection-findings` naming the
   identifier and the source log group, and **not** containing the value
3. the `platform-sensitive-data-in-logs` alarm moves to ALARM

Masking without the finding is containment without detection; the code path
that wrote the data still exists and will write it again.

---

### V-F · Cross-region replication covers the PAST, not just the future

**Manual, and the two halves fail differently.**

**Half one — the rule works.** Write an object to each archive source, then
`head-object` the same key in the replica and assert on the *replica's* lock
state, not the source's:

```
aws s3api head-object --bucket altdig-config-archive-replica-<acct>   --key <key> --region us-west-2   --query '[ObjectLockMode,ObjectLockRetainUntilDate]'
```

Expect `COMPLIANCE` and a date roughly `RetentionDays` out. For the Config
archive this is the whole of B-006: the source has no lock, so a retention date
on the replica can only have been applied by replication.

**Half two — the backlog was carried across, and this is the half that gets
missed.** A replication rule is not retroactive. Every object written before
the rule existed stays where it is, permanently, while every signal reports
success: `get-bucket-replication` returns a valid configuration, the console
shows replication enabled, and the replica genuinely does contain objects.

So counting objects in the replica proves nothing. Compare counts:

```
aws s3 ls s3://altdig-log-archive-<acct>         --recursive | wc -l
aws s3 ls s3://altdig-log-archive-replica-<acct> --recursive --region us-west-2 | wc -l
```

A shortfall means `scripts/backfill-replication.sh` has not run, or has not
finished, or failed some tasks. **Re-run it after any edit to a replication
rule** — the same silent gap reopens every time one is changed, and nothing in
AWS will mention it.

---

### V-G · An alarm can actually reach the alerting topic

**Automated** — `scripts/test-alert-path.sh --alarm <name> --alarm-account <alias>`.

Design doc 13's check 2 is *"a synthetic test alarm actually reached PagerDuty
and paged the correct rotation"*. This is the first half of it, and the half
that can be tested before PagerDuty exists.

**Assert on the receiving side.** `set-alarm-state` returns success whether or
not the publish behind it is permitted, so a green command proves nothing. The
signal is `NumberOfMessagesPublished` on the topic, read from the **topic
owner's** account — the publishing account cannot see it.

Three things can refuse the publish, and all three fail identically and
silently:

1. the SNS topic policy does not admit the publisher
2. the KMS key policy does not admit the publisher — the message is encrypted,
   so a topic grant without a key grant fails after passing SNS
3. the condition key scoping either policy is never populated for the calling
   service principal, so it cannot ever match

(3) is the one to watch. `aws:PrincipalOrgID` against a service principal can
never match and cost a working day on the CloudTrail key. `aws:SourceOrgID` is
the correct key and is now verified for `cloudwatch.amazonaws.com` — but it was
verified, not assumed, and any new service principal added to those policies
needs the same treatment.

**Run it from two positions, not one.** A platform account (Log Archive) and a
member account (the canary) sit in different OUs under different SCPs. Proving
one does not prove the other, and the member account is the one that matters:
it is where every tenant alarm will come from.

**Still unproven until a routing key exists** — that the topic delivers to
PagerDuty and pages a human. A wrong routing key returns `200` to the
subscription confirmation and then discards every message, so `Confirmed` is
not evidence. Only an incident appearing in PagerDuty is.

---

## What the sweep cannot check yet

Stated so the gap is visible rather than assumed closed:

- **Evidence completeness.** Nothing verifies that what reached the archive is
  everything that should have. Design doc 04's sequence numbering and heartbeat
  are not built
- **Partner preconditions.** Partner `active` state and both notification
  windows live in the registry, which does not exist. `create-client-ou.sh`
  warns where design doc 13 requires it to block (D-004)
- **Config immutability.** The Config archive is not Object Lock protected, and
  cannot be — Config will not write to a locked bucket (B-006). Any claim that
  "evidence is immutable" covers CloudTrail, not Config
