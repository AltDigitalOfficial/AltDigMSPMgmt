# ADR-005 — Audit evidence access is its own permission set, not a wider SecurityReadOnly

**Date:** 2026-10-08
**Recorded:** 2026-10-14
**Deciders:** Jamie
**Status:** Accepted — not built. Pending: deployment by `scripts/deploy-audit-access.sh`, then proof of every allow and deny against real resources.
**Tags:** identity, audit, permission-set, soc2, hipaa, personal-data

---

## Context

The CISO is taking the platform through a SOC 2 and HIPAA dress rehearsal
([auditor-guide.md](../auditor-guide.md)). An external assessor is expected
after that. The only estate-wide read grant was `SecurityReadOnly`
([identity/30-security-readonly.yaml](../../identity/30-security-readonly.yaml)),
which is the AWS managed policies `SecurityAudit` and `ViewOnlyAccess` minus
four denies.

On 2026-10-08 those managed policies (SecurityAudit v94, ViewOnlyAccess v46)
were checked action by action against what an audit samples. They show how a
control is *configured* and almost none of what it *produced*:

- no `s3:GetObject`, and `kms:Decrypt` is denied, so the org trail is
  unreadable past the 90 days `LookupEvents` reaches, although it is retained
  for six years;
- no `s3:GetObjectRetention`, so per-object Object Lock cannot be shown;
- no CloudWatch Logs contents;
- no DynamoDB item reads, so the evidence ledger, change events, quorum and
  incident records are unreadable;
- no Step Functions execution history;
- no `ssm:GetParameter`, so `/platform/*` is invisible.

`SecurityReadOnly`'s membership is the security function **plus every quorum
approver**. That is recorded in its template: approvers hold it so that they
can see what they approve.

## Decision

Evidence access is a **separate** permission set, `AuditEvidenceReader`
([identity/40-audit-evidence-reader.yaml](../../identity/40-audit-evidence-reader.yaml)),
assigned to its own `Auditors` group. `SecurityReadOnly` is unchanged.

Jamie, 2026-10-08, choosing between the two: "separate".

## Alternatives considered

- **Widen `SecurityReadOnly`.** One permission set, one group, one deployment.
  Rejected: every quorum approver would gain log contents and archived
  CloudTrail. Those carry personal data (principal names, source IPs), and an
  approver needs none of it to judge a change. It would also turn a standing
  role into the auditor's role, and an external assessor's access has to be
  given for an engagement and removed afterwards.
- **Add the reads as a customer-managed policy attached to `SecurityReadOnly`
  for named people.** Rejected: Identity Center assigns permission sets to
  groups, not to people, and the platform assigns to groups only. "Some
  members of this group can read more" is not expressible.
- **No AWS access for auditors; operators export evidence on request.**
  Rejected for the rehearsal: the auditor would see only what the operator
  chose to export, which is the problem `SecurityReadOnly` was written to solve
  for approvers. It remains how evidence reaches a *client's* auditor where no
  direct access is granted (see *Not decided here*).

## Rationale

Least privilege by population. The approver population needs configuration
visibility. The audit population needs evidence. They overlap today: the CISO
is in both. They will not overlap for an external firm. Keeping them as two
sets means each can be granted, reviewed and removed on its own terms.

## Consequences

**Positive:**
- `SecurityReadOnly` stays exactly as proved on 2026-09-23.
- An external assessor can be given evidence access without becoming a
  standing member of anything.

**Negative:**
- **Two copies of the same denies.** `AuditEvidenceReader` repeats
  `SecurityReadOnly`'s identity-mutation and control-disabling denies
  verbatim, because a permission set cannot reference another's inline
  policy. If one changes, both must. This is a manual discipline only, and the
  template says so.
- **Not proved.** `SecurityReadOnly`'s denies were attacked against real
  resources. This set's have not been, and must be before an external assessor
  receives it.
- One more group whose membership must be reviewed. There is still no periodic
  access review (auditor-guide §3).

**Enforcement:**
- cfn-guard `SSO_PERMISSION_SET_*` rules: no `Action: *`, no
  `AdministratorAccess`, bounded session.
- The inline policy is 7,781 characters, against IAM's 10,240-character limit
  for a role's inline policies.

## Not decided here

- **Scope for engagements on a single client's accounts.** The set is
  assigned estate-wide and reads cross-tenant evidence (the ledger, the whole
  trail). An auditor engaged by one tenant must see only that tenant. Raised
  on 2026-10-08 ahead of the meeting with a prospective outside firm. The shape
  depends on how that firm handles client engagements (carve-out or
  inclusive), which this record does not know.

## Related

- ADR-006 (how the grant is scoped and ended), ADR-007 (decryption), ADR-008
  (trail query)
- [auditor-guide.md §1](../auditor-guide.md)
- `identity/30-security-readonly.yaml`, `identity/40-audit-evidence-reader.yaml`
