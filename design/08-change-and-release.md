# 08 — Change Management and Release

## The constraint

Development teams are not overseen by AltDigital. SOC 2 requires visibility and
approval of changes to the environment. We therefore cannot rely on a gate we
control — **change management is a detective control, reconciled continuously.**

Every deployment leaves an auditable trail whether the dev team cooperates or
not.

---

## The control point

Deployment into a member account happens **only through a role AltDigital's
platform grants**, never through long-lived developer credentials.

- `AppDeployer` role per member account
- Assumable only by the tenant's CI identity via OIDC federation — no static keys
- Scoped to the resources that application legitimately deploys
- Denied from touching platform-managed resources (SCP)
- **Every assumption and every action is logged and reconciled**

### Why this holds as teams change

For the first two applications AltDigital can enforce whatever process it likes.
That leverage should be used now to set the bar.

A later team arriving with its own pipeline keeps their pipeline — they simply
hand off at this role. **The control point stays constant even as the tooling
varies.**

---

## Reconciliation

```
Intent:   Pipeline registers a change in Jira before deploying
Reality:  CloudTrail + EventBridge capture what actually happened
Judgment: Reconciliation compares the two; mismatches are findings
```

### What is captured as a deployment event

- `AppDeployer` role assumption
- ECS task definition revisions and service updates
- Lambda function code and configuration updates
- CloudFormation stack create/update
- EC2 launch template and AMI changes
- RDS parameter group and engine version changes

### Outcomes

| Finding | Routing |
|---|---|
| Deployment with a matching approved change | Evidence record, no action |
| Deployment with no change record | Low-severity page + Jira ticket to the tenant's change approver |
| Deployment outside an approved window | Finding, owned and dated |
| Change record with no corresponding deployment | Review — stale or abandoned |

Auditors accept continuous reconciliation far more readily than a signature on a
form, because it is **evidence rather than a claim**.

---

## System of record

| Layer | System |
|---|---|
| Human workflow — request, approve, discuss | **Jira Service Management** |
| Authoritative record of what happened | **AWS CloudTrail** |
| Correlation, evidence retention, audit response | **Truveon** |

Truveon is the system of record for audit. Jira is the workflow layer feeding it,
not the record.

This distinction matters because **Jira history can be altered by a project
admin**, and auditors will ask about that. Where an approval is legally or
commercially significant, the approval evidence is **snapshotted immutably into
Truveon at the moment of execution**.

---

## Quorum verification (key deletion and equivalents)

For quorum-controlled actions (see [03](03-identity-and-access.md)), the
executing pipeline queries Jira at execution time and verifies:

1. Both approvers are on the authorised list
2. **Neither approver is the requester**
3. Approvals postdate the request
4. The issue has not been reopened or edited since approval
5. The approval is for this specific resource and action

Jira permissions are easy to get subtly wrong. The pipeline **validates rather
than trusting the ticket's status field alone.**

Reconciliation then runs in the opposite direction: every `ScheduleKeyDeletion`
in CloudTrail must map to an approved Jira change. Any that does not is a
finding. **That is what makes it a control rather than a procedure — proving the
negative, not just documenting the positive.**

---

# The platform's own pipeline

The most privileged thing AltDigital owns. It can modify every member account.

## Tooling

**CloudFormation + StackSets.** The only IaC that natively understands the
Organization: StackSets with delegated administration push the baseline to an
OU, and accounts inherit automatically on placement.

That is the vesting mechanic — *create account, move into OU, baseline lands.*

## Where it lives

**A dedicated Platform Tooling account** — not the Organization management
account. The management account stays nearly empty and nearly untouched, which
is both AWS guidance and an easier audit conversation.

The tooling account holds the repository, CodePipeline, and the StackSet
administration role.

## Staged rollout

A bad baseline change is the one failure mode that hits every account
simultaneously (P10).

```
Sandbox canary account
        ↓ bake period
Non-production Members OU
        ↓ bake period
Production Members OU  (max concurrent count limited)
```

- Automatic rollback on stack failure
- StackSet deployment targets and `MaxConcurrentCount` provide this natively
- **Never a single push to all**

## Drift detection

Scheduled StackSet drift detection; findings to Truveon. Same principle as the
monitoring layer — erosion must be visible.

## Approvals

**Interim: Jamie is sole approver for baseline changes.** Practical at current
size and not worth fighting.

Two things to put in place anyway, because they cost almost nothing now and
prevent an awkward finding later:

1. **A named second approver** for periods of unavailability, exercised
   occasionally so it is known to work
2. **Separation of authoring and deployment evidence.** Where the approver is
   also the author, SOC 2 will ask how self-approval is controlled. The
   answer: merge and deploy are separately logged, and a documented periodic
   review of what shipped is performed and retained.

---

## Development environment

The build work is viable through **Claude Code + VS Code + AWS CLI**.
CloudFormation templates are text, the CLI covers every API involved, and the
whole thing is repository-shaped.

**Credential hygiene:** Claude Code executes in the operator's shell and
inherits the active AWS profile. Default to a **read-only or plan-only profile**;
the elevated deploy profile is assumed deliberately. `aws sso login` with
short-lived Identity Center credentials fits this well.

**Local validation loop** — fast, and prevents discovering problems in a
pipeline run:

| Tool | Purpose |
|---|---|
| `cfn-lint` | Template correctness |
| `cfn-guard` | Policy-as-code against AltDigital's own rules |
| ChangeSets | Preview actual deployment effect |

**Wrap common StackSet operations in scripts early.** The CLI works but is slow
and its failure output is terse.

> **Caveat.** Claude Code is strong at generating and refactoring templates and
> at reasoning about CLI errors. It is not a substitute for judgment about
> whether an architectural choice is correct — it will build what it is asked
> for. Given baseline changes reach every account, the review step stays human
> even where authoring is assisted.