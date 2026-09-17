# CLAUDE.md — AltDigital Managed Platform

Standing instructions for any Claude Code session in this repository.

## Read before making architectural choices

The design package lives in [starter_docs/](starter_docs/). Read the relevant
document before implementing against it. The build sequence is
[starter_docs/claude-code-prompts.md](starter_docs/claude-code-prompts.md).

Note: several documents the package cross-references are **not yet present** —
01 (Design Principles), 04, 05, 06, 08, 09, 11, 14, the Truveon functional
assumptions, and four of six diagrams. Where a prompt cites a missing document,
say so rather than inventing its contents.

## Hard rules

- **Never write code that terminates, deletes, deregisters, rolls back or
  purges a resource as part of automated remediation.** Security automation
  isolates and preserves. Operational automation is additive or reversible. If
  containment would take a production service down, it stops and pages.
- **All templates must pass `cfn-lint` and `cfn-guard` against `policies/`**
  before being considered complete. `scripts/lint.sh` and `scripts/guard.sh`.
- **Prefer explicit over clever.** This code will be read by auditors and by
  whoever is on call at 3am. Comments carry the *reason*, not the restatement.
- **No IAM users and no long-lived access keys for human principals** in member
  accounts. Ever.
- **Nothing hardcodes an OU id or an account id.** OU ids are published to SSM
  Parameter Store under `/platform/` and read from there.
- **No personal data in the repository.** Names, direct emails and phone
  numbers live in `config/contacts.env` (AWS alternate contacts) and
  `config/identity.env` (Identity Center group membership). Both are gitignored
  and blocked by the pre-commit hook. Group and permission set *names* are not
  personal data and do live in the repository.

## Windows / Git Bash traps

Both are handled centrally in `scripts/lib/common.sh`. Any new script must
source it; any AWS call made outside it will hit these.

**1. MSYS rewrites slash-prefixed arguments into Windows paths.**
`/platform/org/id` arrives at `aws.exe` as
`C:/Program Files/Git/platform/org/id`, and AWS rejects it with
*"Parameter name must be a fully qualified name"* — an error that points
nowhere near the cause. Affects SSM paths, IAM paths, CloudWatch log groups
beginning `/aws/`, S3 keys and ARNs. Fixed by exporting `MSYS_NO_PATHCONV=1`
and `MSYS2_ARG_CONV_EXCL='*'`, which are ignored on Linux and macOS.

**2. `--output text` can return CRLF.** Command substitution strips the
trailing `\n` and leaves the `\r`, so the *final* element of a multi-element
result carries an invisible carriage return. Substring tests and `awk` field
comparisons then fail for that one item only, which looks exactly like
eventual consistency and wastes an hour. Pipe every `--output text` read
through `no_cr`. Observed on multi-element output; single scalars appear
unaffected, but strip defensively regardless.

**3. Do not pipe a script into `tail`/`head` when the exit code matters.**
The pipeline returns the *last* command's status, so a `set -e` abort inside
the script reports success. Redirect to a file and read the file.

## Decisions already made

| Decision | Value | Notes |
|---|---|---|
| Landing zone | Plain AWS Organizations + custom StackSet baseline | Not Control Tower. The design's staged rollout, guardrail harness, registry and vesting saga replace what CT would provide, and CT would constrain the OU tree and Config scope. |
| Management account | `738815759702` | Holds the Organization and nothing else. |
| Home region | `us-east-2` | Also the IAM Identity Center home region, which cannot be changed without deleting the instance. |
| Allowed regions | `us-east-1`, `us-east-2`, `us-west-2` | us-west-1 dropped from design doc 02's list — two AZs and lagging service coverage. us-east-1 retained for global service endpoints. |
| Account naming | `ad-<partner>-<client>[-<app>]-<env>` | Enforced in `validate_slug` and by template `AllowedPattern`. |
| IaC | CloudFormation + StackSets | Terraform only for PagerDuty (design doc 13). |
| Commercial scope | Billing mechanics only | Pricing, SLA, margin and contract work are out of scope for this build. Everything required for exact per-partner and per-client billing is in. |

See [docs/deviations.md](docs/deviations.md) for accepted departures from the
design package.

## Working style

- Full drop-in replacement files, never snippets.
- One scoped piece of work at a time. Do the thing asked, then stop.
- Give the why alongside the what, and be direct about tradeoffs.
- Flag anything uncertain rather than assuming — especially where a load-bearing
  assumption (the `V` items in `starter_docs/open-items.md`) is involved.

## Layout

```
config/       platform.env — single source of truth for shared values
org/          CloudFormation for the Organization tree
identity/     Identity Center permission sets and assignments
policies/     cfn-guard rules; SCPs and RCPs land here
scripts/      bash entry points; lib/common.sh holds the shared helpers
docs/         runbooks and deviation records
starter_docs/ the design package (source material, not edited by build work)
```

Directories from the prompts document that do not exist yet — `baseline/`,
`vesting/`, `runbooks/`, `instrumentation/`, `recovery/`, `containment/`,
`provisioning/`, `scheduling/`, `partners/`, `pagerduty/` — are created by their
own phases, not in advance.
