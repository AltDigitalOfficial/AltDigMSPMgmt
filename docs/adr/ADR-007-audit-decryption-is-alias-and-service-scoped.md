# ADR-007 — Audit decryption is limited by key alias and by the calling service, not by key ARN

**Date:** 2026-10-08
**Recorded:** 2026-10-14
**Deciders:** Claude (session), unreviewed
**Status:** Proposed
**Tags:** identity, kms, audit, encryption

---

## Context

`SecurityReadOnly` denies `kms:Decrypt` outright, and that deny is what makes
every SecureString and encrypted object unreadable to it. `AuditEvidenceReader`
(ADR-005) cannot do the same. The org trail, Config archive and flow log
archive are SSE-KMS under `alias/platform-log-archive`, with replicas under
`alias/platform-archive-replica`. They cannot be read without decrypting.

The constraints:

- Key ARNs are random per account and region, and CLAUDE.md forbids
  hardcoding account ids, so the archive keys cannot be named by ARN in a
  permission set.
- The log archive key's policy delegates to IAM in its account (the
  `EnableIAMPolicyDelegation` statement in
  `security/10-log-archive.yaml`), so an identity policy is enough; no key
  policy change is needed.
- The platform's evidence tables use the AWS-managed `aws/dynamodb` key,
  not a customer-managed key. This is the unrecorded deviation in
  auditor-guide §11.

## Decision

In [identity/40-audit-evidence-reader.yaml](../../identity/40-audit-evidence-reader.yaml):

- **Allow** `kms:Decrypt` on `arn:aws:kms:*:*:key/*`, conditioned on
  `kms:ResourceAliases` being one of `platform-log-archive`,
  `platform-archive-replica` or `platform-audit-query`, **and** on
  `kms:ViaService` matching `s3.*.amazonaws.com`.
  `GenerateDataKey` is allowed the same way, for `platform-audit-query` only,
  so Athena can write results.
- **Deny** every decrypt and data-key action:
  - on any key that does not carry one of those aliases or
    `alias/aws/dynamodb`. A key with no alias is denied too, because
    `ForAllValues` over an empty set is true;
  - whenever `kms:ViaService` is not S3 or DynamoDB, which includes every
    direct call.

## Alternatives considered

- **Keep `SecurityReadOnly`'s blanket decrypt deny.** Rejected: the trail
  would stay unreadable, which is the gap ADR-005 exists to close.
- **Name the keys by ARN.** Rejected: that hardcodes account ids, and it
  breaks silently when a key is recreated.
- **Allow decryption by alias only, without a ViaService condition.**
  Rejected: ciphertext lifted from anywhere and encrypted under an archive
  key could then be decrypted directly. With the ViaService condition, the
  keys open archive objects through S3 and nothing else.
- **Grant through the key policies instead of IAM.** Rejected:
  - three key policies across two regions to change;
  - key policy changes are denied by SCP 02, so each change would need the
    quorum path;
  - the grant would then live away from the permission set it belongs to.

## Rationale

The narrowest expression available that names no account and survives key
recreation. The Allow alone would be enough today. The two Denies make the
line hold even if a managed policy or a future attachment grows a broader
KMS grant, which is the same reasoning `SecurityReadOnly` records for its own
denies.

## Consequences

**Positive:**
- The trail, the Config archive, the flow log archive and their replicas are
  readable.
- Secrets, the registry (`alias/platform-registry`), alert payloads and every
  tenant key remain undecryptable.

**Negative:**
- **Unverified for the AWS-managed DynamoDB key.** Whether DynamoDB's use of
  `aws/dynamodb` is evaluated against this identity policy, and whether
  `kms:ResourceAliases` carries `alias/aws/dynamodb` for an AWS-managed key,
  has not been tested. If evidence-table reads fail with a KMS AccessDenied,
  this is the cause. The fix is moving those tables to a customer-managed key,
  which design 04 already wants, not a wider exemption.
- **An alias is a weaker name than an ARN, and re-pointing one is not
  prevented.** SCP 02 v2, which is on the Security OU, denies
  `kms:DeleteAlias` but **not** `kms:UpdateAlias` (checked 2026-10-14). A
  principal in Log Archive holding `UpdateAlias` could re-point
  `alias/platform-log-archive` at a key of its choosing, and this grant would
  follow it to that key. Today that means an operator using OAAR, which is
  already SCP-exempt. Adding `kms:UpdateAlias` for `alias/platform-*` to SCP
  02 is the fix. It is not done here.
- If a condition key fails to match the way it is expected to, the deny could
  be wider or narrower than intended. Both directions must be tested before an
  external assessor receives the set.

**Enforcement:**
- The two Deny statements in the permission set.
- SCP 02 denies `DeleteAlias` only. `UpdateAlias` is unprotected (see
  Negative).

## Related

- ADR-005, ADR-008
- `identity/30-security-readonly.yaml` (statement 3, the deny this replaces)
- `security/10-log-archive.yaml`, `security/20-archive-replicas.yaml`
- auditor-guide §11 (the DynamoDB AWS-managed key deviation)
