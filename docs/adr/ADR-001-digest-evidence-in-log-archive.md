# ADR-001 — Digest send records live in the locked Log Archive, written cross-account by one role

**Date:** 2026-10-13
**Deciders:** Jamie
**Status:** Accepted
**Tags:** evidence, digest, log-archive, kms, cross-account

---

## Context

The instrumentation digest (prompt 4.4, `reporting/src/digest.py`) runs in
Infra Tooling and writes one send record per digest, which design 06 treats as
the evidence that monitoring requirements were communicated to the client.
Prompt 4.4 wants the record in Truveon. Truveon does not exist yet, so the code
pointed the records at the Object Lock log archive
(`altdig-log-archive-<logarchive>`).

That write never worked (B-169). Until 2026-10-13 the archive bucket had been
written only by AWS service principals (CloudTrail, Config and flow-log
delivery), and its bucket and key policies granted nothing to an IAM role in
another account. Fixing it meant choosing where the records live, because
making the existing target work makes the archive's first IAM-role writer.

## Decision

Digest send records live in the locked log archive under `digest-evidence/`.
Infra Tooling's `PlatformDigestSender` role is granted:

- `s3:PutObject` on `digest-evidence/*` only (bucket policy `DigestEvidenceWrite`).
  No GetObject, ListBucket or DeleteObject.
- `kms:GenerateDataKey` on the archive key (key policy
  `AllowDigestEvidenceEncrypt`, mirrored on the role), conditioned on
  `kms:ViaService = s3.<region>` and an encryption context of the locked
  bucket's ARN.

Both grants exist only when `DigestAccountId` is passed to
`security/10-log-archive.yaml`.

## Alternatives considered

- **A new Object Lock bucket in Infra Tooling.** No cross-account write, and
  the archive would stay writable by service principals only. Rejected: the
  evidence would sit in the account that generates it, so whoever controls
  Infra Tooling controls both the sender and its proof of sending. It would
  also be a new bucket for custody, replication and retention to cover.
- **Leave the records out until Truveon exists.** Rejected: design 06 makes
  the record the evidence, and the gap was already six days long.

## Rationale

The record exists to prove to an auditor that something was communicated. Its
value depends on custody being separate from the thing it attests to.
Separating the writer from the custodian is what the Log Archive account is
for. The narrowed grant keeps the cost small: the role can add records under
one prefix, cannot read, list or mask them, and Object Lock COMPLIANCE holds
each one for `RetentionDays`.

## Consequences

**Positive:** send records are immutable and held outside the producing
account, and are replicated with the rest of the archive.
**Negative:** the archive's write surface now includes an IAM role, not only AWS
services. A compromised `PlatformDigestSender` can write arbitrary objects
under `digest-evidence/` for the retention period. It cannot alter or hide
existing ones, but it can add noise that Object Lock then keeps for six years.
**Enforcement:** the bucket and key policies in `security/10-log-archive.yaml`;
the role policy in `reporting/10-digest-sender.yaml`;
`platform-digest-not-delivered`, which counts a failed record write as a failed
digest.

## Not decided here

- Where the records go once Truveon exists. Moving or dual-writing them is the
  Truveon hand-off's decision.
- Whether the twelve missing records (2026-10-08 to 10-13) get anything beyond
  the single gap record. Jamie ruled 2026-10-13 that the bodies are not
  reconstructed (B-169).

## Related

B-169, B-155, design 06, prompt 4.4, `security/10-log-archive.yaml`,
`reporting/10-digest-sender.yaml`. The archive keys are now named by ARN rather
than bare id; that is a fix with one right answer and is recorded in B-169 and
in the template, not here.
