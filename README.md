# AltDigital Managed Platform

AWS infrastructure for a segregated, multi-tenant hosting platform supporting
independent member applications under HIPAA, PCI-DSS and SOC 2.

Design package: [starter_docs/](starter_docs/) · Build sequence:
[starter_docs/claude-code-prompts.md](starter_docs/claude-code-prompts.md)

## Where the build has reached

| Phase | State |
|---|---|
| 0 — Repository and validation loop | Built |
| Management account bootstrap | **Deployed** — org `o-61yvddd7d0`, root `r-t7wy`, 5 policy types, 18 service principals |
| 1.1 / 14.1 — Organization and OU structure | **Deployed** — stack `platform-org-structure`, drift `IN_SYNC` |
| 1.2 — SCPs | **Deployed** — 3 policies attached to Sandbox OU; Members not yet attached |
| 1.3 — Guardrail test harness | **Passing** — 8/9 provable against a live account |
| 2 onward | Not started |

Live tree:

```
Root  r-t7wy
├── Security         ou-t7wy-oa08csv6
├── Infrastructure   ou-t7wy-9pmwmp9x
├── Members          ou-t7wy-w4bt7t9p      (no SCPs attached yet)
│   └── direct       ou-t7wy-4mjvd5lu
└── Sandbox          ou-t7wy-tue14x6e      SCPs attached, harness passing
    └── ad-sandbox-canary  754280127660
```

Guardrails are live **in the Sandbox OU only** and proven by
`scripts/test-guardrails.sh`. The Members OU has no policies attached, so a
tenant account placed there today would be unprotected. See
[policies/scp/README.md](policies/scp/README.md).

Phases 1.1 and 14.1 are folded together deliberately, per the ordering note in
the prompts document: the partner and client OU levels are cheaper to build once
than to retrofit.

## Getting started

```bash
scripts/setup-tooling.sh      # check for cfn-lint and cfn-guard
```

Then follow [docs/bootstrap-runbook.md](docs/bootstrap-runbook.md). Steps 1–4
are manual and come before any script.

```bash
export AWS_PROFILE=msp-mgmt

scripts/bootstrap-management-account.sh --dry-run
scripts/bootstrap-management-account.sh

scripts/deploy-org-structure.sh --dry-run
scripts/deploy-org-structure.sh
```

Every script supports `--dry-run`. Read-only calls still execute during a dry
run, so the output reflects real current state rather than an assumption.

## Adding a partner and a client

```bash
scripts/create-partner-ou.sh --slug oeight --legal-name "OEight" \
  --domain oeight.io --dry-run

scripts/create-client-ou.sh --partner oeight --slug arc8 \
  --legal-name "Arc8" --dry-run
```

These create OUs only. A partner is not `active` because the script ran —
contract, BAA, notification window, downstream SLA commitments, Cost Category
dimension, Truveon tenant and registry entry are all separate and still
outstanding.

## The resulting tree

```
Root
├── Security                    Log Archive · Audit · Forensics
├── Infrastructure              Platform Tooling · Shared Services · Backup · Network
├── Members
│   ├── <partner>/
│   │   └── <client>/
│   │       ├── ad-<partner>-<client>-dev
│   │       ├── ad-<partner>-<client>-test
│   │       ├── ad-<partner>-<client>-uat     (opt-in)
│   │       └── ad-<partner>-<client>-prod
│   └── direct/                 AltDigital's own clients, no partner
└── Sandbox                     baseline canary · guardrail test target
```

## Why the tree is the billing model

Cost Categories derive partner, client, application, environment, service model
and compliance scope from the **account id** — no resource tags required. The
account boundary gives per-application spend for free, and the two roll-ups the
business needs fall out structurally:

- **by client** — what the partner invoices their own client
- **by partner** — what AltDigital invoices the partner

That is why slug validation, alias length limits and duplicate-OU detection are
enforced in code rather than documented as conventions. An account in the wrong
OU, or a second OU with the same name as its sibling, is a billing error that no
amount of downstream reporting can correct.

## Layout

```
config/         platform.env — single source of truth for shared values
org/            CloudFormation for the Organization tree
policies/guard/ cfn-guard rules; SCPs and RCPs land here
scripts/        bash entry points; lib/common.sh holds shared helpers
docs/           bootstrap runbook, deviation records
starter_docs/   the design package (source material)
```

## Conventions

- Nothing hardcodes an OU id or account id — they are published to SSM under
  `/platform/` and read from there
- No personal data in the repository. `config/contacts.env` is gitignored and
  blocked by the pre-commit hook
- No IAM users or long-lived access keys for human principals in member accounts
- Automated remediation never terminates, deletes, deregisters, rolls back or
  purges

Accepted departures from the design package: [docs/deviations.md](docs/deviations.md).
