# Build Backlog

Technical debt and deferred work arising from the build itself. Distinct from
two other lists:

- [deviations.md](deviations.md) — accepted departures from the design package,
  each with a close condition
- [starter_docs/open-items.md](../starter_docs/open-items.md) — the design
  package's own open questions, owned by Jamie, Art and Wayne

Items here are things the build created or discovered, and that nobody has
asked for yet.

---

## B-001 · SCP policy 01 is close to the 5,120-byte quota

**Observed** — 2026-09-17. `01-protect-detective-controls.json` is **4,983 of
5,120 bytes**, leaving 137 bytes of headroom.

**How it got there** — the exclusion list on each statement grew from two ARNs
to six when Security and Infrastructure OU coverage required aligning the
exclusion sets with policy 03. Each statement carries the full list, so one
additional excluded principal costs roughly 220 bytes across the four
statements in this policy.

**Why it matters** — AWS rejects an over-size policy at `update-policy` time,
not at validation, so the failure lands during a deployment rather than in
review. `scripts/validate-policies.sh` does check the size and will fail first,
but only if it is run.

**Worth digging into, not urgent.** Several directions, in rough order of
appeal:

- **Shorten the exclusion list.** Six ARNs per statement is partly redundant:
  the `arn:aws:iam::*:role/...` forms were kept defensively, but the live run
  confirmed that assumed-role requests present only
  `arn:aws:sts::*:assumed-role/...`. Dropping the three `iam::role` variants
  would recover roughly half the exclusion cost with no behavioural change —
  but it removes a hedge against AWS changing the presented form
- **Condition on a principal tag instead of ARN patterns.** A single
  `aws:PrincipalTag/platform-role` condition replaces six ARNs with one
  expression. Requires the tag to be reliably set on every platform role and
  protected from tampering, which is a control in its own right
- **Split the policy.** Detective controls could divide into logging
  (CloudTrail, flow logs, log groups) and threat detection (GuardDuty, Security
  Hub, Macie, Inspector). There are two free policy slots per OU, though Phase
  10.2's bypass controls want one of them
- **Accept it and make the size check a hard gate** in the pre-commit hook, so
  the limit is hit in review rather than in a deployment

**Do not** simply keep adding exclusions and discover the limit during a
production policy update.

---

## B-002 · cfn-lint and cfn-guard are not installed

**Observed** — every commit so far. The pre-commit hook reports
`cfn-lint not installed — skipping` and `cfn-guard not installed — skipping`,
then exits zero.

**Why it matters** — a hook that skips silently is worse than no hook, because
it looks like validation is happening. No template in this repository has ever
been checked against `policies/guard/baseline.guard`, so those rules are
unverified — including the ones that would catch an unencrypted bucket or a
missing `RetentionInDays`.

The CloudFormation `ValidateTemplate` API (`scripts/validate.sh`) and IAM
Access Analyzer (`scripts/validate-policies.sh`) both run and both have caught
real defects, so this is not a total gap — but neither understands the house
rules in `baseline.guard`.

**Fix** — `scripts/setup-tooling.sh` prints the commands. `pip install --user
cfn-lint`; cfn-guard is a binary download or `cargo install cfn-guard`.

**Consider also** making the hook fail rather than skip when the tools are
absent, once they are installed — the current behaviour was right while they
were genuinely optional and is wrong afterwards.

---

## B-003 · StackSets delegated administration is unassigned

**Observed** — 2026-09-16, during delegated admin registration.

Design doc 02 lists CloudFormation StackSets among the services that should run
under delegated administration. It was deliberately skipped: the natural home
is Platform Tooling in the Infrastructure OU, which does not exist yet, and
delegating it to Audit would put the deployment pipeline inside the account
that audits it.

**Blocked on** — creating `altdig-infra-tooling`. Needed before Phase 2.4's
staged rollout pipeline.

---

## B-004 · Design package needs correcting in three places

The build has established facts that contradict the source documents. The
package is the reference an auditor would read, so leaving it wrong is its own
risk.

| Document | Says | Actual |
|---|---|---|
| `15-partner-model.md` | Account alias prefix `ad-` | `altdig-` — `ad-security-audit` is taken by another AWS customer (D-009) |
| `open-items.md` B15 | "`ad-` assumed" | Tested and failed |
| `entity-chain.mermaid` | Arc8 "owns app + data" | OEight owns the Arc8 application; Avergent owns theirs, built by OEight |

Also still absent and cited by the prompts: docs 01, 04, 05, 06, 08, 09, 11, 14,
the Truveon functional assumptions, and four of six diagrams. Doc 04 is the one
that bites — it holds the authoritative SCP denial list, which is why
`policies/scp/` is derived rather than transcribed.

---

## B-005 · CloudTrail is not delivered to CloudWatch Logs

**Decided** — 2026-09-17, when the organization trail was built.

The trail writes to S3 only. Design doc 07 expects incident readouts to answer
"what the principal did in the preceding 24 hours", which against S3 means
Athena rather than a log-group query — slower to write and slower to run, but
materially cheaper at organization scale.

**Revisit when** — the containment automation in Phase 7 is built and the
latency of an Athena query during a live incident can be judged against its
cost. Ingest into CloudWatch Logs is charged per GB and an organization trail
across every account is not a small volume.
