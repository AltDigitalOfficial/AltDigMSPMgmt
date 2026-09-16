# Service Control Policies

**Status: written and validated. Nothing is attached.** Attaching is a separate,
deliberate step and should not happen before the Phase 1.3 guardrail harness can
prove these actually hold — see [Before attaching](#before-attaching).

---

## The source problem

Prompt 1.2 says to take the denial list from
`design/04-security-controls-and-evidence.md`, section *"Protecting the controls
from the account"*. **That document is not in the package.** The prompt literally
reads `[list from design 04]`.

What follows is therefore **derived**, not transcribed, and needs review against
doc 04 when it surfaces. Every denial traces to a requirement stated somewhere
in the documents that do exist:

| Denial | Source |
|---|---|
| `kms:ScheduleKeyDeletion` | [03](../../starter_docs/03-identity-and-access.md) — quorum-controlled actions |
| Backup vault deletion / vault lock modification | 03 — quorum-controlled actions |
| Removal of an account from the Organization | 03 — quorum-controlled actions |
| Disabling CloudTrail or the Config recorder | 03 — quorum-controlled actions |
| Log destination configuration | 03 — customer access, *"enforced by SCP, not by permission set alone"* |
| KMS key policy modification | 03 — customer access |
| Platform role modification | [07](../../starter_docs/07-response-automation.md) — *"a role member accounts cannot modify, deny or delete"* |
| Platform-managed tag modification | [10](../../starter_docs/10-commercial-model.md) — *"modification by member account principals is denied by SCP"* |
| Detective control removal generally | [open-items](../../starter_docs/open-items.md) V1 |
| Backup tampering | open-items V2 |
| Containment role protection | open-items V3 |

## The three policies

Split into three rather than one because the quota is **5,120 bytes per policy**
and **5 policies per OU**. Three focused policies leave two slots at the Members
OU for the Phase 10.2 bypass controls without a rewrite.

| File | Protects | Size |
|---|---|---|
| `01-protect-detective-controls.json` | CloudTrail, Config, GuardDuty, Security Hub, Macie, Inspector, Access Analyzer, flow logs, log groups | 3,827 |
| `02-protect-data-protection.json` | KMS keys, backup vaults and recovery points, platform-managed snapshots and buckets | 3,074 |
| `03-protect-platform-identity.json` | Platform roles and policies, org membership, platform tags, member root user | 3,909 |

## The load-bearing bit: 03 is what makes 01 and 02 sound

Policies 01 and 02 exclude platform principals with a wildcard:

```json
"ArnNotLike": { "aws:PrincipalArn": [
  "arn:aws:iam::*:role/Platform*",
  "arn:aws:sts::*:assumed-role/Platform*/*" ] }
```

On its own **that is an open door.** A member account administrator can create a
role named `PlatformAnything`, assume it, and walk straight through every
exclusion in both policies. The exclusion is matched on a name the attacker
controls.

`03-protect-platform-identity.json` closes it by denying creation of any IAM
role, user, policy or instance profile whose name begins `Platform` — including
denying the creation of a role named exactly like a real platform role, which
is why the deny is on the *resource* being created rather than on the principal.

**Consequence: detaching 03 silently weakens 01 and 02.** They will still appear
attached and still read as protective. Treat the three as one unit.

`DenyMemberAccountRootUser` is in 03 for the same reason — the member account
root user is not constrained by IAM policy and would otherwise be an exception
path around everything else.

## What the validator already caught

`scripts/validate-policies.sh` runs IAM Access Analyzer over every document.
Two real defects on the first pass, neither of which is a syntax error and
neither of which would have failed at `create-policy` time:

- **`dynamodb:DeleteBackup` does not support `aws:ResourceTag`.** It sat inside
  a statement conditioned on that key, so it was *not being denied at all* while
  appearing to be. Removed — AWS Backup-managed DynamoDB recovery points are
  covered by the `backup:` statements
- **`StringLike` on `aws:PrincipalArn` should be `ArnLike`.** String comparison
  against ARNs has different semantics and is flagged as a security warning

Neither cfn-lint nor cfn-guard would have found these. They are IAM policy
semantics, not CloudFormation.

## Why JSON files rather than CloudFormation

Everything else in this repository is CloudFormation. These are not, for three
reasons:

1. `AWS::Organizations::Policy` takes the document inline. A CloudFormation
   `String` parameter caps at **4,096 bytes**, below the 5,120-byte policy
   quota, so the content cannot be passed in — it has to be embedded in the
   template, where 4KB of JSON inside YAML is unreviewable
2. Access Analyzer validates a policy *document*. Embedded in YAML it would have
   to be extracted before it could be checked, and the thing validated would not
   be the thing deployed
3. A policy diff is the security review. `git diff` on a JSON file is legible;
   a diff on a JSON blob inside a YAML block scalar is not

Drift detection is handled by comparing live policy content to these files,
rather than by CloudFormation drift.

## Open uncertainties — flagged rather than assumed

Prompt 1.2 asks explicitly for this rather than quiet assumption.

**1. The exact form of `aws:PrincipalArn` for an assumed role.** I have seen
both `arn:aws:iam::<acct>:role/<name>` and
`arn:aws:sts::<acct>:assumed-role/<name>/<session>` documented. Every exclusion
here matches **both** forms, which is correct either way but means the
conditions are wider than strictly necessary. The 1.3 harness must determine
empirically which form appears and the patterns should then be narrowed.

**2. The StackSet execution principal.** `stacksets-exec-*` is the
service-managed StackSet execution role pattern, but this has not been confirmed
against a real deployment and differs between the self-managed and
service-managed permission models. If it is wrong, the baseline StackSet will be
denied by policy 03 when it tries to create platform roles in a member account.
**Confirm at Phase 2.4 before the first baseline deployment.**

**3. `DenyMemberAccountRootUser` is absolute.** It denies every action by a
member account's root user with no exception. That is the correct posture, and
AWS centralised root access management is the better long-term answer, but be
aware it also blocks the small set of operations only root can perform in a
member account. Revisit alongside centralised root access management.

**4. `organizations:DetachPolicy` and friends are denied without exclusion.**
Member accounts have no business detaching policies. This is safe because SCPs
**do not apply to the management account** — policy management continues to work
from there regardless.

## What is deliberately not here

Phase 10.2 bypass controls, which extend the Members OU SCP later: region
restriction, networking outside the baseline VPC, cross-account trust in role
trust policies, and the snapshot/AMI sharing calls
(`ModifyDBSnapshotAttribute`, `ModifySnapshotAttribute`, `ModifyImageAttribute`).

The region restriction is worth pulling forward. It is entirely innocent in
intent, produces a total visibility blind spot, and costs nothing to add while
there are no member accounts. Two policy slots remain at the Members OU.

Also absent: the four-tag creation requirement from doc 10, which belongs with
Phase 9.2 cost allocation, and any Resource Control Policy — the type is enabled
(see [D-003](../../docs/deviations.md)) but no RCP is written yet.

## Before attaching

1. **The Phase 1.3 harness must exist and pass.** Per the build ordering, no
   account may be vested until guardrails are proven to hold against a principal
   with full local administrator. An untested SCP is an assumption, not a control
2. Attach to the **Members OU** first. It currently contains only the empty
   `direct` OU and no accounts, so blast radius is nil — this is the cheapest
   moment this will ever be
3. Confirm the StackSet execution principal (uncertainty 2) before the baseline
   pipeline runs
4. Remember SCPs never apply to the management account. It is not protected by
   any of this, and cannot be
