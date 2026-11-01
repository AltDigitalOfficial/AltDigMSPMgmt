# ADR-019 — Platform secrets live once, in Audit, under a key whose policy is the read grant

**Date:** 2026-10-31
**Deciders:** Jamie (one copy of each secret, in Audit, "until someone tells me to change it", 2026-10-31; tokens re-issued by hand get a reminder, 2026-10-31); Claude (session), unreviewed (the key in Audit only, the grant in the key policy, the cut-over procedure)
**Status:** Proposed
**Tags:** secrets, kms, audit, phase-15.8

---

## Context

The 15.8 survey (2026-10-31) found 13 platform secrets, 11 in Audit and 2 in
Tooling, all in us-east-2, all under the AWS-managed `aws/secretsmanager`
key. None rotates automatically.

The phase 15 plan assumed the fix was to grant consumers access to
`alias/platform-secrets` and re-encrypt. But that key is a member-baseline key.
It exists in the Sandbox and Arc8 accounts, through the management-owned
`platform-kms-secrets` StackSet, and in neither account that holds a secret.

The PagerDuty REST token and the Jira token were each stored in both accounts.
That was a workaround (B-110): a secret under the AWS-managed key cannot be
read across accounts. The Tooling Jira copy had not been read since it was
last written.

Ten roles in Audit read secrets, found from the live IAM policies rather than
from templates:
- the router;
- the commercial notifier;
- two reconciliation roles;
- three quorum roles;
- two evidence roles;
- HIPAA eligibility's tenant-Jira role.

The plan's list missed the quorum roles.

## Decision

1. **One copy of each secret, in Audit.** The Tooling copies are retired, and
   the scripts that read them read Audit's.
2. **A secrets key in Audit only**,
   [security/80-secrets-key.yaml](../../security/80-secrets-key.yaml), stack
   `platform-secrets-key`. It is a one-off platform-account stack. It has the
   baseline key's properties and its quorum deny.
3. **The key policy is the read grant.** `kms:Decrypt` and `DescribeKey` go to
   the ten reader roles by `aws:PrincipalArn`, only via
   `secretsmanager.us-east-2`, and only from Audit. Each reader's IAM policy
   still decides which secrets it may fetch.
4. **The cut-over is per secret and reversible.** Run `update-secret
   --kms-key-id`, then write the same value twice, then exercise the consumer.
   Measured on a probe secret: changing the key does NOT re-encrypt existing
   versions. Only versions written afterwards use the new key. Two writes put
   both AWSCURRENT and AWSPREVIOUS on it. Switching back is the same procedure.

### Amendment, 2026-11-01: the grant's principal, and the cut-over

**As first deployed, the grant did not grant.** The statement named the
account root as Principal, with the reader roles in an `aws:PrincipalArn`
condition. A root principal in a key policy delegates to IAM. It does not
grant. So a reader also needed `kms:Decrypt` in its own IAM policy, and none
had it.

The first secret moved was the Jira token. Two manual reconciliation runs then
got `AccessDeniedException` and reported all four deployments as
`change_source_unavailable`. That produced 8 false digest entries. No Jira
tickets were created. The token was moved back within minutes.

**The fix:** Principal `*`, narrowed by `kms:CallerAccount` and
`aws:PrincipalArn`. This is the pattern AWS uses for its own managed keys,
and it grants to the matched roles directly. After the fix:
- reconciliation returned `no_change_record`, as scheduled runs do;
- CloudTrail recorded `PlatformChangeReconciliation` decrypting under the
  platform key;
- a deliberate low-urgency test page reached PagerDuty and resolved. The
  router reads routing keys uncached, so this proves its read.

**All 11 Audit secrets were moved** on 2026-11-01 by
[scripts/move-secret-key.sh](../../scripts/move-secret-key.sh). It compares
exact fingerprints before and after, and its `--back` flag reverses a move.
The Tooling copies are scheduled for deletion with a 30-day recovery window.
`pagerduty-apply.sh` and the two `set-*-token.sh` scripts now read and write
Audit only.

New secrets are created under the platform key:
- `store_secret_everywhere` uses it wherever `alias/platform-secrets` exists;
- `sync-pagerduty-tenants.sh` uses it for every new tenant's routing key.

`sync-pagerduty-secrets.sh` still creates the platform routing keys without
it. Those keys exist already, so this only bites if they are ever recreated.

## Alternatives considered

- **Widen `platform-kms-secrets` to the Security OU.** Rejected. It is
  management-owned, so the change is a stop condition. It would also put
  member-baseline resources into every Security account to serve one.
- **Grant through each reader's IAM policy.** Rejected. Six stacks would have
  to deploy before re-encryption, and any one missed stops a reader, possibly
  the router. The key-policy grant arrives with the key, and an auditor reads
  it in one place.
- **Name the reader roles as principals.** Rejected. KMS refuses a policy that
  names a role that does not exist, so redeploying a reader's stack could
  block a key-policy update.
- **Distinct credentials per account.** Declined by Jamie for now. They would
  give attribution inside PagerDuty and Jira, at twice the tokens to manage.
  CloudTrail's Decrypt events already show which role read a secret.
- **Automatic rotation.** Not possible for the PagerDuty REST token or the
  Jira token: neither service issues those credentials through an API. A
  reminder replaces it.

## Consequences

**Positive:**
- The secrets' read surface is one key policy.
- Every decrypt is a CloudTrail event naming the role.
- One rotation per token instead of two.
- Cross-account reads become possible, by adding a principal to the key
  policy, if provisioning (B-110) is ever deployed in Tooling.

**Negative:**
- **Old versions keep the AWS-managed key.** After the rewrite they lose their
  labels but persist until Secrets Manager prunes them. A principal that can
  already fetch the secret can still read them by version id.
- **A new reader is a key-policy change** as well as an IAM change. Forgetting
  it fails closed: the reader cannot decrypt.
- **The router is matched on a wildcard** of its generated role name. That
  adds no access, because creating a role in Audit already needs the
  administrative access that the root delegation serves.

## Related

- B-110 (cross-account secrets), B-070 (Jira identity), B-071 (Jira expiry)
- `baseline/30-kms-and-secrets.yaml` (the member-account secrets key)
- `docs/phase-15-plan.md`, 15.8 findings 3 and 4
